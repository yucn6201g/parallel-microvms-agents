"""scripts/orchestrator_guard.py (司令塔の PC で重い処理を止める Claude Code のフック) のテスト。"""

import importlib.util
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

GUARD = Path(__file__).resolve().parent.parent / "scripts" / "orchestrator_guard.py"
spec = importlib.util.spec_from_file_location("orchestrator_guard", GUARD)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class GuardTest(unittest.TestCase):
    def test_heavy_commands_are_found(self):
        for command, hit in (
            ("npm test", "npm test"), ("cd app && npm install", "npm install"), ("FOO=1 pytest -x", "pytest -x"),
            ("python3 -m pytest tests", "python3 -m pytest tests"), ("npx playwright test", "npx playwright test"),
            ("docker compose up -d", "docker compose up -d"), ("./gradlew build", "./gradlew build"),
            ("ls | npm ci", "npm ci"), ("cargo build --release", "cargo build --release"), ("yarn", "yarn"),
            ("sudo pip install x", "pip install x"), ("uv sync", "uv sync"), ("go test ./...", "go test ./..."),
        ):
            self.assertEqual(guard.heavy_command(command), hit, command)

    def test_light_commands_pass(self):
        for command in (
            'mvm send a1 "直して; npm test を通して"', "mvm wait a1 --timeout 540 --json", "npm --version",
            "npm view react version", "git commit -m 'npm test && pytest'", "gh pr diff 12", "node --version",
            'mvm send a1 "say \\"hi\\"; npm x"', "ls -la && cat README.md", "PMVA_ALLOW_LOCAL=1 npm test",
            "git log --oneline | head", "",
        ):
            self.assertIsNone(guard.heavy_command(command), command)

    def run_hook(self, event):
        # Windows の既定 (cp1252 など) と同じく、標準入出力が UTF-8 でない状態で動かす
        env = dict(os.environ, PYTHONIOENCODING="cp1252")
        return subprocess.run([sys.executable, str(GUARD)], input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
                              capture_output=True, env=env)

    def test_hook_blocks_with_exit_code_2(self):
        p = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "npm run build"}})
        self.assertEqual(p.returncode, 2)
        self.assertIn("この PC は司令塔なので、重い処理はここで動かしません: npm run build", p.stderr.decode("utf-8"))
        # 日本語を含むコマンドも、UTF-8 のまま読み、日本語の説明を UTF-8 で返す
        p = self.run_hook({"tool_name": "Bash", "tool_input": {"command": "echo 日本語 && pytest -k 日本語"}})
        self.assertEqual(p.returncode, 2)
        self.assertIn("ここで動かしません: pytest -k 日本語", p.stderr.decode("utf-8"))
        for event in ({"tool_name": "Bash", "tool_input": {"command": "mvm ls"}},
                      {"tool_name": "Read", "tool_input": {"file_path": "package.json"}}):
            self.assertEqual(self.run_hook(event).returncode, 0, event)
        p = subprocess.run([sys.executable, str(GUARD)], input=b"not json", capture_output=True)
        self.assertEqual(p.returncode, 0, "壊れた入力でエージェントを止めない")


if __name__ == "__main__":
    unittest.main()
