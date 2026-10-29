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
            p.write_text(f'#!/bin/sh\n[ "$1" = --version ] && echo "{c} stub" && exit 0\n'
                         # codex login --with-api-key は標準入力のキーを記録して終わる
                         f'[ "$1" = login ] && cat > "$HOME/.{c}-login-stub" && exit 0\n'
                         f'echo "{c} started: $*"\nexec sleep 600\n')
            p.chmod(0o755)
        # aws のスタブ: s3 cp の内容を保存し、シークレットは STUB_SECRETS/<名前の / を _ に> の中身を返す
        self.s3_out = t / "s3"
        self.s3_out.mkdir()
        self.secrets = t / "secrets"
        self.secrets.mkdir()
        aws = bindir / "aws"
        aws.write_text('#!/bin/sh\n'
                       'case "$1 $2" in\n'
                       '  "s3 cp") if [ "$4" = - ]; then cat > "$STUB_S3_OUT/$(basename "$5")"; '
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
                        STUB_S3_OUT=str(self.s3_out), STUB_SECRETS=str(self.secrets),
                        HISTFILE="")
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
        texts = [b["text"] for b in self.webhook.bodies]
        self.assertEqual(texts, ["[nt] Claude Code の応答が終わりました",
                                 "[nt] Claude Code: Claude needs your permission to use Bash",
                                 "[nt] Codex の応答が終わりました: テスト完了"])

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

    def test_missing_secret_fails_bootstrap(self):
        # 登録していないシークレットを指定したら、空のトークンで「準備完了」にせず失敗にする
        for auth, secret in (("oauth-token", "pmva/claude-oauth-token"), ("api-key", "pmva/anthropic-api-key")):
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


if __name__ == "__main__":
    unittest.main()
