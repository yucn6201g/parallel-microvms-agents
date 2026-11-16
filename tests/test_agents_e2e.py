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
        t = Path(self.tmp.name)
        self.home, self.work = t / "home", t / "work"
        for d in (self.home / ".claude", self.home / ".codex", self.home / ".pmva", self.work):
            d.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(self.work)], check=True)
        self.sup = load_supervisor()

    def tearDown(self):
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
        p = subprocess.run([CLAUDE, "-p", "テストして", "--model", "claude-mock", "--allowedTools", "Bash"],
                           cwd=self.work, env=self.agent_env(ANTHROPIC_BASE_URL=mock.url), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.work / "made-by-claude.txt").exists(), "ツールが実行された")
        st = self.wait_state("claude", ("done",))
        self.assertEqual(st["message"], "完了しました。", "Stop フックの last_assistant_message")
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
        p = subprocess.run([CODEX, "exec", "--skip-git-repo-check", "テストして"], cwd=self.work,
                           env=self.agent_env(MOCK_API_KEY="x", CODEX_HOME=str(self.home / ".codex")),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((self.work / "made-by-codex.txt").exists(), "ツールが実行された")
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
