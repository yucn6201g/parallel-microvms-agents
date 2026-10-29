"""MicroVM 内で動く image/supervisor.py と image/bootstrap.sh の結合テスト (Linux + tmux のみ)。

パスとポートを差し替えたコピーをローカルで動かし、Lambda MicroVMs のフック呼び出しを真似る。
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOOK = "/aws/lambda-microvms/runtime/v1/"


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
        (opt / "supervisor.py").write_text(src, encoding="utf-8")
        for name in ("bootstrap.sh", "snapshot.sh"):
            shutil.copy(ROOT / "image" / name, opt / name)
            (opt / name).chmod(0o755)
        # エージェントのスタブ (--version はすぐ終わり、それ以外は待ち続ける)
        for c in ("claude", "codex"):
            p = bindir / c
            p.write_text(f'#!/bin/sh\n[ "$1" = --version ] && echo "{c} stub" && exit 0\necho "{c} started: $*"\nexec sleep 600\n')
            p.chmod(0o755)
        # tmux 内のシェルが環境の設定で PATH を上書きしても、スタブが先に見つかるようにする
        # (tmux はログインシェルで起動するので .bash_profile も用意する)
        for rc in (".bashrc", ".bash_profile"):
            (home / rc).write_text(f'export PATH="{bindir}:$PATH"\n', encoding="utf-8")
        self.home = home
        self.env = dict(os.environ, HOME=str(home), PATH=f"{bindir}:{os.environ['PATH']}", TMUX_TMPDIR=str(t))
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


if __name__ == "__main__":
    unittest.main()
