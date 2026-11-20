"""MicroVM 内で動く image/supervisor.py と image/bootstrap.sh の結合テスト (Linux + tmux のみ)。

パスとポートを差し替えたコピーをローカルで動かし、Lambda MicroVMs のフック呼び出しを真似る。
"""

import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
HOOK = "/aws/lambda-microvms/runtime/v1/"


class WebhookServer(http.server.ThreadingHTTPServer):
    """通知の送り先 (Slack の Incoming Webhook の代わり)。"""

    def __init__(self):
        self.bodies = []
        srv = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                srv.bodies.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.end_headers()

        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}/hook"
        threading.Thread(target=self.serve_forever, daemon=True).start()


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("tmux") and shutil.which("jq"),
                     "Linux と tmux / jq が必要")
class SupervisorTest(unittest.TestCase):
    # テスト名 → supervisor の起動を遅らせる秒数
    SLOW_START = {"test_setup_waits_for_slow_supervisor_start": 6}

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        opt, home, bindir = t / "opt", t / "home", t / "bin"
        for d in (opt, home, bindir):
            d.mkdir()
        self.hook_port, self.api_port = free_port(), free_port()
        src = (ROOT / "image" / "supervisor.py").read_text(encoding="utf-8")
        src = src.replace("/opt/pmva", str(opt)).replace("HOOK_PORT = 9000", f"HOOK_PORT = {self.hook_port}")
        src = src.replace("API_PORT = 8080", f"API_PORT = {self.api_port}")
        # メモリ不足による強制終了の回数は、テストでは書き換えられるファイルから読む
        self.vmstat = t / "vmstat"
        self.vmstat.write_text("nr_free_pages 1\noom_kill 3\n", encoding="utf-8")
        src = src.replace('"/proc/vmstat"', repr(str(self.vmstat)))
        # テストでは作業判定と状態の書き出しを 1 秒ごとにする
        src = src.replace("ACTIVITY_INTERVAL = 30", "ACTIVITY_INTERVAL = 1").replace("PUBLISH_INTERVAL = 60", "PUBLISH_INTERVAL = 1")
        # 混んだ CI のマシンで supervisor の起動が遅れる場合を真似る (test_setup_waits_for_slow_supervisor_start だけ)
        delay = self.SLOW_START.get(self._testMethodName)
        if delay:
            src = src.replace("def main():\n", f"def main():\n    time.sleep({delay})\n", 1)
        (opt / "supervisor.py").write_text(src, encoding="utf-8")
        for name in ("bootstrap.sh", "snapshot.sh", "notify.sh"):
            shutil.copy(ROOT / "image" / name, opt / name)
            (opt / name).chmod(0o755)
        # エージェントのスタブ (--version はすぐ終わり、それ以外は待ち続ける)
        for c in ("claude", "codex"):
            p = bindir / c
            p.write_text(f'#!/bin/sh\n[ "$1" = --version ] && echo "{c} stub" && exit 0\n'
                         # codex login --with-api-key は標準入力のキーを記録して終わる
                         f'[ "$1" = login ] && cat > "$HOME/.{c}-login-stub" && exit 0\n'
                         f'echo "{c} started: $*"\nexec sleep 600\n')
            p.chmod(0o755)
        # aws のスタブ: s3 cp の内容を保存し、シークレットは STUB_SECRETS/<名前の / を _ に> の中身を返す
        self.s3_out = t / "s3"
        self.s3_out.mkdir()
        self.s3_in = t / "s3-in"
        self.s3_in.mkdir()
        self.secrets = t / "secrets"
        self.secrets.mkdir()
        aws = bindir / "aws"
        aws.write_text('#!/bin/sh\n'
                       'case "$1 $2" in\n'
                       '  "s3 cp") if [ "$4" = - ]; then cat > "$STUB_S3_OUT/$(basename "$5")"; '
                       # S3 → VM (設定のまとまりなど) は STUB_S3_IN/<ファイル名> から読む
                       'elif [ "${4#s3://}" != "$4" ]; then cp "$STUB_S3_IN/$(basename "$4")" "$5"; '
                       'else cp "$4" "$STUB_S3_OUT/$(basename "$5")"; fi ;;\n'
                       '  "secretsmanager get-secret-value")\n'
                       '    f="$STUB_SECRETS/$(echo "$4" | tr / _)"\n'
                       '    [ -f "$f" ] || { echo "ResourceNotFoundException: $4" >&2; exit 254; }\n'
                       '    cat "$f" ;;\n'
                       'esac\n')
        aws.chmod(0o755)
        self.webhook = WebhookServer()
        self.add_secret("pmva/notify-webhook", self.webhook.url)
        # tmux 内のシェルが環境の設定で PATH を上書きしても、スタブが先に見つかるようにする
        # (tmux はログインシェルで起動するので .bash_profile も用意する)
        for rc in (".bashrc", ".bash_profile"):
            (home / rc).write_text(f'export PATH="{bindir}:$PATH"\n', encoding="utf-8")
        self.home = home
        self.env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}", TMUX_TMPDIR=str(t),
                        STUB_S3_OUT=str(self.s3_out), STUB_S3_IN=str(self.s3_in), STUB_SECRETS=str(self.secrets),
                        HISTFILE="")
        self.opt = opt
        self.env.pop("TMUX", None)
        # supervisor の標準エラーは、起動に失敗したときの手がかりとしてファイルに残す
        self.stderr_path = t / "supervisor.stderr"
        with open(self.stderr_path, "wb") as err:
            self.proc = subprocess.Popen([sys.executable, str(opt / "supervisor.py")], env=self.env,
                                         stdout=subprocess.DEVNULL, stderr=err)
        self.wait_supervisor()

    def wait_supervisor(self, timeout=30):
        """API とフックの両方のポートが開くまで待つ。

        フックのサーバーは別スレッドで起動するので、API が応答してもフックがまだのことがある。
        混んだ CI のマシンでは起動に 5 秒以上かかることがあるので、余裕を持って待ち、
        間に合わなければ黙って進まずに失敗させる (後のテストで Connection refused になって原因が分かりにくくなるため)。
        """
        deadline = time.monotonic() + timeout
        while True:
            code = self.proc.poll()
            if code is not None:
                self.fail_setup(f"supervisor が起動の途中で終了しました (終了コード {code})")
            try:
                for port in (self.api_port, self.hook_port):
                    socket.create_connection(("127.0.0.1", port), timeout=1).close()
                self.get("/healthz")
                return
            except OSError:
                if time.monotonic() > deadline:
                    self.fail_setup(f"supervisor が {timeout} 秒たってもポートを開きません")
                time.sleep(0.1)

    def fail_setup(self, message):
        # setUp で失敗すると tearDown は呼ばれないので、ここで後片付けしてから失敗させる
        stderr = self.supervisor_stderr()
        self.tearDown()
        self.fail(f"{message}:\n{stderr}")

    def supervisor_stderr(self):
        return self.stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]

    def tearDown(self):
        self.webhook.shutdown()
        subprocess.run(["tmux", "kill-server"], env=self.env, capture_output=True)
        self.proc.terminate()
        self.proc.wait(10)
        # tmux を止めた直後は、ペインのシェルやエージェントのスタブが終了処理で HOME に書き込むことがある。
        # 消している途中に書き込まれると rmtree が失敗するので、少し待ってやり直す
        for attempt in range(10):
            try:
                self.tmp.cleanup()
                return
            except OSError:
                time.sleep(0.5)
        self.tmp.cleanup()

    def request(self, port, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def add_secret(self, name, value):
        (self.secrets / name.replace("/", "_")).write_text(value, encoding="utf-8")

    def get(self, path):
        return self.request(self.api_port, "GET", path)

    def hook(self, name, body=None):
        return self.request(self.hook_port, "POST", HOOK + name, body if body is not None else {})

    def wait_ready(self):
        for _ in range(100):
            _, st = self.get("/status?lines=5")
            if st["bootstrap"].startswith(("ready", "failed")):
                return st
            time.sleep(0.2)
        self.fail("bootstrap did not finish")

    def test_setup_waits_for_slow_supervisor_start(self):
        """起動に 5 秒以上かかっても、setUp は API とフックの両方のポートが開くまで待つ (CI で Connection refused になっていた)。"""
        code, _ = self.get("/healthz")
        self.assertEqual(code, 200)
        socket.create_connection(("127.0.0.1", self.hook_port), timeout=5).close()

    def test_validate_answers_503_then_200(self):
        codes = []
        for _ in range(50):
            code, _ = self.hook("validate")
            codes.append(code)
            if code != 503:
                break
            time.sleep(0.2)
        self.assertEqual(codes[-1], 200, codes)

    def test_run_hook_unwraps_payload_and_starts_agents(self):
        payload = {"name": "t1", "agent": "both", "claudeAuth": "none", "codexAuth": "none",
                   "prompt": "README を読んで \"要約\" して", "snapshotIntervalSeconds": 0}
        # Lambda MicroVMs は runHookPayload を文字列のまま本文に包んで渡す (PoC で確認)
        code, _ = self.hook("run", {"microvmId": "microvm-test", "runHookPayload": json.dumps(payload)})
        self.assertEqual(code, 200)
        session = json.loads((self.home / ".pmva" / "session.json").read_text(encoding="utf-8"))
        self.assertEqual(session["name"], "t1")
        self.assertEqual(session["microvmId"], "microvm-test")
        st = self.wait_ready()
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual(sorted(w["name"] for w in st["windows"]), ["claude", "codex", "shell"])
        time.sleep(1)
        _, st = self.get("/status?lines=5")
        screens = {w["name"]: w.get("screen", "") for w in st["windows"]}
        # 初期指示がシェル用にエスケープされ、エージェントに 1 つの引数として渡ること
        self.assertIn('claude started: README を読んで "要約" して', screens["claude"])
        self.assertIn('codex started: README を読んで "要約" して', screens["codex"])
        claude_json = json.loads((self.home / ".claude.json").read_text(encoding="utf-8"))
        self.assertTrue(claude_json["hasCompletedOnboarding"])
        self.assertIn('trust_level = "trusted"', (self.home / ".codex" / "config.toml").read_text(encoding="utf-8"))

    def test_send_keys_to_window(self):
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps({"name": "t2", "agent": "none"})})
        self.wait_ready()
        code, r = self.request(self.api_port, "POST", "/send", {"window": "shell", "text": "echo 送信テスト-OK"})
        self.assertEqual((code, r["ok"]), (200, True))
        time.sleep(1)
        out = subprocess.run(["tmux", "capture-pane", "-p", "-t", "main:shell"], env=self.env,
                             capture_output=True, text=True).stdout
        self.assertIn("送信テスト-OK", out)
        # "-" で始まる指示も tmux のオプションと解釈されずにそのまま入力される
        code, r = self.request(self.api_port, "POST", "/send", {"window": "shell", "text": "--window echo dash-OK"})
        self.assertEqual((code, r["ok"]), (200, True), r)
        code, r = self.request(self.api_port, "POST", "/send", {"window": "nope", "text": "x"})
        self.assertEqual(code, 500)
        self.assertTrue(r["error"])
        self.assertEqual(self.get("/status?lines=abc")[0], 400)

    def test_activity_tracking_and_status_publish(self):
        payload = {"name": "act", "agent": "none", "region": "ap-northeast-1", "snapshotBucket": "bkt"}
        self.hook("run", {"microvmId": "microvm-act", "runHookPayload": json.dumps(payload)})
        self.wait_ready()
        published = self.s3_out / "act.json"
        for _ in range(50):
            if published.exists():
                break
            time.sleep(0.2)
        st = json.loads(published.read_text(encoding="utf-8"))
        self.assertEqual((st["name"], st["microvmId"]), ("act", "microvm-act"))
        self.assertLess(time.time() - st["updatedAt"], 30)
        # 画面が変わらなければ idleSeconds が増え、入力で画面が変わると 0 近くに戻る
        time.sleep(3.5)
        _, before = self.get("/status?lines=0")
        self.assertGreaterEqual(before["idleSeconds"], 2)
        self.request(self.api_port, "POST", "/send", {"window": "shell", "text": "echo activity"})
        time.sleep(2.5)
        _, after = self.get("/status?lines=0")
        self.assertLess(after["idleSeconds"], before["idleSeconds"])
        # CPU を使う処理 (ビルドやテストの代わり) も作業中とみなす
        self.request(self.api_port, "POST", "/send", {"window": "shell", "text": "clear"})
        time.sleep(3.5)
        self.request(self.api_port, "POST", "/send",
                     {"window": "shell", "text": "python3 -c 'import time; t=time.time()\nwhile time.time()-t<4: pass' >/dev/null"})
        time.sleep(4)
        _, busy = self.get("/status?lines=0")
        self.assertLess(busy["idleSeconds"], 3)

    def test_notifications(self):
        payload = {"name": "nt", "agent": "none", "notifySecret": "pmva/notify-webhook"}
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps(payload)})
        self.wait_ready()
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertIn("notify.sh claude-stop", json.dumps(settings["hooks"]["Stop"]))
        self.assertIn("notify.sh claude-notification", json.dumps(settings["hooks"]["Notification"]))
        codex_cfg = (self.home / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertTrue(codex_cfg.startswith('notify = ["/opt/pmva/notify.sh", "codex"]'), codex_cfg)
        notify = str(self.opt / "notify.sh")
        subprocess.run([notify, "claude-stop"], input=b'{"hook_event_name":"Stop"}', env=self.env, check=True)
        subprocess.run([notify, "claude-notification"],
                       input='{"message":"Claude needs your permission to use Bash"}'.encode(), env=self.env, check=True)
        subprocess.run([notify, "codex", json.dumps({"type": "agent-turn-complete", "last-assistant-message": "テスト完了"})],
                       env=self.env, check=True)
        subprocess.run([notify, "codex", json.dumps({"type": "other"})], env=self.env, check=True)
        subprocess.run([notify, "claude-stop-failure"], input=json.dumps(
            {"error": "rate_limit", "last_assistant_message": "API Error: Rate limit reached"}).encode(), env=self.env, check=True)
        texts = [b["text"] for b in self.webhook.bodies]
        self.assertEqual(texts, ["[nt] Claude Code の応答が終わりました",
                                 "[nt] Claude Code: Claude needs your permission to use Bash",
                                 "[nt] Codex の応答が終わりました: テスト完了",
                                 "[nt] Claude Code がエラーで止まりました (rate_limit): API Error: Rate limit reached"])

    # ---------------------------------------------------------------- エージェントの状態と最後の応答 (mvm wait / result)

    def agents(self):
        return {a: v["state"] for a, v in self.get("/status?lines=0")[1]["agents"].items()}

    def notify(self, *args, stdin=None):
        subprocess.run([str(self.opt / "notify.sh"), *args], input=(stdin or "").encode(), env=self.env, check=True)

    def test_agent_states_follow_hooks(self):
        # 利用者が自分で書いたフックは残す
        (self.home / ".claude").mkdir()
        (self.home / ".claude" / "settings.json").write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo mine"}]}]}}), encoding="utf-8")
        payload = {"name": "st", "agent": "both", "claudeAuth": "none", "codexAuth": "none", "prompt": "テストして"}
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps(payload)})
        self.wait_ready()
        # 2 回目の起動 (resume 後の run など) でも pmva のフックは重複しない
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps(payload)})
        self.wait_ready()
        hooks = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual([h["hooks"][0]["command"] for h in hooks["Stop"]], ["echo mine", "/opt/pmva/notify.sh claude-stop"])
        for event, kind in (("UserPromptSubmit", "claude-prompt"), ("PostToolUse", "claude-tool"),
                            ("Notification", "claude-notification")):
            self.assertEqual([h["hooks"][0]["command"] for h in hooks[event]], [f"/opt/pmva/notify.sh {kind}"])
        codex_cfg = (self.home / ".codex" / "config.toml").read_text(encoding="utf-8")
        self.assertEqual(codex_cfg.count("notify ="), 1)

        # 最初の指示を渡して起動したので作業中
        self.assertEqual(self.agents(), {"claude": "working", "codex": "working"})
        self.notify("claude-stop", stdin="{}")
        self.notify("codex", json.dumps({"type": "agent-turn-complete", "last-assistant-message": "直しました"}))
        self.assertEqual(self.agents(), {"claude": "done", "codex": "done"})
        # 応答の後にしばらく入力がないときの通知では「完了」のまま。許可を求める通知なら入力待ち
        self.notify("claude-notification", stdin=json.dumps({"message": "Claude is waiting for your input",
                                                             "notification_type": "idle_prompt"}))
        self.assertEqual(self.agents()["claude"], "done")
        self.notify("claude-notification", stdin=json.dumps({"message": "Claude needs your permission to use Bash",
                                                             "notification_type": "permission_prompt"}))
        _, st = self.get("/status?lines=0")
        self.assertEqual(st["agents"]["claude"]["state"], "waiting")
        self.assertEqual(st["agents"]["claude"]["message"], "Claude needs your permission to use Bash")
        self.assertIsInstance(st["agents"]["claude"]["since"], float)
        # 許可した後にツールが動けば作業中に戻る
        self.notify("claude-tool", stdin="{}")
        self.assertEqual(self.agents()["claude"], "working")
        # mvm send で指示を送れば作業中 (Codex には指示を受け取ったときのフックがない)
        code, _ = self.request(self.api_port, "POST", "/send", {"window": "codex", "text": "次はこれ"})
        self.assertEqual(code, 200)
        self.assertEqual(self.agents()["codex"], "working")
        # Enter を送らないときや、shell ウィンドウへの送信では変えない
        self.notify("codex", json.dumps({"type": "agent-turn-complete", "last-assistant-message": "done"}))
        self.request(self.api_port, "POST", "/send", {"window": "codex", "text": "途中", "enter": False})
        self.request(self.api_port, "POST", "/send", {"window": "shell", "text": "true"})
        self.assertEqual(self.agents()["codex"], "done")
        # エージェントが終了してシェルに戻ったら exited
        subprocess.run(["tmux", "send-keys", "-t", "main:codex", "C-c"], env=self.env, check=True)
        for _ in range(50):
            if self.agents()["codex"] == "exited":
                break
            time.sleep(0.1)
        self.assertEqual(self.agents()["codex"], "exited")

    def test_agent_error_state_from_stop_failure(self):
        # 認証の失敗や利用上限など API のエラーで応答が終わると、Claude Code は Stop ではなく StopFailure を呼ぶ
        payload = {"name": "er", "agent": "claude", "claudeAuth": "none", "prompt": "テストして"}
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps(payload)})
        self.wait_ready()
        hooks = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))["hooks"]
        self.assertEqual([h["hooks"][0]["command"] for h in hooks["StopFailure"]],
                         ["/opt/pmva/notify.sh claude-stop-failure"])
        self.assertEqual(self.agents(), {"claude": "working"})
        message = "Failed to authenticate. API Error: 401 OAuth access token is invalid."
        self.notify("claude-stop-failure", stdin=json.dumps(
            {"hook_event_name": "StopFailure", "error": "authentication_failed", "last_assistant_message": message}))
        _, st = self.get("/status?lines=0")
        claude = st["agents"]["claude"]
        self.assertEqual((claude["state"], claude["error"], claude["message"]), ("error", "authentication_failed", message))
        # 応答の後にしばらく入力がないときの通知では、エラーを「入力待ち」で上書きしない
        self.notify("claude-notification", stdin=json.dumps({"message": "Claude is waiting for your input",
                                                             "notification_type": "idle_prompt"}))
        self.assertEqual(self.agents()["claude"], "error")
        _, r = self.get("/result?agent=claude")
        self.assertEqual((r["state"], r["error"], r["text"]), ("error", "authentication_failed", message))
        # 次の指示を受け取れば作業中に戻る
        self.notify("claude-prompt", stdin="{}")
        self.assertEqual(self.agents()["claude"], "working")
        _, st = self.get("/status?lines=0")
        self.assertNotIn("error", st["agents"]["claude"])

    def test_agent_state_before_first_prompt(self):
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps({"name": "r", "agent": "claude"})})
        self.wait_ready()
        self.assertEqual(self.agents(), {"claude": "ready"}, "最初の指示がなければ指示待ち")
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps({"name": "n", "agent": "none"})})
        self.wait_ready()
        self.assertEqual(self.agents(), {}, "エージェントを起動していなければ空")

    def test_result_returns_last_answer(self):
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps({"name": "res", "agent": "both"})})
        self.wait_ready()
        self.assertEqual(self.get("/result?agent=claude")[1]["text"], None, "まだ会話がなければ None")
        # Claude Code の会話記録: 最後のツール結果より後の文章だけが最後の応答
        records = [
            {"type": "user", "message": {"role": "user", "content": "テストを直して"}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "調べます"}]}},
            {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {}}]}},
            {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "assistant", "message": {"content": [{"type": "thinking", "thinking": ""}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "テストが通りました。"}]}},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "PR も作りました。"}]}},
            {"type": "user", "isMeta": True, "message": {"content": "<local-command-caveat>"}},
            {"type": "system", "content": "x"},
        ]
        project = self.home / ".claude" / "projects" / "-home-agent-work"
        project.mkdir(parents=True)
        transcript = project / "s1.jsonl"
        transcript.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n", encoding="utf-8")
        # Stop フックが会話記録の場所を渡さなくても、いちばん新しい記録を読む
        self.notify("claude-stop", stdin="{}")
        _, r = self.get("/result?agent=claude")
        self.assertEqual((r["agent"], r["state"], r["text"]), ("claude", "done", "テストが通りました。\n\nPR も作りました。"))
        # Stop フックが渡した場所を優先する
        other = project / "s0.jsonl"
        other.write_text(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "別の会話"}]}},
                                    ensure_ascii=False) + "\n", encoding="utf-8")
        self.notify("claude-stop", stdin=json.dumps({"transcript_path": str(transcript)}))
        self.assertEqual(self.get("/result?agent=claude")[1]["text"], "テストが通りました。\n\nPR も作りました。")
        self.notify("claude-stop", stdin=json.dumps({"transcript_path": str(other)}))
        self.assertEqual(self.get("/result?agent=claude")[1]["text"], "別の会話")
        # Stop フックが最後の応答の文章を渡す版では、それをそのまま使う
        self.notify("claude-stop", stdin=json.dumps({"transcript_path": str(other), "last_assistant_message": "フックの応答"}))
        self.assertEqual(self.get("/result?agent=claude")[1]["text"], "フックの応答")
        # Codex は notify で受け取った最後の応答 (切り詰めない)
        long_answer = "変更点:\n" + "あ" * 1000
        self.notify("codex", json.dumps({"type": "agent-turn-complete", "last-assistant-message": long_answer}))
        self.assertEqual(self.get("/result?agent=codex")[1]["text"], long_answer)
        _, st = self.get("/status?lines=0")
        self.assertEqual(len(st["agents"]["codex"]["message"]), 300, "/status では短くする")
        self.assertEqual(self.get("/result?agent=x")[0], 400)

    # ---------------------------------------------------------------- 会話記録の流れ (mvm follow) とメモリ

    def place(self, fixture, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIXTURES / fixture, dest)
        return dest

    def events(self, agent, **q):
        query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in dict(agent=agent, **q).items())
        code, r = self.get(f"/events?{query}")
        self.assertEqual(code, 200, r)
        return r

    def test_events_follow_real_transcripts(self):
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps({"name": "ev", "agent": "both"})})
        self.wait_ready()
        self.assertEqual(self.events("claude")["events"], [], "まだ会話がなければ空")
        self.assertEqual(self.events("claude", file="-")["file"], None)
        # 実際の Claude Code 2.1.325 / Codex 0.165.0 が書いた会話記録 (tests/fixtures)
        claude = self.place("claude-2.1.325-transcript.jsonl", self.home / ".claude" / "projects" / "-home-agent-work-repo" / "a.jsonl")
        codex = self.place("codex-0.165.0-rollout.jsonl", self.home / ".codex" / "sessions" / "2026" / "11" / "15" / "rollout-x.jsonl")
        expected = [("prompt", None, "テストして"), ("thinking", None, "まずコマンドを実行する"), ("text", None, "確認します。"),
                    ("tool", "Bash", "echo hello-from-tool"), ("output", None, "hello-from-tool"), ("text", None, "完了しました。")]
        r = self.events("claude", tail=50)
        self.assertEqual([(e["kind"], e.get("name"), e["text"]) for e in r["events"]], expected)
        self.assertEqual((r["file"], r["offset"]), (str(claude), claude.stat().st_size))
        self.assertTrue(r["events"][0]["ts"].startswith("2026-"))
        r = self.events("codex", tail=50)
        self.assertEqual([(e["kind"], e.get("name"), e["text"].rstrip("\n")) for e in r["events"]],
                         [("prompt", None, "テストして"), ("thinking", None, "**手順を考える** ファイルを確認する"),
                          ("text", None, "確認します。"), ("tool", "exec_command", "echo hello-from-tool"),
                          ("output", None, "hello-from-tool"), ("text", None, "完了しました。")])
        # 初回は直前の tail 件だけ
        self.assertEqual([e["text"] for e in self.events("claude", tail=2)["events"]], ["hello-from-tool", "完了しました。"])
        # 前回はまだ記録がなかった (file="-") なら、tail に関係なく最初から
        self.assertEqual(len(self.events("claude", file="-", tail=2)["events"]), 6)

        # 続きから: 追記された行だけを返す。書きかけの行 (改行なし) は次回に回す
        size = claude.stat().st_size
        line = json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "追加の応答"}]}}, ensure_ascii=False)
        with open(claude, "a", encoding="utf-8") as f:
            f.write(line + "\n" + line[:10])
        r = self.events("claude", file=str(claude), offset=size)
        self.assertEqual(([e["text"] for e in r["events"]], r["reset"]), (["追加の応答"], False))
        self.assertEqual(r["offset"], size + len((line + "\n").encode("utf-8")))
        # 長い文章は max で切り詰め、その印を付ける (max=0 なら省略しない)
        with open(claude, "a", encoding="utf-8") as f:
            f.write(line[10:].replace("追加の応答", "あ" * 50) + "\n")
        r2 = self.events("claude", file=str(claude), offset=r["offset"], max=10)
        self.assertEqual((r2["events"][0]["text"], r2["events"][0]["truncated"]), ("あ" * 10, True))
        self.assertEqual(self.events("claude", file=str(claude), offset=r["offset"], max=0)["events"][0]["text"], "あ" * 50)
        # エージェントを起動し直して新しい会話になったら、新しい会話の最初から返す
        time.sleep(0.05)
        newer = self.place("claude-2.1.325-transcript.jsonl", claude.parent / "b.jsonl")
        r = self.events("claude", file=str(claude), offset=r2["offset"])
        self.assertEqual((r["file"], r["reset"], len(r["events"])), (str(newer), True, 6))
        self.assertEqual(self.get("/events?agent=claude&offset=x")[0], 400)
        self.assertEqual(self.get("/events?agent=bad")[0], 400)

    def test_codex_result_falls_back_to_transcript(self):
        """notify が呼ばれなかったとき (設定が利用者のものに置き換わっているなど) は、会話記録の最後の応答を返す。"""
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps({"name": "cr", "agent": "codex"})})
        self.wait_ready()
        self.place("codex-0.165.0-rollout.jsonl", self.home / ".codex" / "sessions" / "2026" / "11" / "15" / "rollout-x.jsonl")
        self.assertEqual(self.get("/result?agent=codex")[1]["text"], "完了しました。")

    def test_memory_and_oom_kills(self):
        payload = {"name": "mem", "agent": "none", "notifySecret": "pmva/notify-webhook"}
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps(payload)})
        self.wait_ready()
        _, st = self.get("/status?lines=0")
        mem = st["memory"]
        self.assertGreater(mem["totalMiB"], 0)
        self.assertEqual(mem["usedMiB"], mem["totalMiB"] - mem["availableMiB"])
        self.assertTrue(mem["top"] and all({"name", "count", "rssMiB"} <= set(g) for g in mem["top"]))
        # イメージを作ったときからの回数 (3) は数えず、セッションが始まってからの回数を返す
        self.assertEqual(st["oomKills"], 0)
        self.vmstat.write_text("nr_free_pages 1\noom_kill 5\n", encoding="utf-8")
        self.assertEqual(self.get("/status?lines=0")[1]["oomKills"], 2)
        # 作業判定のループが見つけて、通知を送る
        for _ in range(50):
            if self.webhook.bodies:
                break
            time.sleep(0.1)
        self.assertEqual([b["text"] for b in self.webhook.bodies],
                         ["[mem] メモリ不足でプロセスが強制終了されました (このセッションで 2 回目)。"
                          "MEMORY_MIB を増やすか、同時に動かすテストやブラウザを減らしてください"])
        time.sleep(2)
        self.assertEqual(len(self.webhook.bodies), 1, "同じ回数では何度も通知しない")

    # ---------------------------------------------------------------- リポジトリごとの準備 (.pmva/setup.sh)

    def make_repo(self, setup_script):
        src = Path(self.tmp.name) / "src" / "app"
        (src / ".pmva").mkdir(parents=True)
        (src / ".pmva" / "setup.sh").write_text(setup_script, encoding="utf-8")
        git = ["git", "-C", str(src), "-c", "user.name=t", "-c", "user.email=t@example.com"]
        subprocess.run(["git", "init", "-q", str(src)], check=True, env=self.env)
        subprocess.run(git + ["add", "-A"], check=True, env=self.env)
        subprocess.run(git + ["commit", "-q", "-m", "init"], check=True, env=self.env)
        return str(src)

    def test_setup_script_runs_before_agents(self):
        repo = self.make_repo('echo "pwd=$(pwd)" > "$HOME/setup-ran.txt"\n'
                              'echo "workdir=$PMVA_WORKDIR" >> "$HOME/setup-ran.txt"\n'
                              'echo "path=$PATH" >> "$HOME/setup-ran.txt"\n'
                              'echo "準備の出力"\nsleep 2\n')
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps(
            {"name": "su", "agent": "claude", "repo": repo, "prompt": "テストして"})})
        # 準備の間はエージェントを起動しない
        for _ in range(100):
            _, st = self.get("/status?lines=0")
            if st["bootstrap"] == "setup":
                break
            time.sleep(0.1)
        self.assertEqual(st["bootstrap"], "setup")
        self.assertEqual(st["agents"]["claude"]["state"], "starting")
        self.assertNotIn("claude", [w["name"] for w in st["windows"]])
        st = self.wait_ready()
        self.assertEqual(st["bootstrap"], "ready")
        self.assertIn("claude", [w["name"] for w in st["windows"]])
        ran = (self.home / "setup-ran.txt").read_text(encoding="utf-8")
        workdir = str(self.home / "work" / "app")
        self.assertIn(f"pwd={workdir}\n", ran)
        self.assertIn(f"workdir={workdir}\n", ran, "env.sh を読んでいる")
        self.assertIn(f"path={self.home}/.local/bin:", ran, "pmva-node で入れた node が先に見つかる")
        self.assertIn("準備の出力", (self.home / ".pmva" / "setup.log").read_text(encoding="utf-8"))

    def test_setup_script_failure_still_starts_agents(self):
        repo = self.make_repo("echo 依存関係のエラー >&2\nexit 3\n")
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps({"name": "sf", "agent": "claude", "repo": repo})})
        st = self.wait_ready()
        self.assertEqual(st["bootstrap"], "ready (warnings: setup.sh failed (exit 3). See ~/.pmva/setup.log)")
        self.assertIn("claude", [w["name"] for w in st["windows"]], "失敗してもエージェントは起動する")
        self.assertIn("依存関係のエラー", (self.home / ".pmva" / "setup.log").read_text(encoding="utf-8"))

    def test_setup_script_timeout_and_skip(self):
        repo = self.make_repo('touch "$HOME/setup-started"\nsleep 30\n')
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps(
            {"name": "st", "agent": "none", "repo": repo, "setupTimeoutSeconds": 1})})
        st = self.wait_ready()
        self.assertEqual(st["bootstrap"], "ready (warnings: setup.sh failed (exit 124). See ~/.pmva/setup.log)")
        (self.home / "setup-started").unlink()
        self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps(
            {"name": "st", "agent": "none", "repo": repo, "skipSetup": "true"})})
        st = self.wait_ready()
        self.assertEqual(st["bootstrap"], "ready")
        self.assertFalse((self.home / "setup-started").exists(), "--no-setup なら実行しない")

    # ---------------------------------------------------------------- 手元の設定 (mvm profile push)

    def test_profile_is_applied(self):
        import io
        import tarfile
        with tarfile.open(self.s3_in / "default.tar.gz", "w:gz") as tar:
            for name, data in (("claude/CLAUDE.md", "手元のルール"), ("claude/skills/review/SKILL.md", "新しい review"),
                               ("claude/agents/tester.md", "tester"), ("codex/AGENTS.md", "codex のルール"),
                               ("codex/skills/fix/SKILL.md", "fix")):
                raw = data.encode("utf-8")
                info = tarfile.TarInfo(name)
                info.size = len(raw)
                tar.addfile(info, io.BytesIO(raw))
        # VM の中にすでにあるもの (スナップショットから復元したものなど)
        claude = self.home / ".claude"
        (claude / "skills" / "review").mkdir(parents=True)
        (claude / "skills" / "review" / "old.md").write_text("手元で消したファイル", encoding="utf-8")
        (claude / "skills" / "vm-made").mkdir(parents=True)
        (claude / "skills" / "vm-made" / "SKILL.md").write_text("VM で作った skill", encoding="utf-8")
        st = self.run_session(profile="s3://bkt/profiles/default.tar.gz")
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual((claude / "CLAUDE.md").read_text(encoding="utf-8"), "手元のルール")
        self.assertEqual((claude / "skills" / "review" / "SKILL.md").read_text(encoding="utf-8"), "新しい review")
        self.assertFalse((claude / "skills" / "review" / "old.md").exists(), "skill は丸ごと置き換える")
        self.assertTrue((claude / "skills" / "vm-made" / "SKILL.md").exists(), "VM で作った別の skill は残す")
        self.assertTrue((claude / "agents" / "tester.md").exists())
        self.assertEqual((self.home / ".codex" / "AGENTS.md").read_text(encoding="utf-8"), "codex のルール")
        self.assertTrue((self.home / ".codex" / "skills" / "fix" / "SKILL.md").exists())
        # pmva のフックは、設定を置いた後でも入っている
        hooks = json.loads((claude / "settings.json").read_text(encoding="utf-8"))["hooks"]
        self.assertIn("Stop", hooks)
        # 見つからないときは警告して、そのまま起動する
        st = self.run_session(profile="s3://bkt/profiles/missing.tar.gz")
        self.assertEqual(st["bootstrap"], "ready (warnings: profile could not be applied: s3://bkt/profiles/missing.tar.gz)")

    # ---------------------------------------------------------------- 認証方式ごとの初期化

    def run_session(self, **payload):
        payload = dict({"name": "auth", "agent": "none"}, **payload)
        code, _ = self.hook("run", {"microvmId": "m", "runHookPayload": json.dumps(payload)})
        self.assertEqual(code, 200)
        return self.wait_ready()

    def env_value(self, var):
        """bootstrap が書いた ~/.pmva/env.sh (tmux のシェルが読む) を読み込んだときの値。"""
        script = f'source "$HOME/.pmva/env.sh"; printf %s "${{{var}:-}}"'
        return subprocess.run(["bash", "-c", script], env=self.env, capture_output=True, text=True, check=True).stdout

    def mode(self, path):
        return oct(path.stat().st_mode & 0o777)

    def test_claude_api_key_uses_key_helper(self):
        self.add_secret("pmva/anthropic-api-key", "sk-ant-test-123")
        st = self.run_session(claudeAuth="api-key", claudeSecret="pmva/anthropic-api-key")
        self.assertEqual(st["bootstrap"], "ready")
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        helper = Path(settings["apiKeyHelper"])
        out = subprocess.run([str(helper)], env=self.env, capture_output=True, text=True, check=True).stdout
        self.assertEqual(out.strip(), "sk-ant-test-123")
        self.assertEqual(self.mode(self.home / ".pmva" / "secrets" / "anthropic_api_key"), "0o600")
        self.assertEqual(self.env_value("ANTHROPIC_API_KEY"), "", "キーは環境変数ではなく apiKeyHelper で渡す")

    def test_subscription_token_codex_api_key_and_github(self):
        self.add_secret("pmva/claude-oauth-token", "sk-ant-oat01-test")
        self.add_secret("pmva/openai-api-key", "sk-openai-test")
        self.add_secret("pmva/github-token", "ghp_test")
        st = self.run_session(agent="both", claudeAuth="oauth-token", claudeSecret="pmva/claude-oauth-token",
                              codexAuth="api-key", codexSecret="pmva/openai-api-key",
                              githubSecret="pmva/github-token", gitUserName="山田 太郎", gitUserEmail="t@example.com")
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual(self.env_value("CLAUDE_CODE_OAUTH_TOKEN"), "sk-ant-oat01-test")
        self.assertEqual(self.env_value("OPENAI_API_KEY"), "sk-openai-test")
        self.assertEqual((self.home / ".codex-login-stub").read_text(encoding="utf-8"), "sk-openai-test")
        self.assertEqual(self.env_value("GH_TOKEN"), "ghp_test")
        cred = self.home / ".git-credentials"
        self.assertEqual(cred.read_text(encoding="utf-8"), "https://x-access-token:ghp_test@github.com\n")
        self.assertEqual(self.mode(cred), "0o600")
        self.assertEqual(self.mode(self.home / ".pmva" / "env.sh"), "0o600")
        git = subprocess.run(["git", "config", "--global", "--list"], env=self.env, capture_output=True, text=True).stdout
        self.assertIn("credential.helper=store", git)
        self.assertIn("user.name=山田 太郎", git)
        self.assertIn("push.autosetupremote=true", git)

    def test_token_secrets_drop_whitespace_from_wrapped_paste(self):
        # 端末の折り返しごとコピーして登録したトークン (途中に空白や改行) でも、正しい値として使う
        self.add_secret("pmva/claude-oauth-token", "sk-ant-oat01-ab\ncd ef\r\n")
        self.add_secret("pmva/codex-access-token", "at-ab cd")
        self.add_secret("pmva/github-token", "ghp_ab\ncd")
        st = self.run_session(agent="both", claudeAuth="oauth-token", claudeSecret="pmva/claude-oauth-token",
                              codexAuth="access-token", codexSecret="pmva/codex-access-token",
                              githubSecret="pmva/github-token")
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual(self.env_value("CLAUDE_CODE_OAUTH_TOKEN"), "sk-ant-oat01-abcdef")
        self.assertEqual(self.env_value("CODEX_ACCESS_TOKEN"), "at-abcd")
        self.assertEqual(self.env_value("GH_TOKEN"), "ghp_abcd")
        cred = self.home / ".git-credentials"
        self.assertEqual(cred.read_text(encoding="utf-8"), "https://x-access-token:ghp_abcd@github.com\n")

    def test_codex_chatgpt_auth_json_and_gateway(self):
        auth = json.dumps({"tokens": {"access_token": "chatgpt-test"}})
        self.add_secret("pmva/codex-auth-json", auth)
        self.add_secret("pmva/anthropic-gateway", json.dumps({"baseUrl": "https://gw.example.com", "authToken": "gw-tok"}))
        st = self.run_session(claudeAuth="gateway", claudeSecret="pmva/anthropic-gateway",
                              codexAuth="chatgpt", codexSecret="pmva/codex-auth-json")
        self.assertEqual(st["bootstrap"], "ready")
        auth_file = self.home / ".codex" / "auth.json"
        self.assertEqual(json.loads(auth_file.read_text(encoding="utf-8")), json.loads(auth))
        self.assertEqual(self.mode(auth_file), "0o600")
        self.assertEqual(self.env_value("ANTHROPIC_BASE_URL"), "https://gw.example.com")
        self.assertEqual(self.env_value("ANTHROPIC_AUTH_TOKEN"), "gw-tok")

    def test_bedrock_sets_region_and_model(self):
        st = self.run_session(claudeAuth="bedrock", region="ap-northeast-1", bedrockRegion="us-west-2",
                              claudeModel="us.anthropic.test-model")
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual(self.env_value("CLAUDE_CODE_USE_BEDROCK"), "1")
        self.assertEqual(self.env_value("AWS_REGION"), "us-west-2")
        self.assertEqual(self.env_value("ANTHROPIC_MODEL"), "us.anthropic.test-model")

    def codex_config(self):
        import tomllib   # Python 3.11 以降 (このテストは Linux の CI で動く)
        text = (self.home / ".codex" / "config.toml").read_text(encoding="utf-8")
        return tomllib.loads(text), text

    def test_bedrock_api_keys_for_claude_and_codex(self):
        self.add_secret("pmva/claude-bedrock-api-key", "claude-bedrock-key")
        self.add_secret("pmva/codex-bedrock-api-key", "codex-bedrock-key")
        st = self.run_session(agent="both", region="ap-northeast-1", bedrockRegion="us-east-1",
                              claudeAuth="bedrock-api-key", claudeSecret="pmva/claude-bedrock-api-key",
                              codexAuth="bedrock-api-key", codexSecret="pmva/codex-bedrock-api-key")
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual(self.env_value("CLAUDE_CODE_USE_BEDROCK"), "1")
        self.assertEqual(self.env_value("AWS_BEARER_TOKEN_BEDROCK"), "claude-bedrock-key")
        self.assertEqual(self.env_value("AWS_REGION"), "us-east-1")
        # Codex のキーは環境変数に出さず、ファイルから読ませる (Claude のキーと混ざらない)
        key_file = self.home / ".pmva" / "secrets" / "codex_bedrock_api_key"
        self.assertEqual(key_file.read_text(encoding="utf-8"), "codex-bedrock-key")
        self.assertEqual(self.mode(key_file), "0o600")
        cfg, text = self.codex_config()
        self.assertEqual(cfg["model_provider"], "amazon-bedrock")
        bedrock = cfg["model_providers"]["amazon-bedrock"]
        self.assertEqual(bedrock["base_url"], "https://bedrock-mantle.us-east-1.api.aws/openai/v1")
        self.assertEqual(bedrock["auth"], {"command": "cat", "args": [str(key_file)]})
        self.assertNotIn("aws", bedrock, "Codex は auth と aws を同時に指定できない")
        self.assertTrue(cfg["notify"][0].endswith("notify.sh"))
        self.assertIn("codex-bedrock-key", key_file.read_text(encoding="utf-8"))
        self.assertNotIn("codex-bedrock-key", text)

    def test_mantle_and_codex_bedrock_role_then_switch_back(self):
        st = self.run_session(agent="both", region="ap-northeast-1", claudeAuth="mantle", codexAuth="bedrock")
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual(self.env_value("CLAUDE_CODE_USE_MANTLE"), "1")
        self.assertEqual(self.env_value("CLAUDE_CODE_USE_BEDROCK"), "")
        self.assertEqual(self.env_value("AWS_REGION"), "ap-northeast-1")
        cfg, _ = self.codex_config()
        self.assertEqual(cfg["model_provider"], "amazon-bedrock")
        self.assertEqual(cfg["model_providers"]["amazon-bedrock"], {"aws": {"region": "ap-northeast-1"}})
        # 同じ ~/.codex (スナップショットからの復元と同じ) で、別の認証方式で起動し直すと、接続先の設定は消える
        self.add_secret("pmva/openai-api-key", "sk-openai-test")
        st = self.run_session(agent="codex", codexAuth="api-key", codexSecret="pmva/openai-api-key")
        self.assertEqual(st["bootstrap"], "ready")
        cfg, text = self.codex_config()
        self.assertNotIn("model_provider", cfg)
        self.assertNotIn("model_providers", cfg)
        self.assertEqual(text.count("notify ="), 1)

    def test_codex_bedrock_keeps_users_own_provider(self):
        (self.home / ".codex").mkdir(exist_ok=True)
        (self.home / ".codex" / "config.toml").write_text('model_provider = "my-gateway"\n', encoding="utf-8")
        st = self.run_session(agent="codex", region="ap-northeast-1", codexAuth="bedrock")
        self.assertIn("codex config.toml already sets model_provider", st["bootstrap"])
        cfg, _ = self.codex_config()
        self.assertEqual(cfg["model_provider"], "my-gateway")

    def test_codex_bedrock_rejects_bad_region(self):
        st = self.run_session(agent="codex", region="ap-northeast-1", codexAuth="bedrock", bedrockRegion='x" = 1')
        self.assertIn("invalid Bedrock region", st["bootstrap"])
        cfg, _ = self.codex_config()
        self.assertNotIn("model_provider", cfg)

    def test_codex_access_token(self):
        self.add_secret("pmva/codex-access-token", "at-test-token")
        st = self.run_session(agent="codex", codexAuth="access-token", codexSecret="pmva/codex-access-token")
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual((self.home / ".codex-login-stub").read_text(encoding="utf-8"), "at-test-token")
        self.assertEqual(self.env_value("CODEX_ACCESS_TOKEN"), "at-test-token")

    def test_codex_gateway_with_bearer_key(self):
        self.add_secret("pmva/codex-gateway", json.dumps({"baseUrl": "https://gw.example.com/v1", "apiKey": "gw-key"}))
        st = self.run_session(agent="codex", codexAuth="gateway", codexSecret="pmva/codex-gateway")
        self.assertEqual(st["bootstrap"], "ready")
        key_file = self.home / ".pmva" / "secrets" / "codex_gateway_key"
        self.assertEqual(key_file.read_text(encoding="utf-8"), "gw-key")
        self.assertEqual(self.mode(key_file), "0o600")
        cfg, text = self.codex_config()
        self.assertEqual(cfg["model_provider"], "pmva-gateway")
        self.assertEqual(cfg["model_providers"]["pmva-gateway"], {
            "name": "pmva-gateway", "base_url": "https://gw.example.com/v1", "wire_api": "responses",
            "auth": {"command": "cat", "args": [str(key_file)]}})
        self.assertNotIn("gw-key", text)

    def test_codex_gateway_with_key_header_query_and_headers(self):
        self.add_secret("pmva/codex-gateway", json.dumps({
            "baseUrl": "https://res.openai.azure.com/openai/v1", "apiKey": "azure-key", "apiKeyHeader": "api-key",
            "queryParams": {"api-version": "2025-04-01-preview"}, "headers": {"X-Team": "a \"b\""}}))
        st = self.run_session(agent="codex", codexAuth="gateway", codexSecret="pmva/codex-gateway")
        self.assertEqual(st["bootstrap"], "ready")
        cfg, text = self.codex_config()
        gw = cfg["model_providers"]["pmva-gateway"]
        self.assertEqual(gw["env_http_headers"], {"api-key": "PMVA_CODEX_GATEWAY_KEY"})
        self.assertEqual(gw["query_params"], {"api-version": "2025-04-01-preview"})
        self.assertEqual(gw["http_headers"], {"X-Team": 'a "b"'})
        self.assertNotIn("auth", gw)
        self.assertEqual(self.env_value("PMVA_CODEX_GATEWAY_KEY"), "azure-key")
        self.assertNotIn("azure-key", text)

    def test_codex_gateway_without_base_url_warns(self):
        self.add_secret("pmva/codex-gateway", json.dumps({"apiKey": "k"}))
        st = self.run_session(agent="codex", codexAuth="gateway", codexSecret="pmva/codex-gateway")
        self.assertIn("codex gateway secret has no baseUrl", st["bootstrap"])
        cfg, _ = self.codex_config()
        self.assertNotIn("model_provider", cfg)

    def test_claude_custom_auth(self):
        self.add_secret("pmva/claude-custom-auth", json.dumps({
            "env": {"ANTHROPIC_BASE_URL": "https://gw.example.com", "CLAUDE_CODE_CLIENT_CERT": "{{files}}/client.pem",
                    "ANTHROPIC_CUSTOM_HEADERS": "X-A: 1\nX-B: 'q'"},
            "files": {"client.pem": "-----BEGIN CERTIFICATE-----\nx\n", "key.sh": "#!/bin/sh\necho k\n"},
            "claudeSettings": {"apiKeyHelper": "sh {{files}}/key.sh", "env": {"X_SETTING": "1"}}}))
        st = self.run_session(claudeAuth="custom", claudeSecret="pmva/claude-custom-auth")
        self.assertEqual(st["bootstrap"], "ready")
        files = self.home / ".pmva" / "secrets" / "claude-custom"
        self.assertEqual(self.env_value("ANTHROPIC_BASE_URL"), "https://gw.example.com")
        self.assertEqual(self.env_value("CLAUDE_CODE_CLIENT_CERT"), f"{files}/client.pem")
        self.assertEqual(self.env_value("ANTHROPIC_CUSTOM_HEADERS"), "X-A: 1\nX-B: 'q'")
        self.assertEqual((files / "client.pem").read_text(encoding="utf-8"), "-----BEGIN CERTIFICATE-----\nx\n")
        self.assertEqual(self.mode(files / "key.sh"), "0o600")
        settings = json.loads((self.home / ".claude" / "settings.json").read_text(encoding="utf-8"))
        self.assertEqual(settings["apiKeyHelper"], f"sh {files}/key.sh")
        self.assertEqual(settings["env"], {"X_SETTING": "1"})
        self.assertIn("Stop", settings["hooks"], "pmva のフックは残る")

    def test_codex_custom_auth(self):
        self.add_secret("pmva/codex-custom-auth", json.dumps({
            "env": {"MY_GATEWAY_KEY": "k-123"}, "files": {"token": "tok"},
            "codexConfig": 'model_provider = "corp"\nmodel_providers.corp.name = "corp"\n'
                           'model_providers.corp.base_url = "https://corp.example.com/v1"\n'
                           'model_providers.corp.auth = { command = "cat", args = ["{{files}}/token"] }\n'}))
        st = self.run_session(agent="codex", codexAuth="custom", codexSecret="pmva/codex-custom-auth")
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual(self.env_value("MY_GATEWAY_KEY"), "k-123")
        cfg, text = self.codex_config()
        files = self.home / ".pmva" / "secrets" / "codex-custom"
        self.assertEqual(cfg["model_provider"], "corp")
        self.assertEqual(cfg["model_providers"]["corp"]["auth"]["args"], [f"{files}/token"])
        self.assertTrue(cfg["notify"][0].endswith("notify.sh"))
        self.assertIn("# pmva:provider begin", text)

    def test_codex_custom_auth_keeps_users_own_keys(self):
        (self.home / ".codex").mkdir(exist_ok=True)
        (self.home / ".codex" / "config.toml").write_text('model = "my-model"\n', encoding="utf-8")
        self.add_secret("pmva/codex-custom-auth", json.dumps({"codexConfig": 'model = "corp-model"\nmodel_provider = "corp"\n'}))
        st = self.run_session(agent="codex", codexAuth="custom", codexSecret="pmva/codex-custom-auth")
        self.assertIn("codex config.toml already sets model", st["bootstrap"])
        cfg, _ = self.codex_config()   # 同じキーを 2 回書かない (書くと Codex が設定を読めずに起動しない)
        self.assertEqual(cfg["model"], "my-model")
        self.assertNotIn("model_provider", cfg)

    def test_custom_auth_rejects_bad_names(self):
        self.add_secret("pmva/claude-custom-auth", json.dumps({
            "env": {"PATH": "/evil", "bad-name": "x", "OK_NAME": "1"}, "files": {"../escape": "x", "ok.txt": "1"}}))
        st = self.run_session(claudeAuth="custom", claudeSecret="pmva/claude-custom-auth")
        self.assertIn("custom auth: skipped env PATH", st["bootstrap"])
        self.assertIn("custom auth: skipped env bad-name", st["bootstrap"])
        self.assertIn("custom auth: skipped file ../escape", st["bootstrap"])
        self.assertEqual(self.env_value("OK_NAME"), "1")
        self.assertFalse((self.home / ".pmva" / "secrets" / "escape").exists())
        self.assertTrue((self.home / ".pmva" / "secrets" / "claude-custom" / "ok.txt").exists())

    def test_claude_key_for_bedrock_warns_when_codex_uses_role(self):
        self.add_secret("pmva/claude-bedrock-api-key", "claude-bedrock-key")
        st = self.run_session(agent="both", region="ap-northeast-1", claudeAuth="bedrock-api-key",
                              claudeSecret="pmva/claude-bedrock-api-key", codexAuth="bedrock")
        self.assertIn("Codex also uses the Claude Bedrock API key", st["bootstrap"])

    def test_claude_platform_on_aws(self):
        self.add_secret("pmva/anthropic-aws-api-key", "sk-ant-aws-test")
        st = self.run_session(region="ap-northeast-1", claudeAuth="anthropic-aws-api-key",
                              claudeSecret="pmva/anthropic-aws-api-key",
                              anthropicAwsWorkspaceId="wrkspc_01Test", anthropicAwsRegion="us-east-1")
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual(self.env_value("CLAUDE_CODE_USE_ANTHROPIC_AWS"), "1")
        self.assertEqual(self.env_value("ANTHROPIC_AWS_WORKSPACE_ID"), "wrkspc_01Test")
        self.assertEqual(self.env_value("AWS_REGION"), "us-east-1")
        self.assertEqual(self.env_value("ANTHROPIC_AWS_API_KEY"), "sk-ant-aws-test")
        # キーなし (実行ロールで署名する)
        st = self.run_session(region="ap-northeast-1", claudeAuth="anthropic-aws", anthropicAwsWorkspaceId="wrkspc_01Test")
        self.assertEqual(st["bootstrap"], "ready")
        self.assertEqual(self.env_value("ANTHROPIC_AWS_API_KEY"), "")
        self.assertEqual(self.env_value("AWS_REGION"), "ap-northeast-1")
        st = self.run_session(region="ap-northeast-1", claudeAuth="anthropic-aws")
        self.assertIn("anthropicAwsWorkspaceId", st["bootstrap"])

    def test_missing_secret_fails_bootstrap(self):
        # 登録していないシークレットを指定したら、空のトークンで「準備完了」にせず失敗にする
        for auth, secret in (("oauth-token", "pmva/claude-oauth-token"), ("api-key", "pmva/anthropic-api-key"),
                             ("custom", "pmva/claude-custom-auth")):
            st = self.run_session(claudeAuth=auth, claudeSecret=secret)
            self.assertTrue(st["bootstrap"].startswith("failed"), (auth, st["bootstrap"]))

    def test_snapshot_excludes_credentials(self):
        self.add_secret("pmva/codex-auth-json", '{"tokens": {}}')
        self.add_secret("pmva/github-token", "ghp_test")
        self.run_session(codexAuth="chatgpt", codexSecret="pmva/codex-auth-json", githubSecret="pmva/github-token",
                         snapshotBucket="bkt", region="ap-northeast-1")
        (self.home / "work" / "note.txt").write_text("作業中のファイル", encoding="utf-8")
        (self.home / ".claude" / ".credentials.json").write_text("{}", encoding="utf-8")
        code, r = self.request(self.api_port, "POST", "/snapshot", {})
        self.assertEqual((code, r["ok"]), (200, True), r)
        names = subprocess.run(["tar", "-tzf", str(self.s3_out / "latest.tar.gz")],
                               capture_output=True, text=True, check=True).stdout.split()
        self.assertIn("work/note.txt", names)
        for secret in (".codex/auth.json", ".claude/.credentials.json"):
            self.assertNotIn(secret, names)
        self.assertFalse([n for n in names if n.startswith((".pmva", ".git-credentials"))], names)

    def test_terminate_hook_saves_unless_told_not_to(self):
        """terminate フックはふだん保存する。mvm stop --no-save が /terminate-save で止めたら保存しない。"""
        self.run_session(snapshotBucket="bkt", region="ap-northeast-1")
        code, _ = self.hook("terminate")
        self.assertEqual(code, 200)
        self.assertTrue((self.s3_out / "latest.tar.gz").exists(), "ふだんは終了時に保存する")
        (self.s3_out / "latest.tar.gz").unlink()
        code, r = self.request(self.api_port, "POST", "/terminate-save", {"save": False})
        self.assertEqual((code, r.get("ok")), (200, True), r)
        code, _ = self.hook("terminate")
        self.assertEqual(code, 200)
        self.assertFalse((self.s3_out / "latest.tar.gz").exists(), "--no-save なら終了時に保存しない")
        code, r = self.request(self.api_port, "POST", "/terminate-save", {"save": "no"})
        self.assertEqual(code, 400, "save は真偽値だけ受け付ける")


if __name__ == "__main__":
    unittest.main()
