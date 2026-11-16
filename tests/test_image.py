"""image/Dockerfile で作った本物のイメージを Docker で動かすテスト。

Lambda MicroVMs のベースイメージ (public.ecr.aws/lambda/microvms:al2023-minimal) は公開されているので、
同じ Dockerfile を手元や CI でビルドして中身を確かめられる。MicroVM 特有の部分 (フックの呼ばれ方、
サスペンド、プロキシ) は確かめられないが、ツールの有無、ブラウザ、起動時の初期化、リポジトリの準備は確かめられる。

ビルドしたイメージの名前を指定したときだけ動く (CI の image ジョブが ARM64 でビルドして動かす):
  docker build -t pmva-agent:test image/
  PMVA_TEST_IMAGE=pmva-agent:test python -m unittest tests.test_image -v
"""

import json
import os
import shutil
import subprocess
import time
import unittest
import urllib.error
import urllib.request

IMAGE = os.environ.get("PMVA_TEST_IMAGE", "")
PLAYWRIGHT_VERSION = "1.64.1"
HOOK = "/aws/lambda-microvms/runtime/v1/"
# 手元のテストでプロキシを通すときだけ使う (CI では空)
DOCKER_RUN_EXTRA = os.environ.get("PMVA_TEST_DOCKER_ARGS", "").split()

SCREENSHOT_JS = r"""
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage();
  await page.setContent('<p id="t" style="font: 32px sans-serif">日本語の表示テスト</p>');
  const cjk = await page.evaluate(async () => { await document.fonts.ready; return document.fonts.check('32px "Noto Sans CJK JP"'); });
  const width = await page.evaluate(() => document.getElementById('t').getBoundingClientRect().width);
  const png = await page.screenshot();
  console.log(JSON.stringify({ version: browser.version(), cjk, width, png: png.length }));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
"""


def docker(*args, check=True, timeout=900, input=None):
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, input=input)
    if check and p.returncode != 0:
        raise AssertionError(f"docker {' '.join(args[:3])} failed ({p.returncode}):\n{p.stdout[-3000:]}\n{p.stderr[-3000:]}")
    return p


@unittest.skipUnless(IMAGE and shutil.which("docker"), "PMVA_TEST_IMAGE (ビルドしたイメージ) と docker が必要")
class ImageTest(unittest.TestCase):
    def run_in_image(self, script, timeout=900):
        return docker("run", "--rm", *DOCKER_RUN_EXTRA, IMAGE, "bash", "-lc", script, timeout=timeout).stdout

    def test_tools_are_installed(self):
        out = self.run_in_image("set -e; id -un; claude --version; codex --version; gh --version | head -1; uv --version; "
                                "node --version; websocat --version; tmux -V; jq --version; command -v pmva-node agent-attach")
        for expected in ("agent", "(Claude Code)", "codex-cli", "gh version", "uv ", "v22.", "websocat", "tmux", "jq-",
                         "/usr/local/bin/pmva-node", "/usr/local/bin/agent-attach"):
            self.assertIn(expected, out)

    def test_headless_chromium_with_japanese_fonts(self):
        script = (f"set -e; mkdir -p /tmp/pw && cd /tmp/pw && npm init -y >/dev/null "
                  f"&& npm install --no-audit --no-fund --silent playwright@{PLAYWRIGHT_VERSION} "
                  f"&& npx playwright install chromium >/dev/null && cat > shot.js <<'JS'\n{SCREENSHOT_JS}\nJS\nnode shot.js")
        out = self.run_in_image(script, timeout=1200)
        result = json.loads(out.strip().splitlines()[-1])
        self.assertTrue(result["cjk"], f"日本語フォント (Noto Sans CJK) が使えない: {result}")
        self.assertGreater(result["width"], 100)
        self.assertGreater(result["png"], 1000)

    def test_python_and_node_versions(self):
        out = self.run_in_image("set -e; cd /tmp; uv python install 3.12 >/dev/null 2>&1; "
                                "uv run --no-project --python 3.12 python -c 'import sys; print(\"py\", sys.version.split()[0])'; "
                                "pmva-node install 20 >/dev/null; hash -r; node --version")
        self.assertIn("py 3.12.", out)
        self.assertRegex(out, r"\nv20\.\d+\.\d+")

    def test_bootstrap_runs_setup_and_starts_agent(self):
        """supervisor.py をそのまま動かし、run フックからリポジトリの準備とエージェントの起動までを通す。"""
        name = f"pmva-image-test-{os.getpid()}"
        host_network = "host" in DOCKER_RUN_EXTRA   # 手元でプロキシを通すとき (ポートはそのまま使う)
        publish = [] if host_network else ["-p", "127.0.0.1::8080", "-p", "127.0.0.1::9000"]
        docker("run", "-d", "--name", name, *DOCKER_RUN_EXTRA, *publish, IMAGE)
        self.addCleanup(docker, "rm", "-f", name, check=False)
        ports = {8080: "8080", 9000: "9000"}
        if not host_network:
            for inner in (8080, 9000):
                ports[inner] = docker("port", name, str(inner)).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        # clone するリポジトリ (.pmva/setup.sh 付き) を VM の中に作る
        setup = ("set -e\npmva-node install 20\nnode --version > $HOME/setup-node.txt\n"
                 "uv venv -q --python 3.12 .venv && .venv/bin/python --version > $HOME/setup-python.txt\n")
        docker("exec", "-i", name, "bash", "-c",
               "set -e; mkdir -p ~/src/app/.pmva && cd ~/src/app && cat > .pmva/setup.sh && git init -q "
               "&& git -c user.name=t -c user.email=t@example.com add -A "
               "&& git -c user.name=t -c user.email=t@example.com commit -q -m init", input=setup)

        def request(port, method, path, body=None):
            data = json.dumps(body).encode() if body is not None else None
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    return r.status, json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                return e.code, {}

        for _ in range(60):
            try:
                if request(ports[8080], "GET", "/healthz")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        # validate フック: claude / codex / tmux / git が動くか (Lambda MicroVMs がイメージを作るときに呼ぶ)
        for _ in range(120):
            code, _ = request(ports[9000], "POST", HOOK + "validate", {})
            if code != 503:
                break
            time.sleep(1)
        self.assertEqual(code, 200)
        payload = {"name": "img", "agent": "claude", "claudeAuth": "none", "codexAuth": "none",
                   "repo": "/home/agent/src/app", "snapshotIntervalSeconds": 0}
        code, _ = request(ports[9000], "POST", HOOK + "run", {"microvmId": "m", "runHookPayload": json.dumps(payload)})
        self.assertEqual(code, 200)
        st = {}
        for _ in range(600):
            _, st = request(ports[8080], "GET", "/status?lines=5")
            if str(st.get("bootstrap", "")).startswith(("ready", "failed")):
                break
            time.sleep(1)
        log = docker("exec", name, "bash", "-c", "cat ~/.pmva/bootstrap.log ~/.pmva/setup.log 2>&1", check=False).stdout
        self.assertEqual(st.get("bootstrap"), "ready", log)
        self.assertEqual(st["agents"]["claude"]["state"], "ready")
        self.assertIn("claude", [w["name"] for w in st["windows"]])
        self.assertGreater(st["memory"]["totalMiB"], 0)
        out = docker("exec", name, "bash", "-c", "cat ~/setup-node.txt ~/setup-python.txt").stdout
        self.assertRegex(out, r"^v20\.")
        self.assertIn("Python 3.12.", out)


if __name__ == "__main__":
    unittest.main()
