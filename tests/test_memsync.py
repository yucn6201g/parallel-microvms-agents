"""image/memsync.py (Claude Code の自動メモリを S3 経由で共有する) のテスト。AWS には一切つながない。

S3 は FakeS3 (aws の呼び出しを受けてメモリ上のオブジェクトで答える実行関数) で作る。
mvm memory の結合テストは tests/test_mvm.py にある。
"""

import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "image"))
import memsync  # noqa: E402

BUCKET = "bkt"
PREFIX = "memory/github.com/owner/repo/"
URI = f"s3://{BUCKET}/{PREFIX}"


def md5(data):
    return hashlib.md5(data).hexdigest()


class FakeS3:
    """memsync が呼ぶ aws の代わり (list-objects-v2 と s3 cp だけ)。"""

    def __init__(self):
        self.objects = {}      # キー → 中身
        self.metadata = {}     # キー → --metadata の値
        self.etags = {}        # キー → ETag の上書き
        self.calls = []
        self.fail = None       # 設定すると全部の呼び出しがこのエラー文で失敗する
        self.page_size = 0     # 0 以外なら list-objects-v2 をこの件数ずつに分けて返す

    def put(self, name, data, prefix=PREFIX):
        self.objects[prefix + name] = data if isinstance(data, bytes) else data.encode("utf-8")

    def get(self, name, prefix=PREFIX):
        return self.objects.get(prefix + name)

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail:
            return 254, "", self.fail
        if args[:2] == ["s3api", "list-objects-v2"]:
            prefix = args[args.index("--prefix") + 1]
            keys = sorted(k for k in self.objects if k.startswith(prefix))
            start = int(args[args.index("--continuation-token") + 1]) if "--continuation-token" in args else 0
            page = keys[start:start + self.page_size] if self.page_size else keys[start:]
            body = {"Contents": [{"Key": k, "ETag": '"%s"' % self.etags.get(k, md5(self.objects[k]))} for k in page]}
            if self.page_size and start + self.page_size < len(keys):
                body["NextContinuationToken"] = str(start + self.page_size)
            return 0, json.dumps(body if page else {}), ""
        if args[:2] == ["s3", "cp"]:
            paths = [x for i, x in enumerate(args[2:], start=2) if not x.startswith("--") and args[i - 1] != "--metadata"]
            src, dest = paths
            if dest.startswith("s3://"):
                key = dest[len("s3://" + BUCKET + "/"):]
                self.objects[key] = Path(src).read_bytes()
                if "--metadata" in args:
                    self.metadata[key] = args[args.index("--metadata") + 1]
                return 0, "", ""
            key = src[len("s3://" + BUCKET + "/"):]
            if key not in self.objects:
                return 1, "", "404 Not Found"
            Path(dest).write_bytes(self.objects[key])
            return 0, "", ""
        return 1, "", "unhandled: %s" % args


class MemsyncTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.dir = t / "memory"
        self.dir.mkdir()
        self.state = t / "state" / "m.json"
        self.s3 = FakeS3()
        self.lines = []

    def sync(self, **kw):
        kw.setdefault("origin", "local")
        return memsync.Memsync(self.dir, URI, self.state, runner=self.s3, out=self.lines.append, **kw)

    def write(self, name, data):
        (self.dir / name).write_bytes(data if isinstance(data, bytes) else data.encode("utf-8"))

    def read(self, name):
        return (self.dir / name).read_bytes()

    def set_state(self, **files):
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps({"files": {k.replace("_", "-") + ".md": v for k, v in files.items()}}), encoding="utf-8")

    def get_state(self):
        return json.loads(self.state.read_text(encoding="utf-8"))["files"]

    def text(self):
        return "\n".join(self.lines)


class RepoIdTest(unittest.TestCase):
    def test_forms_normalize_to_same_id(self):
        for url in ("https://github.com/Owner/Repo.git", "git@github.com:Owner/Repo.git", "ssh://git@github.com/Owner/Repo",
                    "https://github.com/OWNER/repo/", "ssh://git@github.com:22/Owner/Repo.git", "github.com:Owner/Repo"):
            self.assertEqual(memsync.normalize_repo_id(url), "github.com/owner/repo", url)

    def test_userinfo_is_dropped(self):
        rid = memsync.normalize_repo_id("https://x-access-token:ghp_FAKEFAKEFAKE@github.com/Owner/Repo.git")
        self.assertEqual(rid, "github.com/owner/repo")

    def test_nested_groups_are_kept(self):
        self.assertEqual(memsync.normalize_repo_id("https://gitlab.example.com/Group/Sub/Repo.git"),
                         "gitlab.example.com/group/sub/repo")

    def test_invalid_urls_are_rejected_without_echoing_the_url(self):
        for url in ("", "not a url", "https://github.com/onlyowner", "/home/me/repo", r"C:\work\repo", "file:///srv/repo.git",
                    "https://github.com/o/r with space", "https://tok:SECRETSECRET@github.com/../r",
                    "C:/Users/me/repo", "c:/work/a/b", "D:\\work\\a\\b"):
            with self.assertRaises(memsync.MemsyncError, msg=url) as cm:
                memsync.normalize_repo_id(url)
            self.assertNotIn("SECRETSECRET", str(cm.exception))
            self.assertIn("origin", str(cm.exception))


class MemoryDirTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.repo = Path(self.tmp.name) / "repo"
        (self.repo / ".claude").mkdir(parents=True)
        (self.home / ".claude").mkdir(parents=True)

    def git(self, common_dir, toplevel=None):
        def fake(directory, *args):
            if args == ("rev-parse", "--git-common-dir"):
                return common_dir
            if args == ("rev-parse", "--show-toplevel") and toplevel:
                return toplevel
            raise memsync.MemsyncError("unexpected %s" % (args,))
        return fake

    def test_relative_common_dir_is_resolved_against_the_folder(self):
        # git は通常のリポジトリのルートで実行すると、相対パス (.git) を返す
        found = memsync.resolve_memory_dir(self.repo, home=self.home, git=self.git(".git"))
        self.assertEqual(found.parent.name, memsync.sanitize_path(os.path.realpath(str(self.repo))))

    def test_relative_common_dir_from_a_subfolder(self):
        sub = self.repo / "src" / "pkg"
        sub.mkdir(parents=True)
        found = memsync.resolve_memory_dir(sub, home=self.home, git=self.git("../../.git"))
        self.assertEqual(found.parent.name, memsync.sanitize_path(os.path.realpath(str(self.repo))))

    def test_unknown_option_is_not_echoed_as_a_path(self):
        # 古い git は知らないオプションをそのまま出力して終了コード 0 を返す。--path-format には頼らない
        def fake(directory, *args):
            if "--path-format=absolute" in args:
                return "--path-format=absolute\n.git"
            return self.git(".git")(directory, *args)
        found = memsync.resolve_memory_dir(self.repo, home=self.home, git=fake)
        self.assertEqual(found.parent.name, memsync.sanitize_path(os.path.realpath(str(self.repo))))

    def test_submodule_uses_show_toplevel(self):
        # サブモジュールや --separate-git-dir では、共通の git フォルダの親はリポジトリのルートではない
        found = memsync.resolve_memory_dir(self.repo, home=self.home,
                                           git=self.git("/src/super/.git/modules/sub", toplevel="/src/super/sub"))
        self.assertEqual(found.parent.name, "-src-super-sub")

    def test_sanitize(self):
        self.assertEqual(memsync.sanitize_path("C:/work/parallel-microvms-agents"), "C--work-parallel-microvms-agents")
        self.assertEqual(memsync.sanitize_path(r"C:\work\parallel-microvms-agents"), "C--work-parallel-microvms-agents")
        self.assertEqual(memsync.sanitize_path("/Users/me/my repo_1"), "-Users-me-my-repo-1")

    def test_default_folder_from_windows_style_git_output(self):
        # Windows の git は C:/work/... の形で返す。どの OS でもサニタイズの結果は同じになる
        found = memsync.resolve_memory_dir("ignored", home=self.home, git=self.git("C:/work/parallel-microvms-agents/.git"))
        self.assertEqual(found, self.home / ".claude" / "projects" / "C--work-parallel-microvms-agents" / "memory")

    def test_worktree_uses_main_repository_root(self):
        # --git-common-dir は worktree でもメインの .git を指す
        main = Path(self.tmp.name).resolve() / "main-repo"
        found = memsync.resolve_memory_dir("ignored", home=self.home, git=self.git(str(main / ".git")))
        self.assertEqual(found.parent.name, memsync.sanitize_path(main))

    def test_falls_back_to_show_toplevel(self):
        def fake(directory, *args):
            if args[0] == "rev-parse" and "--show-toplevel" in args:
                return "/src/old-git"
            raise memsync.MemsyncError("old git")
        found = memsync.resolve_memory_dir("ignored", home=self.home, git=fake)
        self.assertEqual(found.parent.name, "-src-old-git")

    def test_not_a_repository(self):
        def fake(directory, *args):
            raise memsync.MemsyncError("not a repo")
        with self.assertRaises(memsync.MemsyncError):
            memsync.resolve_memory_dir("ignored", home=self.home, git=fake)

    def test_setting_priority_and_tilde(self):
        git = self.git(str(self.repo / ".git"))
        (self.home / ".claude" / "settings.json").write_text('{"autoMemoryDirectory": "~/from-user"}', encoding="utf-8")
        self.assertEqual(memsync.resolve_memory_dir("x", home=self.home, git=git), self.home / "from-user")
        (self.repo / ".claude" / "settings.local.json").write_text('{"autoMemoryDirectory": "~/from-local"}', encoding="utf-8")
        self.assertEqual(memsync.resolve_memory_dir("x", home=self.home, git=git), self.home / "from-local")
        absolute = Path(self.tmp.name) / "abs"
        (self.repo / ".claude" / "settings.local.json").write_text(
            json.dumps({"autoMemoryDirectory": str(absolute)}), encoding="utf-8")
        self.assertEqual(memsync.resolve_memory_dir("x", home=self.home, git=git), absolute)

    def test_checked_in_settings_are_ignored(self):
        (self.repo / ".claude" / "settings.json").write_text('{"autoMemoryDirectory": "~/evil"}', encoding="utf-8")
        found = memsync.resolve_memory_dir("x", home=self.home, git=self.git(str(self.repo / ".git")))
        self.assertEqual(found.name, "memory")
        self.assertEqual(found.parent.parent.name, "projects")

    def test_relative_setting_is_rejected(self):
        (self.home / ".claude" / "settings.json").write_text('{"autoMemoryDirectory": "relative/dir"}', encoding="utf-8")
        with self.assertRaises(memsync.MemsyncError):
            memsync.resolve_memory_dir("x", home=self.home, git=self.git(str(self.repo / ".git")))

    def test_too_long_path_asks_for_setting(self):
        git = self.git("/" + "a" * 210 + "/.git")
        with self.assertRaises(memsync.MemsyncError) as cm:
            memsync.resolve_memory_dir("x", home=self.home, git=git)
        self.assertIn("autoMemoryDirectory", str(cm.exception))
        self.assertIn("settings.local.json", str(cm.exception))

    def test_state_file_name(self):
        self.assertEqual(memsync.state_file_for("/s", "github.com/owner/repo"), Path("/s") / "memory" / "github.com__owner__repo.json")


class PullTest(MemsyncTestCase):
    def test_remote_only_is_fetched(self):
        self.s3.put("a.md", "remote a\n")
        rc = self.sync().pull()
        self.assertEqual(rc, 0)
        self.assertEqual(self.read("a.md"), b"remote a\n")
        self.assertEqual(self.get_state(), {"a.md": md5(b"remote a\n")})
        self.assertIn("取得 1、送信 0、競合 0、飛ばした 0", self.text())

    def test_remote_updated_when_local_unchanged(self):
        self.write("a.md", "old\n")
        self.set_state(a=md5(b"old\n"))
        self.s3.put("a.md", "new\n")
        self.sync().pull()
        self.assertEqual(self.read("a.md"), b"new\n")
        self.assertEqual(self.get_state()["a.md"], md5(b"new\n"))

    def test_locally_deleted_is_not_fetched(self):
        self.s3.put("a.md", "x\n")
        self.set_state(a=md5(b"x\n"))
        self.sync().pull()
        self.assertFalse((self.dir / "a.md").exists())
        self.assertIn("手元で消したファイル (S3 には残っています)", self.text())
        self.assertIn("飛ばした 1", self.text())
        self.assertEqual(self.get_state(), {"a.md": md5(b"x\n")}, "基準は残す")

    def test_conflict_is_skipped_then_forced(self):
        self.write("a.md", "local\n")
        self.set_state(a=md5(b"base\n"))
        self.s3.put("a.md", "remote\n")
        self.sync().pull()
        self.assertEqual(self.read("a.md"), b"local\n")
        self.assertIn("競合 1", self.text())
        self.assertIn("--force", self.text())
        self.lines.clear()
        self.sync(force=True).pull()
        self.assertEqual(self.read("a.md"), b"remote\n")
        self.assertIn("取得 1、送信 0、競合 0", self.text())

    def test_conflict_without_base(self):
        self.write("a.md", "local\n")
        self.s3.put("a.md", "remote\n")
        self.sync().pull()
        self.assertEqual(self.read("a.md"), b"local\n")
        self.assertIn("競合 1", self.text())

    def test_local_change_is_left_alone(self):
        self.write("a.md", "edited\n")
        self.set_state(a=md5(b"base\n"))
        self.s3.put("a.md", "base\n")
        self.sync().pull()
        self.assertEqual(self.read("a.md"), b"edited\n")
        self.assertIn("取得 0、送信 0、競合 0、飛ばした 0", self.text())
        self.assertEqual(self.get_state()["a.md"], md5(b"base\n"), "手元が変わっているので基準は動かさない")

    def test_remote_updated_after_local_delete_is_fetched(self):
        self.s3.put("a.md", "v2\n")
        self.set_state(a=md5(b"v1\n"))
        self.sync().pull()
        self.assertEqual(self.read("a.md"), b"v2\n")

    def test_same_updates_base(self):
        self.write("a.md", "same\n")
        self.s3.put("a.md", "same\n")
        self.sync().pull()
        self.assertEqual(self.get_state(), {"a.md": md5(b"same\n")})

    def test_local_share_false_is_not_overwritten(self):
        self.write("a.md", "---\nshare: false\n---\nmine\n")
        self.set_state(a=md5(normalize("---\nshare: false\n---\nmine\n")))
        self.s3.put("a.md", "from s3\n")
        self.sync().pull()
        self.assertIn("share: false", self.read("a.md").decode("utf-8"))
        self.assertIn("飛ばした 1", self.text())

    def test_remote_crlf_is_written_with_lf(self):
        self.s3.put("a.md", b"line1\r\nline2\r\n")
        self.sync().pull()
        self.assertEqual(self.read("a.md"), b"line1\nline2\n")

    def test_unknown_etag_is_treated_as_different(self):
        self.write("a.md", "x\n")
        self.set_state(a=md5(b"x\n"))
        self.s3.put("a.md", "x\n")
        self.s3.etags[PREFIX + "a.md"] = "0123456789abcdef0123456789abcdef-2"
        self.sync().pull()
        self.assertIn("取得 1", self.text())

    def test_only_flat_md_files_under_prefix(self):
        self.s3.put("sub/b.md", "x\n")
        self.s3.put("c.txt", "x\n")
        self.s3.put("d.md", "x\n")
        self.sync().pull()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["d.md"])

    def test_unsafe_names_are_not_written_outside_the_folder(self):
        # S3 のキーに \ や : があると、Windows では手元のフォルダの外や別のストリームに書けてしまう
        for name in ("..\\evil.md", "a\\b.md", "c:evil.md", ".hidden.md"):
            self.s3.put(name, "x\n")
        self.s3.put("日本語のメモ.md", "ok\n")
        self.sync().pull()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["日本語のメモ.md"])
        self.assertFalse((self.dir.parent / "evil.md").exists())
        self.assertIn("..\\evil.md", self.text())

    def test_pagination(self):
        for i in range(5):
            self.s3.put("f%d.md" % i, "x%d\n" % i)
        self.s3.page_size = 2
        self.sync().pull()
        self.assertEqual(len(list(self.dir.glob("*.md"))), 5)

    def test_dry_run_changes_nothing(self):
        self.s3.put("a.md", "remote\n")
        self.s3.put("MEMORY.md", "- [A](a.md) - a\n")
        self.sync(dry_run=True).pull()
        self.assertEqual(list(self.dir.iterdir()), [])
        self.assertFalse(self.state.exists())
        self.assertIn("[dry-run] 取得: a.md", self.text())
        self.assertIn("[dry-run] MEMORY.md に 1 行を足します", self.text())


def normalize(text):
    return memsync.normalize_bytes(text.encode("utf-8"))


class PushTest(MemsyncTestCase):
    def test_local_only_is_sent_with_origin(self):
        self.write("a.md", "mine\n")
        self.sync(origin="agent-01").push()
        self.assertEqual(self.s3.get("a.md"), b"mine\n")
        self.assertEqual(self.s3.metadata[PREFIX + "a.md"], "pmva-origin=agent-01")
        self.assertEqual(self.get_state(), {"a.md": md5(b"mine\n")})
        self.assertIn("取得 0、送信 1、競合 0、飛ばした 0", self.text())

    def test_default_origin_is_local(self):
        self.write("a.md", "mine\n")
        self.sync().push()
        self.assertEqual(self.s3.metadata[PREFIX + "a.md"], "pmva-origin=local")

    def test_local_change_is_sent(self):
        self.write("a.md", "edited\n")
        self.set_state(a=md5(b"base\n"))
        self.s3.put("a.md", "base\n")
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"edited\n")

    def test_remote_newer_is_not_overwritten(self):
        self.write("a.md", "base\n")
        self.set_state(a=md5(b"base\n"))
        self.s3.put("a.md", "newer\n")
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"newer\n")
        self.assertIn("先に pull", self.text())
        self.assertIn("飛ばした 1", self.text())

    def test_conflict_is_skipped_then_forced(self):
        self.write("a.md", "local\n")
        self.set_state(a=md5(b"base\n"))
        self.s3.put("a.md", "remote\n")
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"remote\n")
        self.assertIn("競合 1", self.text())
        self.lines.clear()
        self.sync(force=True).push()
        self.assertEqual(self.s3.get("a.md"), b"local\n")
        self.assertIn("送信 1、競合 0", self.text())

    def test_remote_deleted_is_sent_again(self):
        self.write("a.md", "x\n")
        self.set_state(a=md5(b"x\n"))
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"x\n")

    def test_locally_deleted_is_not_propagated(self):
        self.s3.put("a.md", "x\n")
        self.set_state(a=md5(b"x\n"))
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"x\n")
        self.assertFalse(any(c[:2] == ["s3", "rm"] for c in self.s3.calls))

    def test_same_is_not_sent(self):
        self.write("a.md", "x\n")
        self.s3.put("a.md", "x\n")
        self.sync().push()
        self.assertFalse(any(c[:2] == ["s3", "cp"] for c in self.s3.calls))

    def test_crlf_is_normalized_before_sending_and_comparing(self):
        self.write("a.md", b"one\r\ntwo\r\n")
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"one\ntwo\n")
        self.assertEqual(self.get_state()["a.md"], md5(b"one\ntwo\n"))
        self.s3.calls.clear()
        self.sync().push()
        self.assertFalse(any(c[:2] == ["s3", "cp"] for c in self.s3.calls), "改行だけの違いは同じ内容")

    def test_share_false_is_not_sent(self):
        self.write("private.md", "---\nname: p\nshare: false\n---\nsecret plans\n")
        self.write("public.md", "---\nshare: true\n---\nok\n")
        self.sync().push()
        self.assertIsNone(self.s3.get("private.md"))
        self.assertIsNotNone(self.s3.get("public.md"))
        self.assertIn("private.md: 送りません (手元専用", self.text())
        self.assertIn("飛ばした 1", self.text())

    def test_share_false_only_in_frontmatter(self):
        self.write("a.md", "---\nname: a\n---\nthe line below is body\nshare: false\n")
        self.sync().push()
        self.assertIsNotNone(self.s3.get("a.md"))

    def test_secret_like_content_is_not_sent(self):
        self.write("key.md", "token: ghp_" + "A" * 36 + "\n")
        self.write("aws.md", "AKIA" + "A" * 16 + "\n")
        self.sync().push()
        self.assertEqual(self.s3.objects, {})
        self.assertIn("認証情報", self.text())
        self.assertIn("飛ばした 2", self.text())

    def test_large_file_is_not_sent(self):
        self.write("big.md", b"x" * (1024 * 1024 + 1))
        self.write("ok.md", b"x" * 1024 * 1024)
        self.sync().push()
        self.assertIsNone(self.s3.get("big.md"))
        self.assertIsNotNone(self.s3.get("ok.md"))
        self.assertIn("大きすぎる", self.text())

    def test_dry_run_changes_nothing(self):
        self.write("a.md", "mine\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n")
        self.sync(dry_run=True).push()
        self.assertEqual(self.s3.objects, {})
        self.assertFalse(self.state.exists())
        self.assertIn("[dry-run] 送信: a.md", self.text())
        self.assertIn("[dry-run] MEMORY.md に", self.text())


class IndexTest(MemsyncTestCase):
    def test_pull_adds_only_lines_for_files_that_exist_locally(self):
        self.write("a.md", "a\n")
        self.write("MEMORY.md", "# 索引\n- [A](a.md) - 手元\n")
        self.s3.put("a.md", "a\n")
        self.s3.put("c.md", "c\n")
        self.s3.put("MEMORY.md", "# 別の見出し\n- [A](a.md) - S3 の説明\n- [C](c.md) - 新しい\n- [D](missing.md) - 無いファイル\n")
        self.sync().pull()
        self.assertEqual(self.read("MEMORY.md").decode("utf-8"), "# 索引\n- [A](a.md) - 手元\n- [C](c.md) - 新しい\n")

    def test_pull_without_change_does_not_touch_index(self):
        original = b"- [A](a.md) - a"   # 末尾の改行が無い
        self.write("a.md", "a\n")
        self.write("MEMORY.md", original)
        self.s3.put("a.md", "a\n")
        self.s3.put("MEMORY.md", "- [A](a.md) - a\n")
        self.sync().pull()
        self.assertEqual(self.read("MEMORY.md"), original)

    def test_pull_creates_index_when_missing(self):
        self.s3.put("a.md", "a\n")
        self.s3.put("MEMORY.md", "# Memory\n- [A](a.md) - a\n- [Z](z.md) - z\n")
        self.sync().pull()
        self.assertEqual(self.read("MEMORY.md").decode("utf-8"), "# Memory\n- [A](a.md) - a\n")

    def test_pull_index_ignores_missing_remote_index(self):
        self.s3.put("a.md", "a\n")
        self.sync().pull()
        self.assertFalse((self.dir / "MEMORY.md").exists())

    def test_push_adds_local_lines_for_files_on_s3(self):
        self.write("a.md", "a\n")
        self.write("b.md", "b\n")
        self.write("p.md", "---\nshare: false\n---\np\n")
        self.write("MEMORY.md", "# 手元\n- [A](a.md) - a\n- [B](b.md) - b\n- [P](p.md) - private\n- [X](nofile.md) - x\n- メモ\n")
        self.s3.put("a.md", "a\n")
        self.s3.put("MEMORY.md", "# S3\n- [A](a.md) - S3 の説明\n")
        self.sync().push()
        self.assertEqual(self.s3.get("MEMORY.md").decode("utf-8"), "# S3\n- [A](a.md) - S3 の説明\n- [B](b.md) - b\n")
        self.assertEqual(self.s3.metadata[PREFIX + "MEMORY.md"], "pmva-origin=local")

    def test_push_creates_index_when_missing_on_s3(self):
        self.write("a.md", "a\n")
        self.write("MEMORY.md", "# 手元\n- [A](a.md) - a\n- [X](nofile.md) - x\n")
        self.sync().push()
        self.assertEqual(self.s3.get("MEMORY.md").decode("utf-8"), "# 手元\n- [A](a.md) - a\n")

    def test_push_does_not_send_index_lines_for_skipped_files(self):
        self.write("k.md", "ghp_" + "B" * 36 + "\n")
        self.write("MEMORY.md", "- [K](k.md) - key\n")
        self.s3.put("MEMORY.md", "- [Z](z.md) - z\n")
        self.sync().push()
        self.assertEqual(self.s3.get("MEMORY.md"), b"- [Z](z.md) - z\n", "変化がないので送らない")

    def test_push_without_change_does_not_upload_index(self):
        self.write("a.md", "a\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n")
        self.s3.put("a.md", "a\n")
        self.s3.put("MEMORY.md", "- [A](a.md) - a\n")
        self.sync().push()
        self.assertEqual(self.s3.metadata, {}, "何も送らない")

    def test_index_over_200_lines_warns(self):
        names = ["n%03d.md" % i for i in range(201)]
        for n in names:
            self.s3.put(n, "x\n")
        self.s3.put("MEMORY.md", "".join("- [N](%s) - n\n" % n for n in names))
        self.sync().pull()
        self.assertIn("警告: MEMORY.md が 201 行", self.text())

    def test_link_key_forms(self):
        self.assertEqual(memsync.line_key("- [A](./a.md) - x"), "a.md")
        self.assertEqual(memsync.line_key("- [A](a.md#sec) - x"), "a.md")
        self.assertIsNone(memsync.line_key("# 見出し"))
        self.assertIsNone(memsync.line_key("- [web](https://example.com/page)"))


class StatusAndCliTest(MemsyncTestCase):
    def test_status_labels_and_no_writes(self):
        self.write("same.md", "s\n")
        self.s3.put("same.md", "s\n")
        self.write("localonly.md", "l\n")
        self.s3.put("remoteonly.md", "r\n")
        self.write("localnew.md", "new\n")
        self.s3.put("localnew.md", "old\n")
        self.write("remotenew.md", "old\n")
        self.s3.put("remotenew.md", "new\n")
        self.write("conflict.md", "l\n")
        self.s3.put("conflict.md", "r\n")
        self.write("private.md", "---\nshare: false\n---\n")
        self.state.parent.mkdir(parents=True)
        self.state.write_text(json.dumps({"files": {"localnew.md": md5(b"old\n"), "remotenew.md": md5(b"old\n"),
                                                    "conflict.md": md5(b"b\n")}}), encoding="utf-8")
        before = self.state.read_text(encoding="utf-8")
        self.sync().status()
        text = self.text()
        for name, label in (("same.md", "同期済み"), ("localonly.md", "手元だけ"), ("remoteonly.md", "S3 だけ"),
                            ("localnew.md", "手元が新しい"), ("remotenew.md", "S3 が新しい"), ("conflict.md", "競合"),
                            ("private.md", "送らない")):
            self.assertRegex(text, r"(?m)^%s +%s" % (name.replace(".", r"\."), label))
        self.assertEqual(self.state.read_text(encoding="utf-8"), before)
        self.assertFalse(any(c[:2] == ["s3", "cp"] for c in self.s3.calls))

    def test_access_denied_has_next_action(self):
        self.s3.fail = "An error occurred (AccessDenied) when calling the ListObjectsV2 operation: Access Denied"
        err = io.StringIO()
        with redirect_stderr(err):
            rc = memsync.main(["pull", "--dir", str(self.dir), "--uri", URI, "--state", str(self.state)], runner=self.s3,
                              out=self.lines.append)
        self.assertEqual(rc, 1)
        self.assertIn("memory/ の権限がありません", err.getvalue())
        self.assertIn("mvm infra deploy", err.getvalue())

    def test_other_aws_failure_is_nonzero(self):
        self.s3.fail = "Could not connect to the endpoint URL"
        err = io.StringIO()
        with redirect_stderr(err):
            rc = memsync.main(["status", "--dir", str(self.dir), "--uri", URI, "--state", str(self.state)], runner=self.s3,
                              out=self.lines.append)
        self.assertEqual(rc, 1)
        self.assertIn("もう一度", err.getvalue())

    def test_cli_runs_push_and_pull(self):
        self.write("a.md", "a\n")
        args = ["--dir", str(self.dir), "--uri", URI, "--state", str(self.state), "--origin", "vm-1"]
        self.assertEqual(memsync.main(["push", *args], runner=self.s3, out=self.lines.append), 0)
        self.assertEqual(self.s3.get("a.md"), b"a\n")
        (self.dir / "a.md").unlink()
        self.assertEqual(memsync.main(["pull", *args, "--dry-run"], runner=self.s3, out=self.lines.append), 0)
        self.assertFalse((self.dir / "a.md").exists())
        self.assertEqual(memsync.main(["pull", *args], runner=self.s3, out=self.lines.append), 0)
        self.assertFalse((self.dir / "a.md").exists(), "手元で消したファイルは取得しない")
        self.state.unlink()
        self.assertEqual(memsync.main(["pull", *args], runner=self.s3, out=self.lines.append), 0)
        self.assertEqual(self.read("a.md"), b"a\n")

    def test_state_directory_is_created(self):
        self.write("a.md", "a\n")
        self.sync().push()
        self.assertTrue(self.state.is_file())


class ImageWiringTest(unittest.TestCase):
    """VM に memsync.py を入れる配線 (Dockerfile、zip の実行権限、bootstrap.sh / snapshot.sh からの呼び出し)。"""

    def read(self, rel):
        return (ROOT / rel).read_bytes().decode("utf-8")

    def test_installed_and_executable(self):
        script = self.read("image/memsync.py")
        self.assertTrue(script.startswith("#!/usr/bin/env python3" + chr(10)))
        self.assertNotIn(chr(13), script, "image/ のスクリプトは LF")
        dockerfile = self.read("image/Dockerfile")
        self.assertRegex(dockerfile, r"(?m)^COPY .*\bmemsync\.py\b.* /opt/pmva/$")
        self.assertIn("/opt/pmva/memsync.py", dockerfile.split("chmod 0755 /opt/pmva/supervisor.py", 1)[1].split("&&", 1)[0])
        self.assertIn('"memsync.py"', self.read("bin/mvm.py").split("executables = ", 1)[1].split(chr(10), 1)[0])

    def test_bootstrap_and_snapshot_use_it(self):
        bootstrap, snapshot = self.read("image/bootstrap.sh"), self.read("image/snapshot.sh")
        self.assertIn("memsync.py pull", bootstrap)
        self.assertIn("autoMemoryDirectory", bootstrap)
        self.assertIn("memsync.py push", snapshot)
        for script in (bootstrap, snapshot):
            self.assertIn("pmva-memory.state.json", script)


if __name__ == "__main__":
    unittest.main()
