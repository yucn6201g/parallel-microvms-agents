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
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
        # テストでは作業判定と状態の書き出しを 1 秒ごとにする
        src = src.replace("ACTIVITY_INTERVAL = 30", "ACTIVITY_INTERVAL = 1").replace("PUBLISH_INTERVAL = 60", "PUBLISH_INTERVAL = 1")
        (opt / "supervisor.py").write_text(src, encoding="utf-8")
        for name in ("bootstrap.sh", "snapshot.sh", "notify.sh"):
            shutil.copy(ROOT / "image" / name, opt / name)
            (opt / name).chmod(0o755)
        # エージェントのスタブ (--version はすぐ終わり、それ以外は待ち続ける)
        for c in ("claude", "codex"):
            p = bindir / c
            p.write_text(f'#!/bin/sh\n[ "$1" = --version ] && echo "{c} stub" && exit 0\necho "{c} started: $*"\nexec sleep 600\n')
            p.chmod(0o755)
        # aws のスタブ: s3 cp の内容を保存し、シークレットは Webhook の URL を返す
        self.s3_out = t / "s3"
        self.s3_out.mkdir()
        aws = bindir / "aws"
        aws.write_text('#!/bin/sh\n'
                       'case "$1 $2" in\n'
                       '  "s3 cp") cat > "$STUB_S3_OUT/$(basename "$5")" ;;\n'
                       '  "secretsmanager get-secret-value") echo "$STUB_WEBHOOK" ;;\n'
                       'esac\n')
        aws.chmod(0o755)
        self.webhook = WebhookServer()
        # tmux 内のシェルが環境の設定で PATH を上書きしても、スタブが先に見つかるようにする
        # (tmux はログインシェルで起動するので .bash_profile も用意する)
        for rc in (".bashrc", ".bash_profile"):
            (home / rc).write_text(f'export PATH="{bindir}:$PATH"\n', encoding="utf-8")
        self.home = home
        self.env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}", TMUX_TMPDIR=str(t),
                        STUB_S3_OUT=str(self.s3_out), STUB_WEBHOOK=self.webhook.url)
        self.opt = opt
        self.env.pop("TMUX", None)
        self.proc = subprocess.Popen([sys.executable, str(opt / "supervisor.py")], env=self.env,
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(50):
            try:
                self.get("/healthz")
                break
            except OSError:
                time.sleep(0.1)

    def tearDown(self):
        self.webhook.shutdown()
        subprocess.run(["tmux", "kill-server"], env=self.env, capture_output=True)
        self.proc.terminate()
        self.proc.wait(10)
        self.tmp.cleanup()

    def request(self, port, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

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
        texts = [b["text"] for b in self.webhook.bodies]
        self.assertEqual(texts, ["[nt] Claude Code の応答が終わりました",
                                 "[nt] Claude Code: Claude needs your permission to use Bash",
                                 "[nt] Codex の応答が終わりました: テスト完了"])


if __name__ == "__main__":
    unittest.main()
