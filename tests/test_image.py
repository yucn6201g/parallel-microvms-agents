"""image/Dockerfile で作った本物のイメージを Docker で動かすテスト。

Lambda MicroVMs のベースイメージ (public.ecr.aws/lambda/microvms:al2023-minimal) は公開されているので、
同じ Dockerfile を手元や CI でビルドして中身を確かめられる。MicroVM 特有の部分 (フックの呼ばれ方、
サスペンド、プロキシ) は確かめられないが、ツールの有無、ブラウザ、起動時の初期化、リポジトリの準備は確かめられる。

ビルドしたイメージの名前を指定したときだけ動く (CI の image ジョブが ARM64 でビルドして動かす):
  docker build -t pmva-agent:test image/
  PMVA_TEST_IMAGE=pmva-agent:test python -m unittest tests.test_image -v
"""

import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

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


# VM の中で aws の代わりに動くスクリプト (/opt/pmva-test/bin/aws)。シークレットは secrets/ の
# <名前の / を _ に> から、S3 からの取得は s3/<ファイル名> から読み、S3 への保存は s3-out/ に書く
FAKE_AWS = r"""#!/bin/bash
T=/opt/pmva-test
case "$1 $2" in
  "secretsmanager get-secret-value")
    shift 2; id=""
    while [ $# -gt 0 ]; do [ "$1" = --secret-id ] && id="$2"; shift; done
    f="$T/secrets/${id//\//_}"
    [ -f "$f" ] || { echo "ResourceNotFoundException: $id" >&2; exit 254; }
    cat "$f" ;;
  "s3 cp")
    args=(); for a in "${@:3}"; do [[ "$a" == --* ]] || args+=("$a"); done
    src="${args[0]}"; dst="${args[1]}"
    if [ "$src" = - ]; then cat > "$T/s3-out/$(basename "$dst")"
    elif [[ "$src" == s3://* ]]; then
      if [ "$dst" = - ]; then cat "$T/s3/$(basename "$src")"; else cp "$T/s3/$(basename "$src")" "$dst"; fi
    else cp "$src" "$T/s3-out/$(basename "$dst")"; fi ;;
  *) echo "fake aws: $*" >&2; exit 1 ;;
esac
"""

# VM の中で LLM API の代わりを動かす (tests/mock_llm.py)。要求は /tmp/mock-*.jsonl に記録する
MOCK_LAUNCHER = """import json, sys, time
sys.path.insert(0, "/opt/pmva-test")
from mock_llm import AnthropicMock, OpenAIResponsesMock
claude, codex = AnthropicMock("touch made-by-claude.txt"), OpenAIResponsesMock("touch made-by-codex.txt")
claude.log_path, codex.log_path = "/tmp/mock-claude.jsonl", "/tmp/mock-codex.jsonl"
with open("/tmp/mock-ports.json.tmp", "w") as f:
    json.dump({"claude": claude.server_address[1], "codex": codex.server_address[1]}, f)
import os; os.rename("/tmp/mock-ports.json.tmp", "/tmp/mock-ports.json")
time.sleep(3600)
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

    def test_tmux_does_not_use_alt_screen(self):
        # Windows の端末 (ConPTY) で応答が重複して漏れるのを防ぐ設定が、イメージの tmux に効いていること (tests/test_tmux_conf.py)
        out = self.run_in_image("set -e; tmux new-session -d -s t; tmux show-options -s terminal-overrides; tmux kill-server")
        self.assertIn("*:smcup@:rmcup@", out)

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

    def start_vm(self, *extra):
        """イメージを MicroVM と同じく supervisor.py で起動し、validate フックが通るまで待つ。(名前, ポート) を返す。"""
        name = f"pmva-image-test-{os.getpid()}-{time.monotonic_ns()}"
        host_network = "host" in DOCKER_RUN_EXTRA   # 手元でプロキシを通すとき (ポートはそのまま使う)
        publish = [] if host_network else ["-p", "127.0.0.1::8080", "-p", "127.0.0.1::9000"]
        docker("run", "-d", "--name", name, *DOCKER_RUN_EXTRA, *publish, *extra, IMAGE)
        self.addCleanup(docker, "rm", "-f", name, check=False)
        ports = {8080: "8080", 9000: "9000"}
        if not host_network:
            for inner in (8080, 9000):
                ports[inner] = docker("port", name, str(inner)).stdout.strip().splitlines()[0].rsplit(":", 1)[1]
        for _ in range(60):
            try:
                if self.request(ports[8080], "GET", "/healthz")[0] == 200:
                    break
            except OSError:
                pass
            time.sleep(1)
        # validate フック: claude / codex / tmux / git が動くか (Lambda MicroVMs がイメージを作るときに呼ぶ)
        for _ in range(120):
            code, _ = self.request(ports[9000], "POST", HOOK + "validate", {})
            if code != 503:
                break
            time.sleep(1)
        self.assertEqual(code, 200)
        return name, ports

    @staticmethod
    def request(port, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, json.loads(r.read() or b"{}")
        except urllib.error.HTTPError as e:
            return e.code, {}

    def run_hook(self, name, ports, payload, timeout=600):
        code, _ = self.request(ports[9000], "POST", HOOK + "run", {"microvmId": "m", "runHookPayload": json.dumps(payload)})
        self.assertEqual(code, 200)
        st = {}
        for _ in range(timeout):
            _, st = self.request(ports[8080], "GET", "/status?lines=5")
            if str(st.get("bootstrap", "")).startswith(("ready", "failed")):
                break
            time.sleep(1)
        log = docker("exec", name, "bash", "-c", "cat ~/.pmva/bootstrap.log ~/.pmva/setup.log 2>&1", check=False).stdout
        self.assertEqual(st.get("bootstrap"), "ready", log)
        return st

    def test_bootstrap_runs_setup_and_starts_agent(self):
        """supervisor.py をそのまま動かし、run フックからリポジトリの準備とエージェントの起動までを通す。"""
        name, ports = self.start_vm()
        # clone するリポジトリ (.pmva/setup.sh 付き) を VM の中に作る
        setup = ("set -e\npmva-node install 20\nnode --version > $HOME/setup-node.txt\n"
                 "uv venv -q --python 3.12 .venv && .venv/bin/python --version > $HOME/setup-python.txt\n")
        docker("exec", "-i", name, "bash", "-c",
               "set -e; mkdir -p ~/src/app/.pmva && cd ~/src/app && cat > .pmva/setup.sh && git init -q "
               "&& git -c user.name=t -c user.email=t@example.com add -A "
               "&& git -c user.name=t -c user.email=t@example.com commit -q -m init", input=setup)

        payload = {"name": "img", "agent": "claude", "claudeAuth": "none", "codexAuth": "none",
                   "repo": "/home/agent/src/app", "snapshotIntervalSeconds": 0}
        st = self.run_hook(name, ports, payload)
        self.assertEqual(st["agents"]["claude"]["state"], "ready")
        self.assertIn("claude", [w["name"] for w in st["windows"]])
        self.assertGreater(st["memory"]["totalMiB"], 0)
        out = docker("exec", name, "bash", "-c", "cat ~/setup-node.txt ~/setup-python.txt").stdout
        self.assertRegex(out, r"^v20\.")
        self.assertIn("Python 3.12.", out)

    def test_agents_work_end_to_end_inside_image(self):
        """MicroVM と同じ起動の流れで、イメージに入っている本物の Claude Code / Codex を動かす。

        LLM API の代わり (tests/mock_llm.py) を VM の中で動かし、--claude-auth custom / --codex-auth custom で
        そこにつなぐ。AWS (Secrets Manager / S3) は aws の代わりのスクリプトで答える。確かめること:
          - run フック → シークレットの取得 → 設定のまとまり (mvm profile push) の取り込み → tmux の中で両方のエージェントが動く
          - フックと notify で状態が 完了 になり、mvm result / follow / top が使う API が答える
          - mvm send (/send) で次の指示を送ると、もう一度 作業中 → 完了 になる
        """
        test_dir = Path(tempfile.mkdtemp(prefix="pmva-image-e2e-"))
        self.addCleanup(shutil.rmtree, test_dir, True)
        for d in ("bin", "secrets", "s3", "s3-out"):
            (test_dir / d).mkdir()
        (test_dir / "bin" / "aws").write_text(FAKE_AWS, encoding="utf-8")
        (test_dir / "mock_launcher.py").write_text(MOCK_LAUNCHER, encoding="utf-8")
        shutil.copy(ROOT / "tests" / "mock_llm.py", test_dir / "mock_llm.py")
        # 設定のまとまり: 利用者全体のルール (エージェントが読んだかを LLM への要求で確かめる)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for path, text in (("claude/CLAUDE.md", "PMVA-PROFILE-RULE-CLAUDE\n"), ("codex/AGENTS.md", "PMVA-PROFILE-RULE-CODEX\n")):
                data = text.encode("utf-8")
                info = tarfile.TarInfo(path)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        (test_dir / "s3" / "rules.tar.gz").write_bytes(buf.getvalue())
        # VM の中の agent ユーザー (uid 1000) が読み書きできるようにする
        for p in [test_dir, *test_dir.rglob("*")]:
            p.chmod(0o777 if p.is_dir() else 0o755)

        path = "/opt/pmva-test/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        name, ports = self.start_vm("-v", f"{test_dir}:/opt/pmva-test", "-e", f"PATH={path}")
        docker("exec", "-d", name, "python3", "/opt/pmva-test/mock_launcher.py")
        for _ in range(30):
            p = docker("exec", name, "cat", "/tmp/mock-ports.json", check=False)
            if p.returncode == 0:
                mock = json.loads(p.stdout)
                break
            time.sleep(1)
        else:
            self.fail("LLM API の代わりが起動しない")

        no_proxy = {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}
        secrets = {
            "pmva/claude-custom-auth": {
                "env": {"ANTHROPIC_BASE_URL": f"http://127.0.0.1:{mock['claude']}", **no_proxy},
                "files": {"key.sh": "#!/bin/sh\necho sk-ant-mock\n"},
                "claudeSettings": {"apiKeyHelper": "sh {{files}}/key.sh",
                                   "permissions": {"allow": ["Bash(touch:*)"]}}},
            "pmva/codex-custom-auth": {
                "env": {"MOCK_API_KEY": "x", **no_proxy},
                "codexConfig": 'model_provider = "mock"\nmodel = "gpt-mock"\napproval_policy = "never"\n'
                               'sandbox_mode = "danger-full-access"\nmodel_providers.mock.name = "mock"\n'
                               f'model_providers.mock.base_url = "http://127.0.0.1:{mock["codex"]}/v1"\n'
                               'model_providers.mock.wire_api = "responses"\nmodel_providers.mock.env_key = "MOCK_API_KEY"\n'},
        }
        for secret_name, value in secrets.items():
            f = test_dir / "secrets" / secret_name.replace("/", "_")
            f.write_text(json.dumps(value), encoding="utf-8")
            f.chmod(0o644)

        payload = {"name": "e2e", "agent": "both", "region": "ap-northeast-1",
                   "claudeAuth": "custom", "claudeSecret": "pmva/claude-custom-auth", "claudeArgs": "--model claude-mock",
                   "codexAuth": "custom", "codexSecret": "pmva/codex-custom-auth",
                   "prompt": "テストして", "profile": "s3://bkt/profiles/rules.tar.gz",
                   "snapshotBucket": "bkt", "snapshotIntervalSeconds": 0}
        st = self.run_hook(name, ports, payload, timeout=300)
        self.assertEqual(sorted(w["name"] for w in st["windows"]), ["claude", "codex", "shell"])

        def screen(window):
            return docker("exec", name, "tmux", "capture-pane", "-p", "-t", f"main:{window}", check=False).stdout

        def wait_done(agents, timeout=180, after=0):
            """エージェントがすべて 完了 になるまで待つ。after を渡すと、その時刻より後の 完了 だけを数える。"""
            st = {}
            for _ in range(timeout):
                _, st = self.request(ports[8080], "GET", "/status?lines=30")
                states = [st.get("agents", {}).get(a, {}) for a in agents]
                if all(x.get("state") == "done" and (x.get("since") or 0) > after for x in states):
                    return st
                time.sleep(1)
            self.fail(f"終わらない: {st.get('agents')}\n" + "\n".join(screen(a) for a in agents))

        st = wait_done(("claude", "codex"))
        for agent in ("claude", "codex"):
            self.assertEqual(st["agents"][agent].get("message"), "完了しました。", agent)
            code, r = self.request(ports[8080], "GET", f"/result?agent={agent}")
            self.assertEqual((code, r.get("text")), (200, "完了しました。"), agent)
            code, ev = self.request(ports[8080], "GET", f"/events?agent={agent}&tail=50")
            kinds = [e["kind"] for e in ev.get("events", [])]
            self.assertEqual(code, 200)
            for kind in ("prompt", "tool", "text"):
                self.assertIn(kind, kinds, (agent, ev))
        workdir = st.get("workdir") or "/home/agent/work"
        made = docker("exec", name, "bash", "-c", f"ls {workdir}").stdout.split()
        self.assertIn("made-by-claude.txt", made)
        self.assertIn("made-by-codex.txt", made)
        # 設定のまとまりのルールを、両方のエージェントが読んでいる
        logs = docker("exec", name, "bash", "-c", "cat /tmp/mock-claude.jsonl; echo; cat /tmp/mock-codex.jsonl").stdout
        self.assertIn("PMVA-PROFILE-RULE-CLAUDE", logs)
        self.assertIn("PMVA-PROFILE-RULE-CODEX", logs)
        self.assertGreater(st["memory"]["totalMiB"], 0)
        self.assertTrue(st["memory"].get("top"), "多く使っているプロセスの一覧 (mvm top)")
        self.assertEqual(st.get("oomKills", 0), 0)

        # mvm send: 次の指示を送ると、もう一度 作業中 → 完了 になる
        before = st["agents"]["claude"]["since"]
        code, _ = self.request(ports[8080], "POST", "/send", {"window": "claude", "text": "もう一度", "enter": True})
        self.assertEqual(code, 200)
        _, st = self.request(ports[8080], "GET", "/status?lines=5")
        self.assertEqual(st["agents"]["claude"]["state"], "working", "送った直後は作業中")
        st = wait_done(("claude",), after=before)
        self.assertEqual(st["agents"]["claude"].get("message"), "完了しました。")
        _, ev = self.request(ports[8080], "GET", "/events?agent=claude&tail=50")
        prompts = [e["text"] for e in ev.get("events", []) if e["kind"] == "prompt"]
        self.assertEqual(prompts[-1:], ["もう一度"], screen("claude"))

        # 作業内容の保存 (mvm save / suspend / stop): 作業と設定は入り、認証情報と VM ごとの情報は入らない
        code, r = self.request(ports[8080], "POST", "/snapshot", {})
        self.assertEqual((code, r.get("ok")), (200, True), r)
        latest = test_dir / "s3-out" / "latest.tar.gz"
        # 保存したファイルは VM の agent (uid 1000) の権限 600。テストを動かす利用者 (CI では root ではない) も読めるようにする
        docker("exec", name, "chmod", "644", "/opt/pmva-test/s3-out/latest.tar.gz")
        with tarfile.open(latest) as tar:
            names = tar.getnames()
        for expected in ("work/made-by-claude.txt", "work/made-by-codex.txt", ".codex/config.toml", ".claude/settings.json"):
            self.assertIn(expected, names)
        self.assertFalse([n for n in names if n.startswith((".pmva", ".git-credentials")) or n == ".codex/auth.json"], names)

        # 保存からの復元 (mvm renew / --restore): 別の VM で作業が戻り、接続先の設定は重ならずに書き直される
        shutil.copy(latest, test_dir / "s3" / "latest.tar.gz")
        (test_dir / "s3" / "latest.tar.gz").chmod(0o644)   # 別の VM の agent が読めるように
        docker("rm", "-f", name)   # 前の VM は止める (手元でホストのネットワークを使うときに、ポートが重ならないように)
        name2, ports2 = self.start_vm("-v", f"{test_dir}:/opt/pmva-test", "-e", f"PATH={path}")
        restore = dict(payload, name="e2e2", prompt="", restore="s3://bkt/workspaces/e2e/latest.tar.gz")
        st = self.run_hook(name2, ports2, restore, timeout=300)
        made = docker("exec", name2, "bash", "-c", "ls /home/agent/work").stdout.split()
        self.assertIn("made-by-claude.txt", made)
        cfg = docker("exec", name2, "cat", "/home/agent/.codex/config.toml").stdout
        self.assertEqual(cfg.count("# pmva:provider begin"), 1, cfg)
        self.assertEqual(cfg.count("notify ="), 1, cfg)
        self.assertEqual(cfg.count('model_provider = "mock"'), 1, cfg)
        settings = json.loads(docker("exec", name2, "cat", "/home/agent/.claude/settings.json").stdout)
        self.assertEqual(len(settings["hooks"]["Stop"]), 1, "pmva のフックは重ならない")
        self.assertEqual(st["agents"]["claude"]["state"], "ready")


if __name__ == "__main__":
    unittest.main()
