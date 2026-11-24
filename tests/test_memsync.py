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
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "image"))
import memsync  # noqa: E402

T0 = datetime(2026, 11, 24, 10, 15, 3, tzinfo=timezone.utc)     # テストの「今」 (履歴の時刻)
STAMP0 = "20261124T101503Z"
BUCKET = "bkt"
PREFIX = "memory/github.com/owner/repo/"
URI = f"s3://{BUCKET}/{PREFIX}"


def md5(data):
    return hashlib.md5(data).hexdigest()


PRECONDITION_FAILED = ("An error occurred (PreconditionFailed) when calling the PutObject operation: "
                       "At least one of the pre-conditions you specified did not hold")
TRANSFERS = (["s3", "cp"], ["s3api", "put-object"], ["s3api", "get-object"])


class FakeS3:
    """memsync が呼ぶ aws の代わり (list-objects-v2、put-object、get-object、s3 cp)。

    put-object は --if-match / --if-none-match を本物の S3 と同じく確かめ、合わなければ PreconditionFailed で失敗する。
    """

    def __init__(self):
        self.objects = {}      # キー → 中身
        self.metadata = {}     # キー → --metadata の値
        self.etags = {}        # キー → ETag の上書き
        self.calls = []
        self.fail = None       # 設定すると全部の呼び出しがこのエラー文で失敗する
        self.page_size = 0     # 0 以外なら list-objects-v2 をこの件数ずつに分けて返す
        self.before_put = None  # put-object の直前に呼ぶ関数 (キーを渡す)。他のマシンの同時書き込みを作る
        self.old_cli = False   # True なら --if-match / --if-none-match を知らない古い AWS CLI として振る舞う
        self.deny_delete = False  # True なら delete-object が AccessDenied で失敗する (VM の実行ロールと同じ)

    def put(self, name, data, prefix=PREFIX):
        self.objects[prefix + name] = data if isinstance(data, bytes) else data.encode("utf-8")

    def get(self, name, prefix=PREFIX):
        return self.objects.get(prefix + name)

    def etag(self, key):
        return self.etags.get(key, md5(self.objects[key]))

    def __call__(self, args):
        self.calls.append(list(args))
        if self.fail:
            return 254, "", self.fail

        def opt(k):
            return args[args.index(k) + 1] if k in args else None
        if self.old_cli and ("--if-match" in args or "--if-none-match" in args):
            bad = [x for x in args if x in ("--if-match", "--if-none-match")]
            return 252, "", "\nusage: aws [options]\naws: error: Unknown options: %s\n" % ", ".join(bad)
        if args[:2] == ["s3api", "put-object"]:
            key = opt("--key")
            if self.before_put:
                self.before_put(key)
            if opt("--if-none-match") == "*" and key in self.objects:
                return 254, "", PRECONDITION_FAILED
            if "--if-match" in args and (key not in self.objects or self.etag(key) != opt("--if-match").strip('"')):
                return 254, "", PRECONDITION_FAILED
            self.objects[key] = Path(opt("--body")).read_bytes()
            self.etags.pop(key, None)
            if "--metadata" in args:
                self.metadata[key] = opt("--metadata")
            return 0, json.dumps({"ETag": '"%s"' % self.etag(key)}), ""
        if args[:2] == ["s3api", "delete-object"]:
            if self.deny_delete:
                return 254, "", "An error occurred (AccessDenied) when calling the DeleteObject operation: Access Denied"
            self.objects.pop(opt("--key"), None)
            self.metadata.pop(opt("--key"), None)
            return 0, "", ""
        if args[:2] == ["s3api", "get-object"]:
            key = opt("--key")
            if key not in self.objects:
                return 254, "", "An error occurred (NoSuchKey) when calling the GetObject operation: The specified key does not exist."
            Path(args[-1]).write_bytes(self.objects[key])
            return 0, json.dumps({"ETag": '"%s"' % self.etag(key), "ContentLength": len(self.objects[key])}), ""
        if args[:2] == ["s3api", "list-objects-v2"]:
            prefix = args[args.index("--prefix") + 1]
            keys = sorted(k for k in self.objects if k.startswith(prefix))
            if opt("--delimiter"):      # 本物の S3 と同じく、prefix の後ろに delimiter を含むキーは Contents に入れない
                keys = [k for k in keys if opt("--delimiter") not in k[len(prefix):]]
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
        self.clock = [T0]

    def sync(self, **kw):
        kw.setdefault("origin", "local")
        kw.setdefault("now", lambda: self.clock[0])
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
        self.assertFalse(any(c[:2] in TRANSFERS for c in self.s3.calls))

    def test_crlf_is_normalized_before_sending_and_comparing(self):
        self.write("a.md", b"one\r\ntwo\r\n")
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"one\ntwo\n")
        self.assertEqual(self.get_state()["a.md"], md5(b"one\ntwo\n"))
        self.s3.calls.clear()
        self.sync().push()
        self.assertFalse(any(c[:2] in TRANSFERS for c in self.s3.calls), "改行だけの違いは同じ内容")

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
        self.assertFalse(any(c[:2] in TRANSFERS for c in self.s3.calls))

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


class ConditionalWriteTest(MemsyncTestCase):
    """同時に書き込んだときに、他のマシンの書き込みを黙って上書きしない (S3 の条件付き書き込み)。"""

    def puts(self, name):
        return [c for c in self.s3.calls if c[:2] == ["s3api", "put-object"] and c[c.index("--key") + 1] == PREFIX + name]

    def test_new_file_is_created_only_if_absent(self):
        self.write("a.md", "a\n")
        self.sync().push()
        (put,) = self.puts("a.md")
        self.assertEqual(put[put.index("--if-none-match") + 1], "*")
        self.assertNotIn("--if-match", put)

    def test_update_requires_the_listed_etag(self):
        self.s3.put("a.md", "old\n")
        self.set_state(a=md5(b"old\n"))
        self.write("a.md", "new\n")
        self.sync().push()
        (put,) = self.puts("a.md")
        self.assertEqual(put[put.index("--if-match") + 1], md5(b"old\n"))
        self.assertEqual(self.s3.get("a.md"), b"new\n")

    def test_file_changed_by_another_machine_meanwhile_is_a_conflict(self):
        self.s3.put("a.md", "old\n")
        self.set_state(a=md5(b"old\n"))
        self.write("a.md", "mine\n")
        self.s3.before_put = lambda key: self.s3.put("a.md", "theirs\n") if key.endswith("a.md") else None
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"theirs\n", "他のマシンの書き込みを上書きしない")
        self.assertEqual(self.get_state()["a.md"], md5(b"old\n"), "基準は進めない (次の pull で競合として見える)")
        self.assertIn("競合 1", self.text())
        self.assertIn("a.md", self.text())

    def test_new_file_created_by_another_machine_meanwhile_is_a_conflict(self):
        self.write("a.md", "mine\n")
        self.s3.before_put = lambda key: self.s3.put("a.md", "theirs\n") if key.endswith("a.md") and not self.s3.get("a.md") else None
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"theirs\n")
        self.assertNotIn("a.md", self.get_state())
        self.assertIn("競合 1", self.text())

    def test_index_is_merged_again_when_another_machine_updated_it(self):
        self.s3.put("b.md", "b\n")
        self.s3.put("MEMORY.md", "- [B](b.md) - b\n")
        self.write("a.md", "a\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n")
        raced = []

        def race(key):
            if key.endswith("MEMORY.md") and not raced:
                raced.append(key)
                self.s3.put("c.md", "c\n")
                self.s3.put("MEMORY.md", "- [B](b.md) - b\n- [C](c.md) - c\n")
        self.s3.before_put = race
        self.sync().push()
        index = self.s3.get("MEMORY.md").decode("utf-8")
        self.assertIn("(b.md)", index)
        self.assertIn("(c.md)", index, "他のマシンが足した行を消さない")
        self.assertIn("(a.md)", index)
        self.assertEqual(len(self.puts("MEMORY.md")), 2, "取り直して送り直す")

    def test_index_gives_up_after_repeated_races(self):
        self.write("a.md", "a\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n")
        self.s3.put("MEMORY.md", "- [X](x.md) - x\n")
        counter = []

        def race(key):
            if key.endswith("MEMORY.md"):
                counter.append(1)
                self.s3.put("MEMORY.md", "- [X](x.md) - x\n" * (len(counter) + 1))
        self.s3.before_put = race
        self.sync().push()
        self.assertLessEqual(len(self.puts("MEMORY.md")), 5)
        self.assertIn("MEMORY.md", self.text())
        self.assertIn("もう一度", self.text())

    def test_old_cli_without_conditional_options_still_sends(self):
        self.s3.old_cli = True
        self.write("a.md", "a\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n")
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"a\n")
        self.assertIn("(a.md)", self.s3.get("MEMORY.md").decode("utf-8"))
        self.assertIn("AWS CLI", self.text(), "同時書き込みを検出できないことを伝える")


class HookTest(MemsyncTestCase):
    """Claude Code のフックから呼ぶ同期 (memsync.run_hook)。"""

    def setUp(self):
        super().setUp()
        self.now = 1000.0

    def hook(self, event, payload=None, min_interval=0):
        text = json.dumps(payload if payload is not None else {"hook_event_name": event, "cwd": str(self.dir)})
        return memsync.run_hook(event, self.sync(), text, min_interval=min_interval, now=lambda: self.now)

    def context(self, output):
        data = json.loads(output)
        return data["hookSpecificOutput"]["additionalContext"]

    def test_write_into_memory_folder_is_pushed(self):
        self.write("a.md", "a\n")
        out = self.hook("post-tool", {"tool_name": "Write", "tool_input": {"file_path": str(self.dir / "a.md")}})
        self.assertEqual(out, "")
        self.assertEqual(self.s3.get("a.md"), b"a\n")

    def test_windows_style_path_is_matched(self):
        self.write("a.md", "a\n")
        path = str(self.dir / "a.md").replace("/", "\\") if os.name == "nt" else str(self.dir / "." / "a.md")
        self.hook("post-tool", {"tool_name": "Edit", "tool_input": {"file_path": path}})
        self.assertEqual(self.s3.get("a.md"), b"a\n")

    def test_write_outside_memory_folder_does_nothing(self):
        other = Path(self.tmp.name) / "src.md"
        other.write_text("x\n", encoding="utf-8")
        self.write("a.md", "a\n")
        for path in (str(other), str(self.dir / "sub" / "a.md"), str(self.dir / "a.txt"), ""):
            self.assertEqual(self.hook("post-tool", {"tool_name": "Write", "tool_input": {"file_path": path}}), "")
        self.hook("post-tool", {"tool_name": "Bash", "tool_input": {"command": "ls"}})
        self.assertEqual(self.s3.calls, [], "メモリ以外の書き込みでは AWS を呼ばない")

    def test_session_start_pulls_and_tells_what_arrived(self):
        self.s3.put("project_deploy.md", "---\nname: deploy\n---\nデプロイは金曜にしない\n")
        self.s3.put("MEMORY.md", "- [Deploy](project_deploy.md) - 金曜は避ける\n")
        ctx = self.context(self.hook("session-start"))
        self.assertEqual(json.loads(self.hook("session-start") or '{"x": 1}').get("x"), 1, "2 回目は何も届かない")
        self.assertIn("デプロイは金曜にしない", ctx)
        self.assertIn("project_deploy.md", ctx)
        self.assertIn(str(self.dir), ctx)
        self.assertEqual(self.read("project_deploy.md"), "---\nname: deploy\n---\nデプロイは金曜にしない\n".encode("utf-8"))

    def test_hook_event_name_matches_claude_code(self):
        self.s3.put("a.md", "a\n")
        self.assertEqual(json.loads(self.hook("prompt"))["hookSpecificOutput"]["hookEventName"], "UserPromptSubmit")
        self.s3.put("b.md", "b\n")
        self.assertEqual(json.loads(self.hook("session-start"))["hookSpecificOutput"]["hookEventName"], "SessionStart")

    def test_prompt_with_nothing_new_outputs_nothing(self):
        self.assertEqual(self.hook("prompt"), "")
        self.assertTrue(any(c[:2] == ["s3api", "list-objects-v2"] for c in self.s3.calls))

    def test_prompt_is_throttled(self):
        self.s3.put("a.md", "a\n")
        self.assertIn("a.md", self.context(self.hook("prompt", min_interval=60)))
        self.s3.calls.clear()
        self.s3.put("b.md", "b\n")
        self.now += 30
        self.assertEqual(self.hook("prompt", min_interval=60), "")
        self.assertEqual(self.s3.calls, [], "間隔が短いときは S3 を見に行かない")
        self.now += 31
        self.assertIn("b.md", self.context(self.hook("prompt", min_interval=60)))

    def test_context_is_kept_under_the_limit(self):
        for i in range(8):
            self.s3.put("big%d.md" % i, ("x" * 2000 + "\n"))
        ctx = self.context(self.hook("session-start"))
        self.assertLessEqual(len(ctx), memsync.HOOK_CONTEXT_MAX)
        self.assertIn("big7.md", ctx, "入りきらなかったファイルも名前は伝える")
        self.assertIn("読んで", ctx)

    def test_updated_file_is_reported(self):
        self.s3.put("a.md", "v1\n")
        self.hook("session-start")
        self.s3.put("a.md", "v2\n")
        self.assertIn("v2", self.context(self.hook("prompt")))

    def test_conflicts_are_reported_at_session_start(self):
        self.s3.put("a.md", "theirs\n")
        self.write("a.md", "mine\n")
        ctx = self.context(self.hook("session-start"))
        self.assertIn("a.md", ctx)
        self.assertIn("競合", ctx)
        self.assertEqual(self.read("a.md"), b"mine\n", "フックでは --force しない")

    def test_failure_never_breaks_the_session(self):
        self.s3.fail = "Could not connect to the endpoint URL"
        for event in ("session-start", "prompt"):
            self.assertEqual(self.hook(event), "")
        self.write("a.md", "a\n")
        self.assertEqual(self.hook("post-tool", {"tool_input": {"file_path": str(self.dir / "a.md")}}), "")
        log = Path(str(self.state) + ".log").read_text(encoding="utf-8")
        self.assertIn("Could not connect", log)

    def test_broken_input_is_ignored(self):
        self.assertEqual(memsync.run_hook("post-tool", self.sync(), "not json", now=lambda: self.now), "")
        self.assertEqual(memsync.run_hook("post-tool", self.sync(), "[1]", now=lambda: self.now), "")
        self.assertEqual(self.s3.calls, [])

    def test_busy_lock_skips_instead_of_waiting_forever(self):
        self.s3.put("a.md", "a\n")
        lock = Path(str(self.state) + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("other", encoding="utf-8")
        self.assertEqual(memsync.run_hook("prompt", self.sync(), "{}", now=lambda: self.now, lock_wait=0.2), "")
        self.assertEqual(self.s3.calls, [], "他の同期が動いている間は何もしない")
        self.assertTrue(lock.exists(), "他の同期のロックは消さない")
        os.utime(str(lock), (0, 0))      # 古いロック (同期の途中で落ちた) は奪う
        self.assertIn("a.md", self.context(memsync.run_hook("prompt", self.sync(), "{}", now=time.time, lock_wait=0.2)))
        self.assertFalse(lock.exists())

    def test_cli_hook_sets_region(self):
        # VM のエージェントの環境には AWS_REGION が無いことがあるので、フックのコマンドで渡す
        seen = []

        def runner(args):
            seen.append((os.environ.get("AWS_REGION"), os.environ.get("AWS_DEFAULT_REGION")))
            return self.s3(args)
        args = ["hook", "prompt", "--region", "ap-northeast-1", "--dir", str(self.dir), "--uri", URI, "--state", str(self.state)]
        with mock.patch.dict(os.environ, {}, clear=False), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            os.environ.pop("AWS_REGION", None)
            self.assertEqual(memsync.main(args, runner=runner, stdin=io.StringIO("{}")), 0)
        self.assertEqual(seen[0], ("ap-northeast-1", "ap-northeast-1"))

    def test_push_waits_longer_for_the_lock(self):
        # 送信は飛ばすと次に書くまで送られないので、取得より長く待つ
        self.write("a.md", "a\n")
        lock = Path(str(self.state) + ".lock")
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text("other", encoding="utf-8")
        timer = threading.Timer(0.5, lock.unlink)
        timer.start()
        self.addCleanup(timer.cancel)
        event = json.dumps({"tool_input": {"file_path": str(self.dir / "a.md")}})
        self.assertEqual(memsync.run_hook("post-tool", self.sync(), event, now=lambda: self.now, lock_wait=0.2), "")
        self.assertEqual(self.s3.get("a.md"), b"a\n")

    def test_cli_hook_writes_only_the_hook_response_to_stdout(self):
        self.s3.put("a.md", "a\n")
        args = ["hook", "prompt", "--dir", str(self.dir), "--uri", URI, "--state", str(self.state), "--origin", "vm-1"]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = memsync.main(args, runner=self.s3, stdin=io.StringIO("{}"))
        self.assertEqual(rc, 0)
        self.assertIn("a.md", json.loads(out.getvalue())["hookSpecificOutput"]["additionalContext"])
        self.assertIn("取得", err.getvalue(), "経過は標準エラーに出す (標準出力は会話に入る)")
        self.s3.fail = "boom"
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(io.StringIO()):
            self.assertEqual(memsync.main(args[:2] + ["--min-interval", "0"] + args[2:], runner=self.s3,
                                          stdin=io.StringIO("{}")), 0, "失敗しても 0 で終わる")
        self.assertEqual(out.getvalue(), "")


def history_keys(s3, name=None):
    """S3 にある履歴のキー (prefix より後)。"""
    start = PREFIX + ".history/" + (name + "/" if name else "")
    return sorted(k[len(PREFIX):] for k in s3.objects if k.startswith(start))


def put_calls(s3, name):
    return [c for c in s3.calls if c[:2] == ["s3api", "put-object"] and c[c.index("--key") + 1] == PREFIX + name]


def call_index(s3, op, name):
    """op ("put-object" など) で name を指す最初の呼び出しの位置。"""
    for i, c in enumerate(s3.calls):
        if c[:2] == ["s3api", op] and c[c.index("--key") + 1] == PREFIX + name:
            return i
    raise AssertionError("%s %s の呼び出しがありません" % (op, name))


class HistoryTest(MemsyncTestCase):
    """送るたびの履歴 (.history/<名前>/<時刻>-<送り元>-<md5 先頭 8 文字>.md)。"""

    def test_push_leaves_a_copy_with_key_and_metadata(self):
        self.write("a.md", "mine\n")
        self.sync(origin="agent-01").push()
        key = "%s.history/a.md/%s-agent-01-%s.md" % (PREFIX, STAMP0, md5(b"mine\n")[:8])
        self.assertEqual(self.s3.objects[key], b"mine\n")
        self.assertEqual(self.s3.metadata[key], "pmva-origin=agent-01")
        (put,) = put_calls(self.s3, ".history/a.md/%s-agent-01-%s.md" % (STAMP0, md5(b"mine\n")[:8]))
        self.assertNotIn("--if-none-match", put, "履歴は条件なしで書く")
        self.assertNotIn("--if-match", put)

    def test_each_change_adds_a_version_and_unchanged_adds_none(self):
        self.write("a.md", "v1\n")
        self.sync().push()
        self.clock[0] = datetime(2026, 11, 24, 11, 0, 0, tzinfo=timezone.utc)
        self.write("a.md", "v2\n")
        self.sync().push()
        self.sync().push()
        names = history_keys(self.s3, "a.md")
        self.assertEqual(len(names), 2)
        self.assertTrue(names[0].startswith(".history/a.md/20261124T101503Z-local-"))
        self.assertTrue(names[1].startswith(".history/a.md/20261124T110000Z-local-"))

    def test_index_is_not_recorded(self):
        self.write("a.md", "a\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n")
        self.sync().push()
        self.assertEqual(history_keys(self.s3), [history_keys(self.s3, "a.md")[0]])
        self.assertIsNotNone(self.s3.get("MEMORY.md"))

    def test_dry_run_and_skipped_files_write_no_history(self):
        self.write("a.md", "mine\n")
        self.write("secret.md", "ghp_" + "A" * 36 + "\n")
        self.sync(dry_run=True).push()
        self.assertEqual(self.s3.objects, {})
        self.sync().push()
        self.assertEqual(history_keys(self.s3), [history_keys(self.s3, "a.md")[0]], "送らなかったファイルの履歴は残さない")

    def test_conflict_writes_no_history(self):
        self.s3.put("a.md", "old\n")
        self.set_state(a=md5(b"old\n"))
        self.write("a.md", "mine\n")
        self.s3.before_put = lambda key: self.s3.put("a.md", "theirs\n") if key.endswith("/a.md") else None
        self.sync().push()
        self.assertEqual(history_keys(self.s3), [])

    def test_history_failure_does_not_stop_the_sync(self):
        def runner(args):
            if ".history/" in " ".join(args):
                return 254, "", "An error occurred (AccessDenied) when calling the PutObject operation: Access Denied"
            return self.s3(args)
        self.write("a.md", "a\n")
        self.write("b.md", "b\n")
        memsync.Memsync(self.dir, URI, self.state, runner=runner, out=self.lines.append).push()
        self.assertEqual(self.s3.get("a.md"), b"a\n")
        self.assertEqual(self.s3.get("b.md"), b"b\n", "履歴が失敗しても次のファイルを送る")
        self.assertEqual(self.get_state(), {"a.md": md5(b"a\n"), "b.md": md5(b"b\n")})
        self.assertIn("警告: a.md の履歴を残せませんでした", self.text())
        self.assertIn("送信 2", self.text())

    def test_parse_history_key_with_dashes_in_origin(self):
        e = memsync.parse_history_key(".history/日本語.md/20261124T101503Z-vm-agent-01-abcd1234.md")
        self.assertEqual((e["origin"], e["name"], e["md5"], e["at"]), ("vm-agent-01", "日本語.md", "abcd1234", T0))
        for bad in (".history/a.md/notatime-local-abcd1234.md", ".history/a.md/20261124T101503Z-local-xyz.md",
                    ".history/a.md/sub/20261124T101503Z-local-abcd1234.md", "a.md", ".history/20261124T101503Z-x-abcd1234.md"):
            self.assertIsNone(memsync.parse_history_key(bad), bad)

    def test_list_remote_does_not_fetch_history_keys(self):
        for i in range(5):
            self.s3.put(".history/a.md/2026112%dT000000Z-local-%08x.md" % (i, i), "x\n")
        self.s3.put("a.md", "a\n")
        self.s3.put(".deleted.json", json.dumps({"files": {}}))
        sync = self.sync()
        self.assertEqual(sync.list_remote(), {"a.md": md5(b"a\n")})
        (call,) = [c for c in self.s3.calls if c[:2] == ["s3api", "list-objects-v2"]]
        self.assertEqual(call[call.index("--delimiter") + 1], "/")
        self.assertEqual(call[call.index("--prefix") + 1], PREFIX)
        self.assertTrue(sync._deleted_etag, ".deleted.json は直下なので見つかる")
        self.s3.calls.clear()
        self.sync().history_log()
        listing = [c for c in self.s3.calls if c[:2] == ["s3api", "list-objects-v2"]]
        self.assertTrue(listing and all("--delimiter" not in c for c in listing), "履歴の一覧には付けない")

    def test_history_and_marks_do_not_disturb_pull(self):
        self.s3.put("a.md", "a\n")
        self.s3.put(".history/a.md/%s-local-%s.md" % (STAMP0, md5(b"a\n")[:8]), "a\n")
        self.s3.put(".deleted.json", json.dumps({"files": {}}))
        self.sync().pull()
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["a.md"])
        self.assertIn("取得 1、送信 0、競合 0、飛ばした 0", self.text())

    def test_old_s3_without_history_or_marks_works_as_before(self):
        self.s3.put("a.md", "a\n")
        self.s3.put("MEMORY.md", "- [A](a.md) - a\n")
        self.sync().pull()
        self.assertEqual(self.read("a.md"), b"a\n")
        self.write("b.md", "b\n")
        self.sync().push()
        self.lines.clear()
        self.sync().status()
        self.assertRegex(self.text(), r"(?m)^a\.md +同期済み")
        self.sync().history_log("a.md")
        self.assertIn("履歴がありません", self.text())
        with self.assertRaises(memsync.MemsyncError) as cm:
            self.sync().revert("a.md")
        self.assertIn("履歴がありません", str(cm.exception))


class DeletedMarkTest(MemsyncTestCase):
    """rm の削除の印 (.deleted.json) を pull / push / status / フックがどう扱うか。"""

    def mark(self, name, data, line=None, by="pc", at="2026-11-24T09:00:00Z"):
        raw = self.s3.get(".deleted.json")
        files = json.loads(raw.decode("utf-8"))["files"] if raw else {}
        data = data if isinstance(data, bytes) else data.encode("utf-8")
        files.setdefault(name, []).insert(0, {"md5": md5(data), "at": at, "by": by, "line": line})
        self.s3.put(".deleted.json", json.dumps({"files": files}))

    def test_pull_removes_memory_deleted_elsewhere(self):
        self.write("a.md", "old\n")
        self.write("b.md", "keep\n")
        self.write("MEMORY.md", "# 索引\n- [A](a.md) - a\n- [B](b.md) - b\n")
        self.set_state(a=md5(b"old\n"), b=md5(b"keep\n"))
        self.s3.put("b.md", "keep\n")
        self.mark("a.md", "old\n", line="- [A](a.md) - a")
        sync = self.sync()
        sync.pull()
        self.assertFalse((self.dir / "a.md").exists())
        self.assertTrue((self.dir / "b.md").exists())
        self.assertEqual(self.read("MEMORY.md").decode("utf-8"), "# 索引\n- [B](b.md) - b\n")
        self.assertEqual(self.get_state(), {"b.md": md5(b"keep\n")})
        self.assertEqual(sync.deleted, ["a.md"])
        self.assertIn("他のマシンで消されたメモリ: a.md", self.text())
        self.assertIn("取得 0、送信 0、競合 0、飛ばした 0、削除 1", self.text())

    def test_pull_removes_when_s3_still_has_the_deleted_content(self):
        self.write("a.md", "old\n")
        self.s3.put("a.md", "old\n")        # 古い VM が送り直した
        self.mark("a.md", "old\n")
        sync = self.sync()
        sync.pull()
        self.assertFalse((self.dir / "a.md").exists())
        self.assertIn("削除 1", self.text())
        self.assertEqual(self.s3.get("a.md"), b"old\n", "S3 には触らない")

    def test_pull_does_not_fetch_deleted_content_that_was_sent_again(self):
        self.s3.put("a.md", "old\n")
        self.mark("a.md", "old\n")
        self.set_state(a=md5(b"old\n"))
        self.s3.put("MEMORY.md", "- [A](a.md) - a\n")
        self.sync().pull()
        self.assertFalse((self.dir / "a.md").exists())
        self.assertFalse((self.dir / "MEMORY.md").exists())
        self.assertIn("取得しません", self.text())
        self.assertIn("飛ばした 1", self.text())
        self.assertNotIn("a.md", self.get_state())

    def test_pull_leaves_a_file_rewritten_after_the_deletion(self):
        self.write("a.md", "rewritten\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n")
        self.set_state(a=md5(b"old\n"))
        self.mark("a.md", "old\n")
        sync = self.sync()
        sync.pull()
        self.assertEqual(self.read("a.md"), b"rewritten\n")
        self.assertEqual(self.read("MEMORY.md"), b"- [A](a.md) - a\n")
        self.assertEqual(sync.deleted, [])
        self.assertNotIn("削除", self.text())

    def test_pull_fetches_content_recreated_after_the_deletion(self):
        self.write("a.md", "old\n")
        self.set_state(a=md5(b"old\n"))
        self.mark("a.md", "old\n")
        self.s3.put("a.md", "new\n")           # 消した後に、別のマシンが作り直した
        self.sync().pull()
        self.assertEqual(self.read("a.md"), b"new\n")

    def test_pull_does_not_delete_share_false_files(self):
        data = "---\nshare: false\n---\nmine\n"
        self.write("a.md", data)
        self.mark("a.md", normalize(data))
        self.sync().pull()
        self.assertTrue((self.dir / "a.md").exists())
        self.assertIn("share: false", self.text())

    def test_pull_dry_run_changes_nothing(self):
        self.write("a.md", "old\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n")
        self.set_state(a=md5(b"old\n"))
        self.mark("a.md", "old\n")
        self.sync(dry_run=True).pull()
        self.assertEqual(self.read("a.md"), b"old\n")
        self.assertEqual(self.read("MEMORY.md"), b"- [A](a.md) - a\n")
        self.assertIn("[dry-run] 他のマシンで消されたメモリ: a.md", self.text())
        self.assertIn("[dry-run] MEMORY.md から", self.text())
        self.assertEqual(self.get_state(), {"a.md": md5(b"old\n")})

    def test_broken_marks_are_warned_and_ignored(self):
        self.s3.put(".deleted.json", "{broken")
        self.s3.put("a.md", "a\n")
        self.sync().pull()
        self.assertEqual(self.read("a.md"), b"a\n")
        self.assertIn("警告", self.text())
        self.assertIn(".deleted.json", self.text())

    def test_push_does_not_send_deleted_content_or_its_index_line(self):
        self.write("a.md", "old\n")
        self.write("b.md", "b\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n- [B](b.md) - b\n")
        self.s3.put("a.md", "old\n")            # 古い VM が送り直した内容
        self.s3.put("b.md", "b\n")
        self.s3.put("MEMORY.md", "- [B](b.md) - b\n")
        self.mark("a.md", "old\n")
        self.sync().push()
        self.assertIn("mvm memory rm で消されたメモリなので送りません", self.text())
        self.assertIn("飛ばした 1", self.text())
        self.assertEqual(put_calls(self.s3, "MEMORY.md"), [], "索引の行も送らない")
        self.assertEqual(history_keys(self.s3), [])

    def test_push_does_not_resend_after_s3_copy_was_removed(self):
        self.write("a.md", "old\n")
        self.set_state(a=md5(b"old\n"))
        self.mark("a.md", "old\n")
        self.sync().push()
        self.assertIsNone(self.s3.get("a.md"))
        self.assertIn("飛ばした 1", self.text())

    def test_push_sends_a_rewritten_file(self):
        self.write("a.md", "rewritten\n")
        self.mark("a.md", "old\n")
        self.sync().push()
        self.assertEqual(self.s3.get("a.md"), b"rewritten\n")

    def test_status_shows_deleted(self):
        self.write("a.md", "old\n")
        self.write("b.md", "b\n")
        self.mark("a.md", "old\n")
        self.sync().status()
        self.assertRegex(self.text(), r"(?m)^a\.md +消された \(mvm memory rm\)")
        self.assertRegex(self.text(), r"(?m)^b\.md +手元だけ")

    def test_hook_tells_the_conversation_about_deleted_memories(self):
        self.write("a.md", "old\n")
        self.set_state(a=md5(b"old\n"))
        self.mark("a.md", "old\n")
        out = memsync.run_hook("session-start", self.sync(), "{}")
        self.assertTrue(out.isascii(), "ASCII だけの JSON")
        ctx = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("他のマシンで消されたメモリ: a.md (手元からも消しました。この記憶には頼らないでください)", ctx)
        self.assertLessEqual(len(ctx), memsync.HOOK_CONTEXT_MAX)
        self.assertFalse((self.dir / "a.md").exists())
        self.assertEqual(memsync.run_hook("session-start", self.sync(), "{}"), "", "2 回目は何も伝えない")

    def test_hook_context_stays_under_the_limit_with_many_deleted(self):
        for i in range(120):
            self.write("n%03d-%s.md" % (i, "x" * 20), "old\n")
            self.mark("n%03d-%s.md" % (i, "x" * 20), "old\n")
        ctx = json.loads(memsync.run_hook("session-start", self.sync(), "{}"))["hookSpecificOutput"]["additionalContext"]
        self.assertLessEqual(len(ctx), memsync.HOOK_CONTEXT_MAX)
        self.assertIn("ほか", ctx)


class RmTest(MemsyncTestCase):
    def marks(self):
        return json.loads(self.s3.get(".deleted.json").decode("utf-8"))["files"]

    def test_rm_marks_then_deletes_everywhere_in_order(self):
        self.write("a.md", "local\n")
        self.s3.put("a.md", "remote\n")
        self.set_state(a=md5(b"base\n"))
        self.write("MEMORY.md", "- [A](a.md) - local line\n- [B](b.md) - b\n")
        self.s3.put("MEMORY.md", "- [A](a.md) - s3 line\n- [B](b.md) - b\n")
        self.assertEqual(self.sync(origin="pc").rm("a.md"), 0)
        entries = self.marks()["a.md"]
        self.assertEqual([e["md5"] for e in entries], [md5(b"local\n"), md5(b"remote\n"), md5(b"base\n")])
        for e in entries:
            self.assertEqual((e["at"], e["by"], e["line"]), ("2026-11-24T10:15:03Z", "pc", "- [A](a.md) - s3 line"))
        order = [call_index(self.s3, "put-object", ".deleted.json"), call_index(self.s3, "delete-object", "a.md"),
                 call_index(self.s3, "put-object", "MEMORY.md")]
        self.assertEqual(order, sorted(order), "印 → S3 のファイル → S3 の索引")
        self.assertIsNone(self.s3.get("a.md"))
        self.assertEqual(self.s3.get("MEMORY.md"), b"- [B](b.md) - b\n")
        self.assertFalse((self.dir / "a.md").exists())
        self.assertEqual(self.read("MEMORY.md"), b"- [B](b.md) - b\n")
        self.assertEqual(self.get_state(), {})
        self.assertEqual(self.s3.metadata[PREFIX + ".deleted.json"], "pmva-origin=pc")

    def test_first_mark_is_created_only_if_absent_and_later_ones_are_conditional(self):
        self.write("a.md", "a\n")
        self.sync().rm("a.md")
        (put,) = put_calls(self.s3, ".deleted.json")
        self.assertEqual(put[put.index("--if-none-match") + 1], "*")
        etag = md5(self.s3.get(".deleted.json"))
        self.write("b.md", "b\n")
        self.sync().rm("b.md")
        self.assertEqual(put_calls(self.s3, ".deleted.json")[1][put_calls(self.s3, ".deleted.json")[1].index("--if-match") + 1], etag)
        self.assertEqual(set(self.marks()), {"a.md", "b.md"})

    def test_line_falls_back_to_the_local_index_and_is_null_without_one(self):
        self.write("a.md", "a\n")
        self.write("c.md", "c\n")
        self.write("MEMORY.md", "- [A](a.md) - mine\n")
        self.sync().rm("a.md")
        self.sync().rm("c.md")
        self.assertEqual(self.marks()["a.md"][0]["line"], "- [A](a.md) - mine")
        self.assertIsNone(self.marks()["c.md"][0]["line"])

    def test_rm_only_on_s3(self):
        self.s3.put("a.md", "a\n")
        self.sync().rm("a.md")
        self.assertIsNone(self.s3.get("a.md"))
        self.assertEqual(self.marks()["a.md"][0]["md5"], md5(b"a\n"))

    def test_rm_keeps_newest_20_marks_first(self):
        old = [{"md5": "%032x" % i, "at": "2026-11-0%dT00:00:00Z" % (i % 9 + 1), "by": "x", "line": None} for i in range(20)]
        self.s3.put(".deleted.json", json.dumps({"files": {"a.md": old, "other.md": old[:1]}}))
        self.write("a.md", "new\n")
        self.sync().rm("a.md")
        entries = self.marks()["a.md"]
        self.assertEqual(len(entries), 20)
        self.assertEqual(entries[0]["md5"], md5(b"new\n"))
        self.assertEqual(entries[1]["md5"], old[0]["md5"])
        self.assertEqual(self.marks()["other.md"], old[:1], "ほかの名前は変えない")

    def test_rm_dry_run_writes_nothing(self):
        self.write("a.md", "a\n")
        self.s3.put("a.md", "a\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n")
        self.s3.put("MEMORY.md", "- [A](a.md) - a\n")
        self.set_state(a=md5(b"a\n"))
        self.assertEqual(self.sync(dry_run=True).rm("a.md"), 0)
        self.assertFalse(any(c[0:2] in (["s3api", "put-object"], ["s3api", "delete-object"]) for c in self.s3.calls))
        self.assertEqual(self.read("a.md"), b"a\n")
        self.assertEqual(self.get_state(), {"a.md": md5(b"a\n")})
        for part in ("削除の印を付けます: a.md", "S3 から消します: a.md", "MEMORY.md から行を消します", "手元から消します"):
            self.assertIn("[dry-run] " + part, self.text())

    def test_rm_unknown_name_fails_with_next_step(self):
        err = io.StringIO()
        args = ["rm", "nothing.md", "--dir", str(self.dir), "--uri", URI, "--state", str(self.state)]
        with redirect_stderr(err):
            self.assertEqual(memsync.main(args, runner=self.s3, out=self.lines.append), 1)
        self.assertIn("そのメモリはありません", err.getvalue())
        self.assertIn("mvm memory status", err.getvalue())
        self.assertIsNone(self.s3.get(".deleted.json"))

    def test_rm_rejects_bad_names(self):
        for name in ("MEMORY.md", "a.txt", "../a.md", "sub/a.md", ".hidden.md", "a\\b.md", "", None):
            with self.assertRaises(memsync.MemsyncError, msg=repr(name)) as cm:
                self.sync().rm(name)
            self.assertIn(".md", str(cm.exception))
        self.assertEqual(self.s3.calls, [])

    def test_marks_race_is_retried_and_merged(self):
        self.write("a.md", "a\n")
        self.s3.put("a.md", "a\n")
        self.set_state(a=md5(b"a\n"))
        self.s3.put(".deleted.json", json.dumps({"files": {"x.md": [{"md5": "f" * 32, "at": "2026-11-23T00:00:00Z", "by": "vm", "line": None}]}}))
        raced = []

        def race(key):
            if key.endswith(".deleted.json") and not raced:
                raced.append(1)
                files = json.loads(self.s3.get(".deleted.json").decode("utf-8"))
                files["files"]["y.md"] = [{"md5": "e" * 32, "at": "2026-11-23T00:00:00Z", "by": "vm", "line": None}]
                self.s3.put(".deleted.json", json.dumps(files))
        self.s3.before_put = race
        self.sync().rm("a.md")
        self.assertEqual(set(self.marks()), {"x.md", "y.md", "a.md"}, "他のマシンの印を消さない")
        self.assertEqual(len(put_calls(self.s3, ".deleted.json")), 2)
        self.assertIsNone(self.s3.get("a.md"))

    def test_marks_gives_up_after_repeated_races_without_deleting(self):
        self.write("a.md", "a\n")
        self.s3.put("a.md", "a\n")
        counter = []

        def race(key):
            if key.endswith(".deleted.json"):
                counter.append(1)
                self.s3.put(".deleted.json", json.dumps({"files": {"z%d.md" % len(counter): []}}))
        self.s3.before_put = race
        with self.assertRaises(memsync.MemsyncError) as cm:
            self.sync().rm("a.md")
        self.assertIn("削除の印", str(cm.exception))
        self.assertIn("もう一度", str(cm.exception))
        self.assertEqual(len(counter), memsync.INDEX_PUT_ATTEMPTS)
        self.assertEqual(self.s3.get("a.md"), b"a\n", "印が書けなければ何も消さない")
        self.assertTrue((self.dir / "a.md").exists())

    def test_index_race_is_retried(self):
        self.write("a.md", "a\n")
        self.s3.put("a.md", "a\n")
        self.s3.put("MEMORY.md", "- [A](a.md) - a\n")
        raced = []

        def race(key):
            if key.endswith("MEMORY.md") and not raced:
                raced.append(1)
                self.s3.put("MEMORY.md", "- [A](a.md) - a\n- [N](n.md) - 他のマシンが足した\n")
        self.s3.before_put = race
        self.sync().rm("a.md")
        self.assertEqual(self.s3.get("MEMORY.md"), "- [N](n.md) - 他のマシンが足した\n".encode("utf-8"))
        self.assertEqual(len(put_calls(self.s3, "MEMORY.md")), 2)

    def test_access_denied_says_to_run_it_locally_and_keeps_the_rest(self):
        self.write("a.md", "a\n")
        self.s3.put("a.md", "a\n")
        self.s3.deny_delete = True
        with self.assertRaises(memsync.MemsyncError) as cm:
            self.sync().rm("a.md")
        self.assertIn("手元の PC", str(cm.exception))
        self.assertIn("s3:DeleteObject", str(cm.exception))
        self.assertIn("a.md", self.marks(), "印は書き込み済み")
        self.assertTrue((self.dir / "a.md").exists(), "S3 を消せなかったときは手元も消さない")
        self.s3.deny_delete = False
        self.sync().rm("a.md")
        self.assertIsNone(self.s3.get("a.md"))

    def test_other_machine_removes_it_on_pull_and_never_sends_it_back(self):
        # 手元 (self.dir) で rm → 別のマシン (dir2) が pull で消し、push しても送り返さない
        dir2 = Path(self.tmp.name) / "memory2"
        dir2.mkdir()
        state2 = Path(self.tmp.name) / "state2.json"
        self.write("a.md", "a\n")
        self.write("MEMORY.md", "- [A](a.md) - a\n")
        self.sync().push()

        def other():
            return memsync.Memsync(dir2, URI, state2, origin="vm-1", runner=self.s3, out=self.lines.append)
        other().pull()
        self.assertEqual((dir2 / "a.md").read_bytes(), b"a\n")
        self.sync().rm("a.md")
        self.assertIsNone(self.s3.get("a.md"))
        sync2 = other()
        sync2.pull()
        self.assertFalse((dir2 / "a.md").exists())
        self.assertNotIn("(a.md)", (dir2 / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual(sync2.deleted, ["a.md"])
        other().push()
        self.assertIsNone(self.s3.get("a.md"))

    def test_old_vm_sending_it_back_is_ignored_by_everyone(self):
        self.write("a.md", "a\n")
        self.sync().push()
        self.sync().rm("a.md")
        self.s3.put("a.md", "a\n")                  # 印を読まない古い VM が送り直した
        sync = self.sync()
        sync.pull()
        self.assertFalse((self.dir / "a.md").exists())
        self.assertIn("取得しません", self.text())


class LogTest(MemsyncTestCase):
    def hist(self, name, stamp, origin, text):
        data = text.encode("utf-8")
        self.s3.put(".history/%s/%s-%s-%s.md" % (name, stamp, origin, md5(data)[:8]), data)
        return md5(data)[:8]

    def setUp(self):
        super().setUp()
        self.h1 = self.hist("a.md", "20261124T090000Z", "local", "a1\n")
        self.h2 = self.hist("b.md", "20261124T093000Z", "vm-agent-01", "b1\n")
        self.h3 = self.hist("a.md", "20261124T101503Z", "vm-agent-01", "a2\n")
        self.s3.put(".deleted.json", json.dumps({"files": {"b.md": [
            {"md5": md5(b"b1\n"), "at": "2026-11-24T10:00:00Z", "by": "local", "line": None}]}}))

    def run_log(self, *args, **kw):
        self.lines.clear()
        rc = self.sync().history_log(*args, **kw)
        self.assertEqual(rc, 0)
        return list(self.lines)

    def test_table_is_oldest_first_with_deletes_mixed_in(self):
        rows = self.run_log()
        self.assertEqual(len(rows), 4)
        self.assertEqual([r.split()[0] + " " + r.split()[1] for r in rows],
                         ["2026-11-24 09:00:00Z", "2026-11-24 09:30:00Z", "2026-11-24 10:00:00Z", "2026-11-24 10:15:03Z"])
        self.assertRegex(rows[0], r"^2026-11-24 09:00:00Z  local +書いた +a\.md$")
        self.assertRegex(rows[1], r"vm-agent-01 +書いた +b\.md$")
        self.assertRegex(rows[2], r"local +消した +b\.md$")
        self.assertRegex(rows[3], r"vm-agent-01 +書いた +a\.md$")

    def test_name_filter(self):
        rows = self.run_log("b.md")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.endswith("b.md") for r in rows))
        listing = [c for c in self.s3.calls if c[:2] == ["s3api", "list-objects-v2"]]
        self.assertIn(PREFIX + ".history/b.md/", [c[c.index("--prefix") + 1] for c in listing])

    def test_origin_filter(self):
        rows = self.run_log(only_origin="vm-agent-01")
        self.assertEqual(len(rows), 2)
        self.assertTrue(all("vm-agent-01" in r for r in rows))
        self.assertEqual(len(self.run_log(only_origin="local")), 2)
        self.assertTrue(self.run_log(only_origin="nobody")[0].startswith("履歴はありません"))

    def test_since_relative_and_absolute(self):
        self.clock[0] = datetime(2026, 11, 24, 10, 30, 0, tzinfo=timezone.utc)
        self.assertEqual(len(self.run_log(since="30m")), 2)         # 10:00 以降: 削除と a2
        self.assertEqual(len(self.run_log(since="2h")), 4)          # 08:30 以降
        self.assertEqual(len(self.run_log(since="1d")), 4)
        self.assertEqual(len(self.run_log(since="2026-11-24T10:00:00Z")), 2, "その時刻を含む")
        self.assertTrue(self.run_log(since="2026-11-24T19:15:04+09:00")[0].startswith("履歴はありません"), "UTC 10:15:04 より後はない")
        self.assertEqual(len(self.run_log(since="2026-11-24")), 4)

    def test_parse_since(self):
        now = datetime(2026, 11, 24, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(memsync.parse_since("30m", now), datetime(2026, 11, 24, 11, 30, tzinfo=timezone.utc))
        self.assertEqual(memsync.parse_since("2h", now), datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(memsync.parse_since("3d", now), datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc))
        self.assertEqual(memsync.parse_since("2026-11-24T10:00:00Z"), datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(memsync.parse_since("2026-11-24"), datetime(2026, 11, 24, tzinfo=timezone.utc))
        self.assertEqual(memsync.parse_since("2026-11-24T10:00:00"), datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc))
        self.assertEqual(memsync.parse_since("2026-11-24T19:00:00+09:00"), datetime(2026, 11, 24, 10, 0, tzinfo=timezone.utc))
        # 99999999999d と 0001-01-01T00:00:00+09:00 は、日時の範囲を外れる (OverflowError で落ちていた)
        for bad in ("", "yesterday", "5x", "-3d", "1.5h", "2026-13-40", "h", "99999999999d", "0001-01-01T00:00:00+09:00"):
            with self.assertRaises(memsync.MemsyncError, msg=bad) as cm:
                memsync.parse_since(bad, now)
            self.assertIn("--since", str(cm.exception))
            self.assertIn("30m", str(cm.exception), "次にすることを書く")

    def test_bad_since_is_an_error_before_any_aws_call(self):
        with self.assertRaises(memsync.MemsyncError):
            self.sync().history_log(since="soon")
        self.assertEqual(self.s3.calls, [])

    def test_json(self):
        (text,) = self.run_log("a.md", as_json=True)
        data = json.loads(text)
        self.assertEqual(data, [
            {"at": "2026-11-24T09:00:00Z", "origin": "local", "action": "write", "name": "a.md", "md5": self.h1,
             "key": ".history/a.md/20261124T090000Z-local-%s.md" % self.h1},
            {"at": "2026-11-24T10:15:03Z", "origin": "vm-agent-01", "action": "write", "name": "a.md", "md5": self.h3,
             "key": ".history/a.md/20261124T101503Z-vm-agent-01-%s.md" % self.h3}])
        (text,) = self.run_log("b.md", as_json=True)
        delete = [e for e in json.loads(text) if e["action"] == "delete"]
        self.assertEqual(delete, [{"at": "2026-11-24T10:00:00Z", "origin": "local", "action": "delete", "name": "b.md",
                                   "md5": md5(b"b1\n"), "key": ".deleted.json"}])

    def test_json_empty_is_an_empty_array(self):
        self.assertEqual(self.run_log(only_origin="nobody", as_json=True), ["[]"])

    def test_pagination(self):
        for i in range(5):
            self.hist("p.md", "2026112%dT000000Z" % i, "local", "p%d\n" % i)
        self.s3.page_size = 2
        self.assertEqual(len(self.run_log("p.md")), 5)

    def test_no_history_says_older_memories_have_none(self):
        self.s3.objects.clear()
        (text,) = self.run_log()
        self.assertIn("段階 3 より前に送られたメモリには履歴がありません", text)

    def test_log_writes_nothing(self):
        self.run_log()
        self.assertFalse(any(c[:2] in (["s3api", "put-object"], ["s3api", "delete-object"]) for c in self.s3.calls))
        self.assertFalse(self.state.exists())


class RevertTest(MemsyncTestCase):
    def hist(self, name, stamp, origin, data):
        data = data if isinstance(data, bytes) else data.encode("utf-8")
        self.s3.put(".history/%s/%s-%s-%s.md" % (name, stamp, origin, md5(data)[:8]), data)

    def three_versions(self, current=b"v3\n"):
        for stamp, origin, text in (("20261124T080000Z", "local", b"v1\n"), ("20261124T090000Z", "vm-1", b"v2\n"),
                                    ("20261124T100000Z", "local", b"v3\n")):
            self.hist("a.md", stamp, origin, text)
        self.s3.put("a.md", current)
        self.write("a.md", current)
        self.set_state(a=md5(current))

    def test_default_goes_back_one_version(self):
        self.three_versions()
        self.s3.calls.clear()
        self.assertEqual(self.sync(origin="pc").revert("a.md"), 0)
        self.assertEqual(self.s3.get("a.md"), b"v2\n")
        self.assertEqual(self.read("a.md"), b"v2\n")
        self.assertEqual(self.get_state(), {"a.md": md5(b"v2\n")})
        (put,) = put_calls(self.s3, "a.md")
        self.assertEqual(put[put.index("--if-match") + 1], md5(b"v3\n"), "今の S3 の ETag を条件にする")
        key = ".history/a.md/%s-pc-%s.md" % (STAMP0, md5(b"v2\n")[:8])
        self.assertEqual(self.s3.get(key), b"v2\n", "戻したことも履歴に新しい版として残る")
        self.assertIn("戻しました: a.md", self.text())

    def test_reverting_twice_walks_back_through_history(self):
        self.three_versions()
        self.sync().revert("a.md")
        self.clock[0] = datetime(2026, 11, 24, 11, 0, 0, tzinfo=timezone.utc)
        self.sync().revert("a.md")
        self.assertEqual(self.s3.get("a.md"), b"v3\n", "最新の履歴は戻した版 (v2) なので、その 1 つ前は v3")

    def test_to_picks_the_latest_version_at_or_before_the_time(self):
        for to, expected in (("20261124T094500Z", b"v2\n"), ("20261124T090000Z", b"v2\n"), ("2026-11-24T08:30:00Z", b"v1\n"),
                             ("2026-11-24T18:30:00+09:00", b"v2\n"), ("20261124T080000Z", b"v1\n")):
            self.s3.objects.clear()
            self.three_versions()
            self.sync().revert("a.md", to=to)
            self.assertEqual(self.s3.get("a.md"), expected, to)

    def test_to_errors(self):
        self.three_versions()
        with self.assertRaises(memsync.MemsyncError) as cm:
            self.sync().revert("a.md", to="20261124T070000Z")
        self.assertIn("mvm memory log", str(cm.exception))
        with self.assertRaises(memsync.MemsyncError) as cm:
            self.sync().revert("a.md", to="tomorrow")
        self.assertIn("--to", str(cm.exception))
        self.assertEqual(self.s3.get("a.md"), b"v3\n")

    def test_to_the_current_version_does_nothing(self):
        self.three_versions()
        self.s3.calls.clear()
        self.assertEqual(self.sync().revert("a.md", to="20261124T100000Z"), 0)
        self.assertIn("何もしません", self.text())
        self.assertFalse(any(c[:2] == ["s3api", "put-object"] for c in self.s3.calls))

    def test_unsynced_local_change_stops_without_force(self):
        self.three_versions()
        self.write("a.md", "mine\n")
        with self.assertRaises(memsync.MemsyncError) as cm:
            self.sync().revert("a.md")
        self.assertIn("--force", str(cm.exception))
        self.assertIn("mvm memory push", str(cm.exception))
        self.assertEqual(self.read("a.md"), b"mine\n")
        self.assertEqual(self.s3.get("a.md"), b"v3\n")
        self.sync(force=True).revert("a.md")
        self.assertEqual(self.read("a.md"), b"v2\n")
        self.assertEqual(self.s3.get("a.md"), b"v2\n")

    def test_local_equal_to_s3_but_not_base_is_not_unsynced(self):
        self.three_versions()
        self.set_state(a=md5(b"v1\n"))      # 基準は古いが、手元と S3 は同じ
        self.sync().revert("a.md")
        self.assertEqual(self.s3.get("a.md"), b"v2\n")

    def test_dry_run_shows_the_version_and_changes_nothing(self):
        self.three_versions(current=b"v3\n")
        self.s3.calls.clear()
        self.sync(dry_run=True).revert("a.md")
        self.assertEqual(self.s3.get("a.md"), b"v3\n", "今のまま")
        self.assertEqual(self.read("a.md"), b"v3\n")
        self.assertEqual(self.get_state(), {"a.md": md5(b"v3\n")})
        self.assertFalse(any(c[:2] == ["s3api", "put-object"] for c in self.s3.calls))
        self.assertIn("[dry-run] 戻す版: 20261124T090000Z (書いたマシン vm-1", self.text())
        self.assertIn("    | v2", self.lines)

    def test_dry_run_preview_is_first_five_lines(self):
        self.hist("a.md", "20261124T080000Z", "local", "".join("old%d\n" % i for i in range(8)))
        self.s3.put("a.md", "now\n")
        self.sync(dry_run=True).revert("a.md")
        previews = [l for l in self.lines if l.startswith("    | ")]
        self.assertEqual(previews, ["    | old%d" % i for i in range(5)])

    def test_revert_after_rm_restores_content_removes_mark_and_index_lines(self):
        data = b"---\nname: a\n---\nkept\n"
        self.hist("a.md", "20261124T080000Z", "local", data)
        self.s3.put(".deleted.json", json.dumps({"files": {
            "a.md": [{"md5": md5(data), "at": "2026-11-24T09:00:00Z", "by": "pc", "line": "- [A](a.md) - 大事な記憶"}],
            "z.md": [{"md5": "d" * 32, "at": "2026-11-24T09:00:00Z", "by": "pc", "line": None}]}}))
        self.s3.put("MEMORY.md", "- [B](b.md) - b\n")
        self.write("MEMORY.md", "- [B](b.md) - b\n")
        self.sync().revert("a.md")
        self.assertEqual(self.s3.get("a.md"), data)
        self.assertEqual(self.read("a.md"), data)
        (put,) = put_calls(self.s3, "a.md")
        self.assertIn("--if-none-match", put, "S3 に無いので「まだ無いこと」を条件にする")
        files = json.loads(self.s3.get(".deleted.json").decode("utf-8"))["files"]
        self.assertEqual(list(files), ["z.md"], "印を外す (ほかの名前は残す)")
        self.assertEqual(self.s3.get("MEMORY.md").decode("utf-8"), "- [B](b.md) - b\n- [A](a.md) - 大事な記憶\n")
        self.assertEqual(self.read("MEMORY.md").decode("utf-8"), "- [B](b.md) - b\n- [A](a.md) - 大事な記憶\n")
        # 戻したメモリは、印が外れたので pull / push でまた消えたり送り返されたりしない
        self.sync().pull()
        self.sync().push()
        self.assertEqual(self.read("a.md"), data)
        self.assertEqual(self.s3.get("a.md"), data)

    def test_index_line_is_not_duplicated_and_missing_files_are_created(self):
        data = b"kept\n"
        self.hist("a.md", "20261124T080000Z", "local", data)
        self.s3.put(".deleted.json", json.dumps({"files": {"a.md": [
            {"md5": md5(data), "at": "2026-11-24T09:00:00Z", "by": "pc", "line": "- [A](a.md) - x"}]}}))
        self.s3.put("MEMORY.md", "- [A](a.md) - すでにある\n")      # S3 には行がある。手元には索引が無い
        self.sync().revert("a.md")
        self.assertEqual(self.s3.get("MEMORY.md").decode("utf-8"), "- [A](a.md) - すでにある\n")
        self.assertEqual(self.read("MEMORY.md").decode("utf-8"), "- [A](a.md) - x\n")

    def test_only_the_reverted_content_is_unmarked(self):
        old, new = b"old\n", b"new\n"
        self.hist("a.md", "20261124T080000Z", "local", old)
        self.s3.put(".deleted.json", json.dumps({"files": {"a.md": [
            {"md5": md5(new), "at": "2026-11-24T09:00:00Z", "by": "pc", "line": None},
            {"md5": md5(old), "at": "2026-11-24T08:30:00Z", "by": "pc", "line": None}]}}))
        self.sync().revert("a.md")
        files = json.loads(self.s3.get(".deleted.json").decode("utf-8"))["files"]
        self.assertEqual([e["md5"] for e in files["a.md"]], [md5(new)])

    def test_no_history_and_bad_names(self):
        self.s3.put("a.md", "a\n")
        with self.assertRaises(memsync.MemsyncError) as cm:
            self.sync().revert("a.md")
        self.assertIn("履歴がありません", str(cm.exception))
        for name in ("MEMORY.md", "a.txt", "", None):
            with self.assertRaises(memsync.MemsyncError):
                self.sync().revert(name)

    def test_only_one_version_means_nothing_to_go_back_to(self):
        self.hist("a.md", "20261124T080000Z", "local", "v1\n")
        self.s3.put("a.md", "v1\n")
        with self.assertRaises(memsync.MemsyncError) as cm:
            self.sync().revert("a.md")
        self.assertIn("戻せる版がありません", str(cm.exception))

    def test_other_machine_writing_meanwhile_stops_the_revert(self):
        self.three_versions()
        self.s3.before_put = lambda key: self.s3.put("a.md", "theirs\n") if key.endswith("/a.md") else None
        with self.assertRaises(memsync.MemsyncError) as cm:
            self.sync().revert("a.md")
        self.assertIn("他のマシンが書き換えました", str(cm.exception))
        self.assertEqual(self.s3.get("a.md"), b"theirs\n")
        self.assertEqual(self.read("a.md"), b"v3\n")

    def test_corrupted_history_is_refused(self):
        self.three_versions()
        self.s3.put(".history/a.md/20261124T095900Z-local-%s.md" % md5(b"v2\n")[:8], "tampered\n")
        self.s3.objects.pop(PREFIX + ".history/a.md/20261124T090000Z-vm-1-%s.md" % md5(b"v2\n")[:8])
        with self.assertRaises(memsync.MemsyncError) as cm:
            self.sync().revert("a.md")
        self.assertIn("合いません", str(cm.exception))
        self.assertEqual(self.s3.get("a.md"), b"v3\n")


class AdminCliTest(MemsyncTestCase):
    def main(self, *argv):
        args = list(argv) + ["--dir", str(self.dir), "--uri", URI, "--state", str(self.state)]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = memsync.main(args, runner=self.s3, out=lambda m: print(m))
        return rc, out.getvalue(), err.getvalue()

    def test_push_log_rm_revert_through_main(self):
        self.write("a.md", "v1\n")
        self.assertEqual(self.main("push", "--origin", "vm-7")[0], 0)
        self.write("a.md", "v2\n")
        self.assertEqual(self.main("push", "--origin", "vm-7")[0], 0)
        rc, out, _ = self.main("log", "a.md", "--origin", "vm-7", "--since", "2000-01-01", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual([e["origin"] for e in json.loads(out)], ["vm-7", "vm-7"])
        self.assertEqual(json.loads(self.main("log", "--origin", "someone", "--json")[1]), [])
        rc, out, _ = self.main("log")
        self.assertIn("a.md", out)
        rc, out, _ = self.main("revert", "a.md", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("戻す版", out)
        self.assertEqual(self.main("revert", "a.md")[0], 0)
        self.assertEqual(self.read("a.md"), b"v1\n")
        self.assertEqual(self.main("rm", "a.md")[0], 0)
        self.assertFalse((self.dir / "a.md").exists())
        self.assertIsNone(self.s3.get("a.md"))
        self.assertIn("a.md", json.loads(self.s3.get(".deleted.json").decode("utf-8"))["files"])

    def test_origin_of_rm_is_recorded_as_the_machine_name(self):
        self.write("a.md", "a\n")
        self.main("rm", "a.md", "--origin", "vm-7")
        self.assertEqual(json.loads(self.s3.get(".deleted.json").decode("utf-8"))["files"]["a.md"][0]["by"], "vm-7")

    def test_errors_are_nonzero_with_next_steps(self):
        rc, _, err = self.main("rm")
        self.assertEqual(rc, 1)
        self.assertIn(".md", err)
        rc, _, err = self.main("revert", "a.md")
        self.assertEqual(rc, 1)
        self.assertIn("履歴がありません", err)
        rc, _, err = self.main("log", "--since", "soon")
        self.assertEqual(rc, 1)
        self.assertIn("--since", err)
        rc, _, err = self.main("pull", "a.md")
        self.assertEqual(rc, 1)
        self.assertIn("余分な引数", err)

    def test_hook_with_a_bad_event_still_exits_zero(self):
        for argv in (["hook", "nonsense"], ["hook"], ["hook", "a.md"]):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                rc = memsync.main(argv + ["--dir", str(self.dir), "--uri", URI, "--state", str(self.state)],
                                  runner=self.s3, stdin=io.StringIO("{}"))
            self.assertEqual(rc, 0, argv)
            self.assertEqual(out.getvalue(), "")
            self.assertIn("hook のイベント", err.getvalue())
        self.assertEqual(self.s3.calls, [])


def fail_first(real, times=1, only=None):
    """最初の times 回だけ PermissionError を投げ、その後は本物を呼ぶ差し替え。

    Windows では、他のプロセスが消している最中 (削除待ち) や開いている最中のファイルへの
    os.open / os.replace / os.remove が PermissionError になる。それを OS によらず再現する。
    only (パスか、パスを受け取って真偽を返す関数) を渡すと、それに当たる呼び出しだけを数えて失敗させる
    (一時フォルダの後始末などは素通し)。
    """
    calls = []
    match = only if callable(only) else (lambda p: str(p) == str(only))

    def fake(*args, **kwargs):
        if only is not None and not match(args[0]):
            return real(*args, **kwargs)
        calls.append(args)
        if len(calls) <= times:
            raise PermissionError(13, "Permission denied", args[0])
        return real(*args, **kwargs)
    fake.calls = calls
    return fake


class WindowsBusyFileTest(MemsyncTestCase):
    """Windows で一時的に PermissionError になるファイル操作で、フックや pull を落とさない。"""

    def setUp(self):
        super().setUp()
        self.lock = Path(str(self.state) + ".lock")

    def test_acquire_lock_retries_when_lock_is_being_deleted(self):
        fake = fail_first(os.open)
        with mock.patch.object(memsync.os, "open", fake):
            self.assertTrue(memsync._acquire_lock(self.lock, 2))
        self.assertGreaterEqual(len(fake.calls), 2, "取り直している")
        self.assertTrue(self.lock.exists())

    def test_acquire_lock_gives_up_at_deadline_when_permission_error_persists(self):
        fake = fail_first(os.open, times=10 ** 6)
        start = time.monotonic()
        with mock.patch.object(memsync.os, "open", fake):
            self.assertFalse(memsync._acquire_lock(self.lock, 0.3))
        self.assertLess(time.monotonic() - start, 5, "期限で諦める")

    def test_hook_survives_permission_error_on_lock(self):
        self.s3.put("a.md", "a\n")
        with mock.patch.object(memsync.os, "open", fail_first(os.open, only=self.lock)):
            out = memsync.run_hook("prompt", self.sync(), "{}", now=time.time, lock_wait=2)
        self.assertIn("a.md", json.loads(out)["hookSpecificOutput"]["additionalContext"])
        self.assertFalse(self.lock.exists(), "終わったらロックを外す")

    def test_write_local_retries_replace(self):
        fake = fail_first(os.replace)
        with mock.patch.object(memsync.os, "replace", fake):
            self.sync().write_local("a.md", b"new\n")
        self.assertEqual((self.dir / "a.md").read_bytes(), b"new\n")
        self.assertEqual(len(fake.calls), 2)
        self.assertFalse((self.dir / "a.md.pmva-tmp").exists())

    def test_write_local_removes_tmp_when_replace_keeps_failing(self):
        self.write("a.md", "old\n")
        with mock.patch.object(memsync, "BUSY_DELAY", 0), \
                mock.patch.object(memsync.os, "replace", fail_first(os.replace, times=10 ** 6)):
            with self.assertRaises(PermissionError):
                self.sync().write_local("a.md", b"new\n")
        self.assertEqual((self.dir / "a.md").read_bytes(), b"old\n", "手元のファイルは壊さない")
        self.assertFalse((self.dir / "a.md.pmva-tmp").exists(), "一時ファイルを残さない")

    def test_hook_releases_lock_even_if_first_remove_fails(self):
        # ロックが残ると、120 秒たつまで他の会話の同期が飛ばされる
        self.s3.put("a.md", "a\n")
        fake = fail_first(os.remove, only=self.lock)
        with mock.patch.object(memsync.os, "remove", fake), mock.patch.object(memsync.os, "unlink", fake):
            memsync.run_hook("prompt", self.sync(), "{}", now=time.time, lock_wait=2)
        self.assertGreaterEqual(len(fake.calls), 1, "ロックを外そうとしている")
        self.assertFalse(self.lock.exists())

    def test_busy_temp_file_does_not_break_pull_and_push(self):
        # aws.exe やウイルス対策ソフトが一時ファイルをまだ開いていると、一時フォルダの後始末が PermissionError になる
        self.s3.put("a.md", "a\n")
        self.write("b.md", "b\n")
        in_tmp = lambda p: os.path.basename(str(p)) in ("download", "body") and "memory" not in str(p)  # noqa: E731
        fake = fail_first(os.unlink, times=3, only=in_tmp)
        with mock.patch.object(memsync, "BUSY_DELAY", 0), \
                mock.patch.object(memsync.os, "unlink", fake), mock.patch.object(memsync.os, "remove", fake):
            sync = self.sync()
            sync.pull()
            sync.push()
        self.assertGreaterEqual(len(fake.calls), 3)
        self.assertEqual((self.dir / "a.md").read_bytes(), b"a\n")
        self.assertEqual(self.s3.get("b.md"), b"b\n")

    def busy_remove(self, name):
        fake = fail_first(os.remove, only=self.dir / name)
        return fake, mock.patch.multiple(memsync.os, remove=fake, unlink=fake)

    def test_pull_deletes_memory_removed_elsewhere_even_if_busy(self):
        self.write("a.md", "old\n")
        self.set_state(a=md5(b"old\n"))
        self.s3.put(".deleted.json", json.dumps(
            {"files": {"a.md": [{"md5": md5(b"old\n"), "at": "2026-11-24T09:00:00Z", "by": "pc", "line": None}]}}))
        fake, patch = self.busy_remove("a.md")
        with patch:
            sync = self.sync()
            sync.pull()
        self.assertEqual(len(fake.calls), 2)
        self.assertFalse((self.dir / "a.md").exists())
        self.assertEqual(sync.deleted, ["a.md"])

    def test_rm_deletes_local_file_even_if_busy(self):
        self.write("a.md", "a\n")
        self.s3.put("a.md", "a\n")
        fake, patch = self.busy_remove("a.md")
        with patch:
            self.assertEqual(self.sync().rm("a.md"), 0)
        self.assertEqual(len(fake.calls), 2)
        self.assertFalse((self.dir / "a.md").exists())
        self.assertNotIn("消せませんでした", self.text())


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

    def test_bootstrap_installs_memory_hooks(self):
        bootstrap = self.read("image/bootstrap.sh")
        section = bootstrap.split("# 自動メモリのフック", 1)[1].split("\n# Codex", 1)[0]
        for name in ("SessionStart", "UserPromptSubmit", "PostToolUse", "session-start", "prompt", "post-tool"):
            self.assertIn('"%s"' % name, section)
        self.assertIn("/opt/pmva/memsync.py", section)
        self.assertIn("hook", section)
        self.assertIn('"async": true', section, "送信は会話を待たせない")
        self.assertIn("Write|Edit", section)


if __name__ == "__main__":
    unittest.main()
