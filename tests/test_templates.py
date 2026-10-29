"""infra/ の 2 つの CloudFormation テンプレート (標準と最小構成) がずれていないことを確かめる。

最小構成 (cloudformation-minimal.yaml) は標準 (cloudformation.yaml) から、キープアライブ・毎晩の自動停止・
予算アラートを除いたもの。共通のリソースと出力は同じ内容でなければならない (片方だけ直すと動作が変わるため)。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FULL = ROOT / "infra" / "cloudformation.yaml"
MINIMAL = ROOT / "infra" / "cloudformation-minimal.yaml"
# 最小構成に入れないもの (標準だけにあるリソースとパラメータ)
OPTIONAL_PREFIXES = ("Keepalive", "NightlyStop", "MonthlyBudget", "Budget", "ImageName")


def sections(path):
    """トップレベルのセクション名 → そのセクションの中の {論理 ID: 定義のテキスト}。"""
    result, section, name = {}, None, None
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\S", line) and not line.startswith("#"):
            section, name = line.split(":", 1)[0], None
            result.setdefault(section, {})
            continue
        m = re.match(r"^  ([A-Za-z0-9]+):", line)
        if section and m:
            name = m.group(1)
            result[section][name] = []
        if section and name:
            result[section][name].append(line)
    return {s: {n: "\n".join(body).rstrip() for n, body in items.items()} for s, items in result.items()}


def without_comment_lines(text):
    return "\n".join(l for l in text.splitlines() if not l.strip().startswith("#")).rstrip()


class TemplateTest(unittest.TestCase):
    def setUp(self):
        self.full, self.minimal = sections(FULL), sections(MINIMAL)

    def test_minimal_has_only_required_resources(self):
        optional = [n for n in self.minimal["Resources"] if n.startswith(OPTIONAL_PREFIXES)]
        self.assertEqual(optional, [], "最小構成にキープアライブ・夜間停止・予算のリソースを入れない")
        params = [n for n in self.minimal["Parameters"] if n.startswith(OPTIONAL_PREFIXES)]
        self.assertEqual(params, [])

    def test_shared_resources_are_identical(self):
        for name, body in self.minimal["Resources"].items():
            self.assertIn(name, self.full["Resources"], name)
            self.assertEqual(without_comment_lines(body), without_comment_lines(self.full["Resources"][name]),
                             f"{name} が標準と最小構成で違う。両方のテンプレートを同じように直すこと")
        missing = [n for n in self.full["Resources"] if n not in self.minimal["Resources"]
                   and not n.startswith(OPTIONAL_PREFIXES)]
        self.assertEqual(missing, [], "標準にだけある必須リソース")

    def test_same_outputs_and_parameters(self):
        self.assertEqual(self.minimal["Outputs"], self.full["Outputs"], "mvm が読む出力は両方で同じにする")
        for name, body in self.minimal["Parameters"].items():
            self.assertEqual(body, self.full["Parameters"][name], name)


if __name__ == "__main__":
    unittest.main()
