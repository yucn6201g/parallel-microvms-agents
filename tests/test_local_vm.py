"""mvm のコマンドを、手元の Docker で動かす本物のイメージを相手に、通しで動かすテスト。

Lambda MicroVMs の代わりは tests/local_microvms.py (aws_stub.py が Docker でコンテナを起動し、FakeIngress が
受け口の代わりをする)。AWS は使わない。MicroVM と同じ経路 (トークン付きの HTTP と WebSocket、
SSH over WebSocket ブリッジ) で、mvm up / wait / result / follow / peek / top / send / ssh / forward / login /
save / suspend / resume / stop と、保存からの復元を確かめる。

エージェントは、イメージに入っている本物の Claude Code / Codex を、VM の中で動かす LLM API の代わり
(tests/mock_llm.py) に --claude-auth custom / --codex-auth custom でつなぐ。

ビルドしたイメージの名前を指定したときだけ動く (CI の image ワークフロー):
  PMVA_TEST_IMAGE=pmva-agent:test python -m unittest tests.test_local_vm -v
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

sys.path.insert(0, str(Path(__file__).resolve().parent))
from local_microvms import FakeIngress  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("PMVA_TEST_IMAGE", "")

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@unittest.skipUnless(IMAGE and shutil.which("docker") and shutil.which("ssh") and shutil.which("ssh-keygen")
                     and os.name == "posix", "PMVA_TEST_IMAGE (ビルドしたイメージ)、docker、ssh が必要")
class LocalVmTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp(prefix="pmva-local-vm-"))
        self.addCleanup(self.cleanup)
        self.stub = self.base / "stub"
        bindir, self.home = self.base / "bin", self.base / "home"
        for d in (self.stub, bindir, self.home / ".ssh"):
            d.mkdir(parents=True)
        self.base.chmod(0o755)
        # aws は AWS のスタブ。ssh は、テスト用の鍵だけを使う (ssh は HOME ではなく passwd のホームを見るため)
        key = self.home / ".ssh" / "id_ed25519"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
        (bindir / "aws").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{ROOT / "tests" / "aws_stub.py"}" "$@"\n',
                                    encoding="utf-8")
        (bindir / "ssh").write_text(f'#!/bin/sh\nexec "{shutil.which("ssh")}" -F /dev/null -o IdentitiesOnly=yes '
                                    f'-i "{key}" "$@"\n', encoding="utf-8")
        for f in ("aws", "ssh"):
            (bindir / f).chmod(0o755)
        cfg = self.base / "config.env"
        cfg.write_text(
            "AWS_REGION=ap-northeast-1\nUSE_GITHUB_TOKEN=false\nSNAPSHOT_INTERVAL_SECONDS=0\n"
            f"SSH_PUBLIC_KEY_FILE={key}.pub\nDEFAULT_AGENT=both\nDEFAULT_CLAUDE_AUTH=custom\nDEFAULT_CODEX_AUTH=custom\n"
            'DEFAULT_CLAUDE_ARGS="--model claude-mock"\n', encoding="utf-8")
        self.ingress = FakeIngress(self.stub)
        self.addCleanup(self.ingress.close)
        self.env = dict(os.environ)
        for k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            self.env.pop(k, None)
        self.env.update({
            "PATH": f"{bindir}:{os.environ.get('PATH', '')}", "HOME": str(self.home), "PYTHONIOENCODING": "utf-8",
            "PMVA_CONFIG": str(cfg), "PMVA_STATE_DIR": str(self.base / "state"), "PMVA_STUB_DIR": str(self.stub),
            "PMVA_STUB_DOCKER_IMAGE": IMAGE, "PMVA_STUB_QUOTA_GB": "100",
            "PMVA_API_BASE_URL": f"http://127.0.0.1:{self.ingress.port}",
            "PMVA_WS_BASE_URL": f"ws://127.0.0.1:{self.ingress.port}",
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        })
        self.setup_mock_llm()

    def cleanup(self):
        for f in self.stub.glob("vm-*.json"):
            subprocess.run(["docker", "rm", "-f", json.loads(f.read_text())["container"]], capture_output=True)
        # VM の中の agent (uid 1000) が作ったファイルも消せるよう、コンテナの root で消す
        subprocess.run(["docker", "run", "--rm", "--user", "root", "-v", f"{self.base}:/d", IMAGE,
                        "bash", "-c", "rm -rf /d/stub"], capture_output=True)
        shutil.rmtree(self.base, ignore_errors=True)

    def setup_mock_llm(self):
        """VM の中で LLM API の代わりを動かし (起動時の準備 prerun.sh)、custom 認証でそこにつなぐ。"""
        shutil.copy(ROOT / "tests" / "mock_llm.py", self.stub / "mock_llm.py")
        ports = {"claude": free_port(), "codex": free_port()}
        (self.stub / "mock_server.py").write_text(
            'import sys, time\nsys.path.insert(0, "/opt/pmva-test")\nimport mock_llm\n'
            f'claude = mock_llm.AnthropicMock("touch made-by-claude.txt", port={ports["claude"]})\n'
            f'codex = mock_llm.OpenAIResponsesMock("touch made-by-codex.txt", port={ports["codex"]})\n'
            'open("/opt/pmva-test/prerun.ready", "w").close()\ntime.sleep(36000)\n', encoding="utf-8")
        (self.stub / "prerun.sh").write_text(
            "rm -f /opt/pmva-test/prerun.ready\nexec python3 /opt/pmva-test/mock_server.py > /tmp/mock.log 2>&1\n",
            encoding="utf-8")
        no_proxy = {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        self.mvm("secret", "set", "claude-custom-auth", input=json.dumps({
            "env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{ports['claude']}", **no_proxy},
            "files": {"key.sh": "#!/bin/sh\necho sk-ant-mock\n"},
            "claudeSettings": {"apiKeyHelper": "sh {{files}}/key.sh", "permissions": {"allow": ["Bash(touch:*)"]}}}))
        self.mvm("secret", "set", "codex-custom-auth", input=json.dumps({
            "env": {"MOCK_API_KEY": "x", **no_proxy},
            "codexConfig": 'model_provider = "mock"\nmodel = "gpt-mock"\napproval_policy = "never"\n'
                           'sandbox_mode = "danger-full-access"\nmodel_providers.mock.name = "mock"\n'
                           f'model_providers.mock.base_url = "http://127.0.0.1:{ports["codex"]}/v1"\n'
                           'model_providers.mock.wire_api = "responses"\nmodel_providers.mock.env_key = "MOCK_API_KEY"\n'}))

    def mvm(self, *args, input=None, check=True, timeout=600):
        p = subprocess.run([sys.executable, str(ROOT / "bin" / "mvm.py"), *args], input=input, capture_output=True,
                           text=True, env=self.env, timeout=timeout)
        if check and p.returncode != 0:
            self.fail(f"mvm {' '.join(args)} failed ({p.returncode}):\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}")
        return p

    def vm_shell(self, name, command):
        return self.mvm("ssh", name, "--no-reconnect", "--", command).stdout

    def test_mvm_commands_end_to_end(self):
        # 起動して、両方のエージェントが最初の指示を終えるまで待つ
        self.mvm("up", "v1", "--prompt", "テストして")
        p = self.mvm("wait", "v1", "--timeout", "240", "--interval", "2", "--json")
        waited = json.dumps(json.loads(p.stdout), ensure_ascii=False)
        self.assertIn('"done"', waited, p.stdout)
        p = self.mvm("result", "v1", "--json")
        results = json.loads(p.stdout)
        texts = sorted(r.get("text") for r in (results if isinstance(results, list) else [results]))
        self.assertEqual(texts, ["完了しました。", "完了しました。"], p.stdout)
        out = self.mvm("follow", "v1", "--once", "--agent", "claude").stdout
        self.assertIn("touch made-by-claude.txt", out)
        peek = json.loads(self.mvm("peek", "v1", "--json").stdout)
        self.assertEqual(peek["agents"]["claude"]["state"], "done")
        self.assertIn("MiB", self.mvm("top", "v1").stdout + self.mvm("top", "v1").stderr)
        self.assertIn("v1", self.mvm("ls").stdout)

        # mvm send: 次の指示を送り、もう一度終わるまで待つ
        self.mvm("send", "v1", "--window", "claude", "もう一度")
        self.mvm("wait", "v1", "--timeout", "120", "--interval", "2")
        out = self.mvm("follow", "v1", "--once", "--agent", "claude").stdout
        self.assertIn("もう一度", out)

        # mvm ssh (SSH over WebSocket ブリッジ): VM の中でコマンドを動かす
        listing = self.vm_shell("v1", "ls ~/work")
        self.assertIn("made-by-claude.txt", listing)
        self.assertIn("made-by-codex.txt", listing)

        # mvm forward: VM の中のサーバーを手元のポートにつなぐ
        self.vm_shell("v1", "cd ~/work && (setsid python3 -m http.server 8765 >/dev/null 2>&1 < /dev/null &)")
        local = free_port()
        fwd = subprocess.Popen([sys.executable, str(ROOT / "bin" / "mvm.py"), "forward", "v1", f"{local}:8765",
                                "--no-reconnect"], env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(fwd.kill)
        body = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{local}/", timeout=5) as r:
                    body = r.read().decode("utf-8", "replace")
                    break
            except OSError:
                time.sleep(0.5)
        fwd.terminate()
        self.assertIsNotNone(body, "forward でつないだポートに届かない")
        self.assertIn("made-by-claude.txt", body)

        # mvm save → suspend → resume (サスペンド中は受け口が断る)
        out = self.mvm("save", "v1").stdout
        self.assertIn("saved", out)
        self.assertTrue(list((self.stub / "s3").rglob("workspaces/v1/latest.tar.gz")))
        self.mvm("suspend", "v1")
        self.assertIn("SUSPENDED", self.mvm("ls").stdout)
        self.mvm("resume", "v1")
        self.assertEqual(json.loads(self.mvm("peek", "v1", "--json").stdout)["name"], "v1")

        # mvm stop (保存して終了) → --restore latest で別の VM に復元する
        self.mvm("stop", "v1")
        self.assertIn("TERMINATED", self.mvm("ls", "--all").stdout)
        self.mvm("up", "v1", "--restore", "latest")
        self.mvm("wait", "v1", "--timeout", "120", "--interval", "2")
        self.assertIn("made-by-claude.txt", self.vm_shell("v1", "ls ~/work"))
        self.mvm("stop", "v1", "--no-save")
        self.assertEqual(self.ingress.rejected and [r for r in self.ingress.rejected if r[1] is None], [],
                         "トークンなしの要求はしない")

    def test_login_reaches_codex_in_vm(self):
        """mvm login: VM の中の codex login の戻り先 (127.0.0.1:1455) に、手元の 1455 番から届く。"""
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", 1455))
            except OSError:
                self.skipTest("この PC では 1455 番が使われている")
        self.mvm("up", "l1", "--agent", "codex", "--codex-auth", "interactive")
        proc = subprocess.Popen([sys.executable, str(ROOT / "bin" / "mvm.py"), "login", "l1", "--no-browser"],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.kill)
        url = None
        deadline = time.time() + 120
        while time.time() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            if "https://auth.openai.com/" in line:
                url = line.strip()
                break
        self.assertIsNotNone(url, "codex login の URL が表示されない")
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A1455", url)
        code = None
        for _ in range(30):
            try:
                urllib.request.urlopen("http://127.0.0.1:1455/auth/callback?code=x&state=wrong", timeout=10)
            except urllib.error.HTTPError as e:
                code = e.code
                break
            except OSError:
                time.sleep(0.5)
        self.assertEqual(code, 400, "手元の 1455 番から VM の codex login に届き、正しくない state を断る")
        proc.terminate()
        self.mvm("stop", "l1", "--no-save")


if __name__ == "__main__":
    unittest.main()
