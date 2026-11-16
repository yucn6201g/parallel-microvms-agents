"""本物の Claude Code / Codex を、LLM API の代わり (tests/mock_llm.py) につないで動かすテスト。

MicroVM の中と同じ設定 (フック、notify) で、次を確かめる:
  - エージェントのフックから image/notify.sh が呼ばれ、状態 (作業中・完了・入力待ち) と最後の応答が記録される
  - エージェントが書く会話記録を、supervisor.py が読める (mvm follow / result)

エージェントの実行ファイルを環境変数で指定したときだけ動く (CI の agents-e2e ジョブが、image/Dockerfile で固定した版を入れて動かす):
  PMVA_E2E_CLAUDE=/path/to/claude  PMVA_E2E_CODEX=/path/to/codex  python -m unittest tests.test_agents_e2e -v
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mock_llm import AnthropicMock, OpenAIResponsesMock  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
NOTIFY = ROOT / "image" / "notify.sh"
MVM = ROOT / "bin" / "mvm.py"
CLAUDE = os.environ.get("PMVA_E2E_CLAUDE", "")
CODEX = os.environ.get("PMVA_E2E_CODEX", "")


def mvm_constant(name):
    spec = importlib.util.spec_from_file_location("mvm_for_e2e", MVM)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, name)


MVM_LOGIN_URL = mvm_constant("LOGIN_URL")   # mvm login が手元のブラウザで開く URL の形


def load_supervisor():
    spec = importlib.util.spec_from_file_location("pmva_supervisor", ROOT / "image" / "supervisor.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(sys.platform.startswith("linux") and shutil.which("jq") and (CLAUDE or CODEX),
                     "Linux と jq、PMVA_E2E_CLAUDE / PMVA_E2E_CODEX が必要")
class AgentE2ETest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # 一時フォルダは、各テストが addCleanup で止めるエージェント (tmux など) より後に消す
        # (addCleanup は登録の逆順で、tearDown より後に動く)
        self.addCleanup(self.remove_tmp)
        t = Path(self.tmp.name)
        self.home, self.work = t / "home", t / "work"
        for d in (self.home / ".claude", self.home / ".codex", self.home / ".pmva", self.work):
            d.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.work)], check=True)
        self.sup = load_supervisor()

    def remove_tmp(self):
        # 止めた直後のエージェントが終了処理で ~/.claude などに書き込むことがあるので、少し待ってやり直す
        for _ in range(10):
            try:
                self.tmp.cleanup()
                return
            except OSError:
                time.sleep(0.5)
        self.tmp.cleanup()

    def agent_env(self, **extra):
        """手元のエージェントの設定 (このテストを動かしているセッションの環境変数など) を持ち込まない。"""
        env = {"HOME": str(self.home), "PATH": os.environ.get("PATH", ""), "LANG": "C.UTF-8", "TERM": "xterm-256color",
               "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
               "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
        env.update(extra)
        return env

    def state(self, agent):
        try:
            return json.loads((self.home / ".pmva" / "agents" / f"{agent}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def wait_state(self, agent, states, timeout=60):
        deadline = time.time() + timeout
        while time.time() < deadline:
            st = self.state(agent)
            if st.get("state") in states:
                return st
            time.sleep(0.3)
        self.fail(f"{agent} did not reach {states}: {self.state(agent)}")

    def events(self, agent, transcript):
        lines = Path(transcript).read_text(encoding="utf-8").splitlines()
        return [(e["kind"], e.get("name"), e["text"].rstrip("\n")) for e in self.sup.parse_events(agent, lines, 2000)]

    # ---------------------------------------------------------------- Claude Code

    def claude_settings(self, with_notification=False):
        mock = AnthropicMock("touch made-by-claude.txt")
        self.addCleanup(mock.shutdown)
        helper = self.home / "key-helper.sh"
        helper.write_text("#!/bin/sh\necho sk-ant-mock\n", encoding="utf-8")
        helper.chmod(0o755)
        hooks = {"UserPromptSubmit": "claude-prompt", "PostToolUse": "claude-tool", "Stop": "claude-stop"}
        if with_notification:
            hooks["Notification"] = "claude-notification"
        # bootstrap.sh と同じ形のフックと、API キーの渡し方 (apiKeyHelper)
        settings = {"apiKeyHelper": str(helper),
                    "hooks": {ev: [{"hooks": [{"type": "command", "command": f"{NOTIFY} {kind}"}]}] for ev, kind in hooks.items()}}
        (self.home / ".claude" / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
        (self.home / ".claude.json").write_text(json.dumps(
            {"hasCompletedOnboarding": True, "projects": {str(self.work): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
        return mock

    @unittest.skipUnless(CLAUDE, "PMVA_E2E_CLAUDE が必要")
    def test_claude_hooks_record_state_and_transcript(self):
        mock = self.claude_settings()
        # mvm profile push で置く、利用者全体のルール
        (self.home / ".claude" / "CLAUDE.md").write_text("PMVA-PROFILE-RULE: テストのルール\n", encoding="utf-8")
        p = subprocess.run([CLAUDE, "-p", "テストして", "--model", "claude-mock", "--allowedTools", "Bash"],
                           cwd=self.work, env=self.agent_env(ANTHROPIC_BASE_URL=mock.url), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.work / "made-by-claude.txt").exists(), "ツールが実行された")
        st = self.wait_state("claude", ("done",))
        self.assertEqual(st["message"], "完了しました。", "Stop フックの last_assistant_message")
        self.assertIn("PMVA-PROFILE-RULE", json.dumps([b for _, b in mock.requests], ensure_ascii=False),
                      "~/.claude/CLAUDE.md を読んでいる")
        transcript = st["transcript"]
        self.assertTrue(transcript.endswith(".jsonl") and os.path.isfile(transcript))
        self.assertEqual(self.sup.last_claude_text(transcript), "完了しました。", "会話記録から取り出す経路")
        self.assertEqual(self.events("claude", transcript), [
            ("prompt", None, "テストして"), ("thinking", None, "まずコマンドを実行する"), ("text", None, "確認します。"),
            ("tool", "Bash", "touch made-by-claude.txt"), ("output", None, "(Bash completed with no output)"),
            ("text", None, "完了しました。")])

    @unittest.skipUnless(CLAUDE and shutil.which("tmux"), "PMVA_E2E_CLAUDE と tmux が必要")
    def test_claude_permission_prompt_sets_waiting(self):
        """MicroVM と同じく tmux の中で対話モードで動かし、許可待ち → 許可 → 完了 と状態が変わること。"""
        mock = self.claude_settings(with_notification=True)
        sock = str(Path(self.tmp.name) / "t.sock")
        env = self.agent_env(ANTHROPIC_BASE_URL=mock.url)
        inner = " ".join(f"{k}={json.dumps(v)}" for k, v in env.items())
        cmd = f"cd {json.dumps(str(self.work))} && env -i {inner} {json.dumps(CLAUDE)} --model claude-mock " \
              f"--permission-mode default テストして; sleep 60"
        subprocess.run(["tmux", "-S", sock, "new-session", "-d", "-x", "160", "-y", "45", cmd], check=True)
        self.addCleanup(subprocess.run, ["tmux", "-S", sock, "kill-server"], capture_output=True)
        st = self.wait_state("claude", ("waiting", "done"), timeout=90)
        screen = subprocess.run(["tmux", "-S", sock, "capture-pane", "-p"], capture_output=True, text=True).stdout
        self.assertEqual(st["state"], "waiting", screen)
        self.assertIn("permission", st["message"])
        self.assertFalse((self.work / "made-by-claude.txt").exists())
        subprocess.run(["tmux", "-S", sock, "send-keys", "Enter"], check=True)   # 既定の選択肢 (Yes) で許可する
        st = self.wait_state("claude", ("done",))
        self.assertEqual(st["message"], "完了しました。")
        self.assertTrue((self.work / "made-by-claude.txt").exists())

    # ---------------------------------------------------------------- AWS の認証方式 (bootstrap.sh と同じ設定)
    # 接続先だけを偽の API に差し替え、エージェントが AWS の認証 (署名 / キー / ワークスペース ID) を付けて送ることを確かめる。
    # 実行ロールの代わりに、偽のアクセスキーを環境変数で渡す (MicroVM では SDK が実行ロールの認証情報を自分で取る)

    FAKE_AWS = {"AWS_ACCESS_KEY_ID": "AKIAPMVATEST0000000", "AWS_SECRET_ACCESS_KEY": "fake-secret",
                "AWS_SESSION_TOKEN": "fake-session", "AWS_EC2_METADATA_DISABLED": "true"}

    def run_claude_aws(self, **env):
        mock = AnthropicMock("touch made-by-claude.txt")
        self.addCleanup(mock.shutdown)
        (self.home / ".claude.json").write_text(json.dumps(
            {"hasCompletedOnboarding": True, "projects": {str(self.work): {"hasTrustDialogAccepted": True}}}), encoding="utf-8")
        env = {k: v.replace("{mock}", mock.url) for k, v in env.items()}
        p = subprocess.run([CLAUDE, "-p", "テストして", "--model", "claude-mock", "--allowedTools", "Bash"], cwd=self.work,
                           env=self.agent_env(**env), stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=180)
        self.assertTrue(mock.headers, p.stdout + p.stderr)
        return p, mock

    @unittest.skipUnless(CLAUDE, "PMVA_E2E_CLAUDE が必要")
    def test_claude_mantle_signs_with_aws_credentials(self):
        p, mock = self.run_claude_aws(CLAUDE_CODE_USE_MANTLE="1", AWS_REGION="ap-northeast-1",
                                      ANTHROPIC_BEDROCK_MANTLE_BASE_URL="{mock}", **self.FAKE_AWS)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.work / "made-by-claude.txt").exists())
        for h in mock.headers:
            self.assertRegex(h["authorization"],
                             r"^AWS4-HMAC-SHA256 Credential=AKIAPMVATEST0000000/\d{8}/ap-northeast-1/bedrock-mantle/aws4_request")

    @unittest.skipUnless(CLAUDE, "PMVA_E2E_CLAUDE が必要")
    def test_claude_platform_on_aws_role_and_api_key(self):
        p, mock = self.run_claude_aws(CLAUDE_CODE_USE_ANTHROPIC_AWS="1", ANTHROPIC_AWS_WORKSPACE_ID="wrkspc_01Test",
                                      AWS_REGION="us-east-1", ANTHROPIC_AWS_BASE_URL="{mock}", **self.FAKE_AWS)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for h in mock.headers:
            self.assertEqual(h["anthropic-workspace-id"], "wrkspc_01Test")
            self.assertRegex(h["authorization"], r"^AWS4-HMAC-SHA256 Credential=AKIAPMVATEST0000000/\d{8}/us-east-1/")
        (self.work / "made-by-claude.txt").unlink()
        # ワークスペースの API キーは、AWS の認証情報より優先される
        p, mock = self.run_claude_aws(CLAUDE_CODE_USE_ANTHROPIC_AWS="1", ANTHROPIC_AWS_WORKSPACE_ID="wrkspc_01Test",
                                      AWS_REGION="us-east-1", ANTHROPIC_AWS_BASE_URL="{mock}",
                                      ANTHROPIC_AWS_API_KEY="sk-ant-aws-test", **self.FAKE_AWS)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for h in mock.headers:
            self.assertEqual((h["x-api-key"], h["anthropic-workspace-id"]), ("sk-ant-aws-test", "wrkspc_01Test"))
            self.assertNotIn("authorization", h)

    @unittest.skipUnless(CLAUDE, "PMVA_E2E_CLAUDE が必要")
    def test_claude_bedrock_api_key_is_sent_as_bearer(self):
        # Bedrock の応答 (バイナリのイベントストリーム) は偽の API では返せないので、要求に付く認証だけを確かめる
        p, mock = self.run_claude_aws(CLAUDE_CODE_USE_BEDROCK="1", AWS_BEARER_TOKEN_BEDROCK="bedrock-key-test",
                                      AWS_REGION="ap-northeast-1", ANTHROPIC_BEDROCK_BASE_URL="{mock}", **self.FAKE_AWS)
        self.assertIn("/model/claude-mock/invoke-with-response-stream", [path for path, _ in mock.requests])
        for h in mock.headers:
            self.assertEqual(h["authorization"], "Bearer bedrock-key-test", "キーがあれば AWS の認証情報より優先される")

    def write_codex_bedrock_config(self, provider_lines):
        # bootstrap.sh が書くのと同じ形 (notify と接続先の設定をトップレベルに置き、テーブルはその後)
        (self.home / ".codex" / "config.toml").write_text(
            f'# pmva:provider begin\nmodel_provider = "amazon-bedrock"\n{provider_lines}\n# pmva:provider end\n'
            f'notify = ["{NOTIFY}", "codex"]\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
            f'\n[projects."{self.work}"]\ntrust_level = "trusted"\n', encoding="utf-8")

    def run_codex(self, mock, *overrides, **env):
        args = [CODEX, "exec", "--skip-git-repo-check"]
        for o in overrides:
            args += ["-c", o]
        p = subprocess.run(args + ["テストして"], cwd=self.work,
                           env=self.agent_env(CODEX_HOME=str(self.home / ".codex"), **env),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.work / "made-by-codex.txt").exists(), "ツールが実行された")
        self.assertEqual(self.wait_state("codex", ("done",))["message"], "完了しました。")
        self.assertTrue(mock.requests)
        for path, _ in mock.requests:
            self.assertEqual(path, "/v1/responses")
        return mock.headers

    @unittest.skipUnless(CODEX, "PMVA_E2E_CODEX が必要")
    def test_codex_bedrock_signs_with_aws_credentials(self):
        mock = OpenAIResponsesMock("touch made-by-codex.txt")
        self.addCleanup(mock.shutdown)
        self.write_codex_bedrock_config('model_providers.amazon-bedrock.aws.region = "ap-northeast-1"')
        headers = self.run_codex(mock, f'model_providers.amazon-bedrock.base_url="{mock.url}/v1"', **self.FAKE_AWS)
        for h in headers:
            self.assertRegex(h["authorization"],
                             r"^AWS4-HMAC-SHA256 Credential=AKIAPMVATEST0000000/\d{8}/ap-northeast-1/bedrock-mantle/aws4_request")
            self.assertEqual(h["x-amz-security-token"], "fake-session")

    @unittest.skipUnless(CODEX, "PMVA_E2E_CODEX が必要")
    def test_codex_bedrock_api_key_from_file(self):
        mock = OpenAIResponsesMock("touch made-by-codex.txt")
        self.addCleanup(mock.shutdown)
        key = self.home / ".pmva" / "codex_bedrock_api_key"
        key.write_text("codex-bedrock-key", encoding="utf-8")
        self.write_codex_bedrock_config(
            f'model_providers.amazon-bedrock.base_url = "{mock.url}/v1"\n'
            f'model_providers.amazon-bedrock.auth = {{ command = "cat", args = [{json.dumps(str(key))}] }}')
        # Claude Code 用のキーが環境変数にあっても、ファイルのキーを使う
        headers = self.run_codex(mock, AWS_BEARER_TOKEN_BEDROCK="claude-bedrock-key", **self.FAKE_AWS)
        for h in headers:
            self.assertEqual(h["authorization"], "Bearer codex-bedrock-key")

    @unittest.skipUnless(CODEX, "PMVA_E2E_CODEX が必要")
    def test_codex_gateway_bearer_key_from_file(self):
        mock = OpenAIResponsesMock("touch made-by-codex.txt")
        self.addCleanup(mock.shutdown)
        key = self.home / ".pmva" / "codex_gateway_key"
        key.write_text("gw-key", encoding="utf-8")
        # bootstrap.sh が --codex-auth gateway で書くのと同じ形
        (self.home / ".codex" / "config.toml").write_text(
            '# pmva:provider begin\nmodel_provider = "pmva-gateway"\n'
            'model_providers.pmva-gateway.name = "pmva-gateway"\n'
            f'model_providers.pmva-gateway.base_url = "{mock.url}/v1"\n'
            'model_providers.pmva-gateway.wire_api = "responses"\n'
            f'model_providers.pmva-gateway.auth = {{ command = "cat", args = [{json.dumps(str(key))}] }}\n'
            '# pmva:provider end\n'
            f'notify = ["{NOTIFY}", "codex"]\nmodel = "gpt-mock"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n',
            encoding="utf-8")
        headers = self.run_codex(mock)
        for h in headers:
            self.assertEqual(h["authorization"], "Bearer gw-key")

    @unittest.skipUnless(CODEX, "PMVA_E2E_CODEX が必要")
    def test_codex_gateway_key_header_and_query(self):
        mock = OpenAIResponsesMock("touch made-by-codex.txt")
        self.addCleanup(mock.shutdown)
        (self.home / ".codex" / "config.toml").write_text(
            '# pmva:provider begin\nmodel_provider = "pmva-gateway"\n'
            'model_providers.pmva-gateway.name = "pmva-gateway"\n'
            f'model_providers.pmva-gateway.base_url = "{mock.url}/v1"\n'
            'model_providers.pmva-gateway.wire_api = "responses"\n'
            'model_providers.pmva-gateway.env_http_headers = { "api-key" = "PMVA_CODEX_GATEWAY_KEY" }\n'
            'model_providers.pmva-gateway.query_params = { "api-version" = "2025-04-01-preview" }\n'
            'model_providers.pmva-gateway.http_headers = { "X-Team" = "a" }\n'
            '# pmva:provider end\n'
            f'notify = ["{NOTIFY}", "codex"]\nmodel = "gpt-mock"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n',
            encoding="utf-8")
        p = subprocess.run([CODEX, "exec", "--skip-git-repo-check", "テストして"], cwd=self.work,
                           env=self.agent_env(CODEX_HOME=str(self.home / ".codex"), PMVA_CODEX_GATEWAY_KEY="azure-key"),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        for (path, _), h in zip(mock.requests, mock.headers):
            self.assertEqual(path, "/v1/responses?api-version=2025-04-01-preview")
            self.assertEqual((h["api-key"], h["x-team"]), ("azure-key", "a"))

    @unittest.skipUnless(CODEX, "PMVA_E2E_CODEX が必要")
    def test_codex_browser_login_waits_on_port_1455(self):
        """mvm login が前提にしていること: codex login は 127.0.0.1:1455 で戻りを待ち、ログインの URL を表示する。"""
        import re
        import urllib.error
        import urllib.request
        import signal
        # npm の codex は本体を子プロセスで動かすので、プロセスのグループごと止める (残ると 1455 番を使い続ける)
        proc = subprocess.Popen([CODEX, "login"], env=self.agent_env(CODEX_HOME=str(self.home / ".codex"), BROWSER="true"),
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                start_new_session=True)
        self.addCleanup(proc.stdout.close)
        self.addCleanup(proc.wait, 10)
        self.addCleanup(os.killpg, proc.pid, signal.SIGTERM)
        url = None
        for line in proc.stdout:
            m = re.search(r"https://auth\.openai\.com/\S+", line)
            if m:
                url = m.group(0)
                break
        self.assertIsNotNone(url)
        self.assertRegex(url, MVM_LOGIN_URL)
        self.assertIn("redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback", url)
        with self.assertRaises(urllib.error.HTTPError) as e:   # 正しくない state には 400 を返す (待っている)
            urllib.request.urlopen("http://127.0.0.1:1455/auth/callback?code=x&state=wrong", timeout=10)
        self.assertEqual(e.exception.code, 400)

    # ---------------------------------------------------------------- 司令塔 (mvm orchestrator install)

    def install_orchestrator(self, *args):
        subprocess.run([sys.executable, str(MVM), "orchestrator", "install", *args], check=True, capture_output=True,
                       env=dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.home / ".codex")))

    def run_claude(self, mock):
        p = subprocess.run([CLAUDE, "-p", "テストして", "--model", "claude-mock"], cwd=self.work,
                           env=self.agent_env(ANTHROPIC_BASE_URL=mock.url, PMVA_CONFIG=str(self.home / "no-config.env")),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        transcript = self.wait_state("claude", ("done",))["transcript"]
        return [e for e in self.events("claude", transcript) if e[0] == "output"]

    @unittest.skipUnless(CLAUDE, "PMVA_E2E_CLAUDE が必要")
    def test_claude_orchestrator_skill_guard_and_allowed_commands(self):
        mock = self.claude_settings()
        mock.command = "npm test"
        self.install_orchestrator("claude", "--dir", str(self.work))
        # 重い処理はフックが止め、その理由が Claude に返る
        outputs = self.run_claude(mock)
        self.assertEqual(len(outputs), 1)
        self.assertIn("この PC は司令塔なので、重い処理はここで動かしません: npm test", outputs[0][2])
        # skill が読み込まれている (一覧が API への要求に含まれる)
        self.assertIn("mvm-orchestrator", json.dumps([b for _, b in mock.requests], ensure_ascii=False))
        # 確認用の mvm コマンドは、許可を求めずに実行される (-p では許可されていないツールは実行されない)
        (self.home / ".pmva" / "agents" / "claude.json").unlink()
        mock.command = f"{ROOT / 'bin' / 'mvm'} ls --json"
        outputs = self.run_claude(mock)
        self.assertIn("no-config.env がありません", outputs[0][2], outputs)

    # ---------------------------------------------------------------- Codex

    @unittest.skipUnless(CODEX, "PMVA_E2E_CODEX が必要")
    def test_codex_finds_orchestrator_skill(self):
        self.install_orchestrator("codex")
        mock = OpenAIResponsesMock("true")
        self.addCleanup(mock.shutdown)
        self.write_codex_config(mock)
        p = subprocess.run([CODEX, "exec", "--skip-git-repo-check", "テストして"], cwd=self.work,
                           env=self.agent_env(MOCK_API_KEY="x", CODEX_HOME=str(self.home / ".codex")),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("mvm-orchestrator", json.dumps(mock.requests[0][1], ensure_ascii=False))

    def write_codex_config(self, mock):
        # bootstrap.sh と同じく notify をいちばん上に書く
        (self.home / ".codex" / "config.toml").write_text(
            f'notify = ["{NOTIFY}", "codex"]\n'
            'model = "gpt-mock"\nmodel_provider = "mock"\napproval_policy = "never"\nsandbox_mode = "danger-full-access"\n'
            f'[model_providers.mock]\nname = "mock"\nbase_url = "{mock.url}/v1"\nwire_api = "responses"\nenv_key = "MOCK_API_KEY"\n',
            encoding="utf-8")

    @unittest.skipUnless(CODEX, "PMVA_E2E_CODEX が必要")
    def test_codex_notify_records_state_and_rollout(self):
        mock = OpenAIResponsesMock("touch made-by-codex.txt")
        self.addCleanup(mock.shutdown)
        self.write_codex_config(mock)
        (self.home / ".codex" / "AGENTS.md").write_text("PMVA-PROFILE-RULE: テストのルール\n", encoding="utf-8")
        p = subprocess.run([CODEX, "exec", "--skip-git-repo-check", "テストして"], cwd=self.work,
                           env=self.agent_env(MOCK_API_KEY="x", CODEX_HOME=str(self.home / ".codex")),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.work / "made-by-codex.txt").exists(), "ツールが実行された")
        self.assertIn("PMVA-PROFILE-RULE", json.dumps(mock.requests[0][1], ensure_ascii=False), "~/.codex/AGENTS.md を読んでいる")
        st = self.wait_state("codex", ("done",))
        self.assertEqual(st["message"], "完了しました。", "notify の last-assistant-message")
        rollouts = list((self.home / ".codex" / "sessions").rglob("rollout-*.jsonl"))
        self.assertEqual(len(rollouts), 1)
        self.assertEqual(self.sup.last_codex_text(str(rollouts[0])), "完了しました。", "会話記録から取り出す経路")
        self.assertEqual(self.events("codex", rollouts[0]), [
            ("prompt", None, "テストして"), ("thinking", None, "まずコマンドを実行する"), ("text", None, "確認します。"),
            ("tool", "exec_command", "touch made-by-codex.txt"), ("output", None, ""), ("text", None, "完了しました。")])


if __name__ == "__main__":
    unittest.main()
