#!/usr/bin/env python3
"""Claude Code の自動メモリ (auto memory) を S3 経由で手元 PC と MicroVM で共有する。

手元の `mvm memory` (bin/mvm.py が読み込む) と、MicroVM の bootstrap.sh / snapshot.sh (CLI) の両方がこの 1 つの実装を使う。
Python 標準ライブラリと AWS CLI だけで動く (Python 3.9 以上)。

  memsync.py {pull|push|status} --dir <メモリのフォルダ> --uri s3://<bucket>/memory/<repo-id>/
             --state <状態ファイル> [--origin <名前>] [--dry-run] [--force]
  memsync.py hook {session-start|prompt|post-tool} --dir ... --uri ... --state ... [--min-interval <秒>]
             Claude Code のフックから呼ぶ。イベントの JSON を標準入力で受け取り、応答の JSON だけを標準出力に書く
  memsync.py log [<名前>] [--origin <書いたマシン>] [--since <30m|2h|3d|日時>] [--json] --dir ... --uri ... --state ...
  memsync.py revert <名前> [--to <時刻>] [--force] [--dry-run] --dir ... --uri ... --state ...
  memsync.py rm <名前> [--dry-run] --dir ... --uri ... --state ...

メモリは 1 件 1 ファイルの *.md と、索引の MEMORY.md。*.md は「最後に同期したときの内容」を基準にした 3 者比較
(手元 / S3 / 基準) で、MEMORY.md は行の和集合でマージする。削除は相手に伝えないが、rm で消したものだけは
S3 の .deleted.json に「消した中身の md5」を印として残し、他のマシンが pull で手元からも消す。
送るたびに .history/<名前>/ へ写しを残し (log / revert で見る・戻す)、誤ったメモリを戻せるようにする。
S3 への書き込みは条件付き (--if-match / --if-none-match) にして、一覧を取ってから送るまでの間に他のマシンが
書き換えたものを黙って上書きしない (ファイルは競合として飛ばし、MEMORY.md は取り直してマージし直す)。
"""

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

INDEX_NAME = "MEMORY.md"
DELETED_NAME = ".deleted.json"    # rm した削除の印 (S3 の memory/<repo-id>/ 直下)
HISTORY_DIR = ".history/"         # 送るたびの写し (.history/<名前>/<時刻>-<送り元>-<md5 先頭 8 文字>.md)
DELETED_KEEP = 20                 # 印に残す、名前ごとの件数
MAX_FILE_BYTES = 1024 * 1024      # これを超えるファイルは送らない
INDEX_MAX_LINES = 200             # Claude Code が読む索引の上限 (先頭 200 行 / 25KB)
INDEX_MAX_BYTES = 25 * 1024
SANITIZED_MAX = 200               # これを超えると Claude Code はフォルダ名を切り詰めてハッシュを付ける (規則が不明)
INDEX_PUT_ATTEMPTS = 3            # MEMORY.md を送るときに、他のマシンと書き込みがぶつかったら取り直す回数
HOOK_CONTEXT_MAX = 9500           # フックで会話に渡す文字数 (Claude Code は 1 万文字を超えるとファイルに逃がす)
HOOK_LOG_MAX = 256 * 1024
# Windows で、他のプロセスが開いている・消している最中のファイルを操作して PermissionError になったときに
# 待ってやり直す回数と間隔 (合わせて 1 秒ほど)
BUSY_RETRIES = 20
BUSY_DELAY = 0.05
LOCK_STALE = 120                  # これより長く更新されていないロックは、同期の途中で落ちて残ったものとして奪う
CLI_LOCK_WAIT = 60                # 手動の同期 (pull / push / rm / revert) が、フックなど他の同期の終わりを待つ秒数
LOCKED_ACTIONS = ("pull", "push", "rm", "revert")    # 手元のファイルや状態ファイルを書き換える (ロックを取る) 操作
# フックの名前 (memsync.py hook <名前>) → Claude Code のイベント名
HOOK_EVENTS = {"session-start": "SessionStart", "prompt": "UserPromptSubmit", "post-tool": "PostToolUse"}

# bin/mvm.py の SECRET_PATTERN と同じ正規表現 (認証情報らしい文字列)
SECRET_PATTERN = re.compile(
    rb"(sk-ant-[A-Za-z0-9_-]{16,}|sk-(proj-)?[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    rb"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[abprs]-[A-Za-z0-9-]{10,}"
    rb"|hooks\.slack\.com/services/[A-Za-z0-9/]+)")

HISTORY_FILE = re.compile(r"^(\d{8}T\d{6}Z)-(.+)-([0-9a-f]{8})\.md$")
LINK_PATTERN = re.compile(r"\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")
SHARE_FALSE_PATTERN = re.compile(r"^share:\s*[\"']?false[\"']?\s*(#.*)?$", re.IGNORECASE)


class MemsyncError(Exception):
    """利用者に次の行動を伝えて終わるエラー。"""


class LockBusy(MemsyncError):
    """他の同期 (会話のフック、mvm memory、VM の snapshot.sh) がロックを持ったまま、待っても終わらなかった。"""

    def __init__(self, lock, wait):
        super().__init__("他の同期 (会話のフックなど) が %d 秒たっても終わりません。少し待ってからもう一度実行してください。"
                         "同期の途中で落ちて残ったロックなら、%d 秒たつと自動で外れます (%s)" % (wait, LOCK_STALE, lock))
        self.lock = lock


# ---------------------------------------------------------------- Windows で使用中のファイル

def _retry_busy(func, *args):
    """func(*args) を呼ぶ。PermissionError なら少し待ってやり直し、BUSY_RETRIES 回で諦めて投げる。

    Windows では、他のプロセス (同時に動く別の会話のフック、aws.exe、ウイルス対策ソフトなど) が開いている、
    または消している最中 (削除待ち) のファイルへの os.replace / os.remove などが PermissionError になる。
    たいていすぐ終わるので、待てば通る。
    """
    for attempt in range(BUSY_RETRIES):
        try:
            return func(*args)
        except PermissionError:
            if attempt + 1 >= BUSY_RETRIES:
                raise
            time.sleep(BUSY_DELAY)


@contextlib.contextmanager
def _temp_dir():
    """一時フォルダ。後始末で消せなくても (Windows で使用中)、同期そのものは失敗にしない。

    tempfile.TemporaryDirectory は、後始末の PermissionError をそのまま投げる (Python 3.9)。
    """
    path = tempfile.mkdtemp(prefix="pmva-memsync-")
    try:
        yield path
    finally:
        try:
            _retry_busy(shutil.rmtree, path)
        except OSError:
            shutil.rmtree(path, ignore_errors=True)      # 残っても OS の一時フォルダの中なので害は無い


# ---------------------------------------------------------------- repo-id とメモリのフォルダ

def normalize_repo_id(url):
    """git のリモート URL を host/owner/name (小文字、.git なし、userinfo なし) にする。

    https://github.com/Owner/Repo.git、git@github.com:Owner/Repo.git、ssh://git@github.com/Owner/Repo は
    すべて github.com/owner/repo。URL にトークンが入っていることがあるので、エラーには URL を載せない。
    """
    text = (url or "").strip()
    m = re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://(?:[^/@]*@)?([^/:@]+)(?::\d+)?/(.+)$", text)
    if not m:
        m = re.match(r"^(?:[^@/\s]+@)?([^:/\s@]+):(?!//)(.+)$", text)
        if m and re.fullmatch(r"[A-Za-z]", m.group(1)):
            m = None        # C:/Users/... は Windows のパスで、ホスト名ではない
    if m:
        path = m.group(2).strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        repo_id = f"{m.group(1)}/{path}".lower()
        parts = repo_id.split("/")
        if re.fullmatch(r"[a-z0-9._/-]+", repo_id) and len(parts) >= 3 and all(p not in ("", ".", "..") for p in parts):
            return repo_id
    raise MemsyncError("git の remote (origin) の URL から、メモリの置き場所 (host/owner/name) を決められません。"
                       "https://github.com/<owner>/<repo>.git や git@github.com:<owner>/<repo>.git の形の origin を設定してください")


def sanitize_path(path):
    """Claude Code がプロジェクトのフォルダ名にする規則: 英数字以外を - にする。"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _git(directory, *args):
    p = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if p.returncode != 0:
        raise MemsyncError("git %s に失敗しました: %s" % (args[0], p.stderr.strip()))
    return p.stdout.strip()


def git_remote_url(directory, git=_git):
    try:
        return git(directory, "remote", "get-url", "origin")
    except MemsyncError:
        raise MemsyncError("%s の git に remote (origin) がありません。--dir で git リポジトリのフォルダを指定するか、"
                           "git remote add origin <URL> で設定してください" % directory)


def repo_root(directory, git=_git):
    """リポジトリのルート。worktree でもメインのリポジトリのルートになる。

    --path-format=absolute は使わない (2.31 より古い git は知らないオプションをそのまま出力し、終了コード 0 で返すため)。
    --git-common-dir は相対パスで返ることがあるので、git を実行したフォルダを基準に絶対パスにする。
    """
    try:
        text = git(directory, "rev-parse", "--git-common-dir")
        common = Path(text)
        # Windows の git は C:/... で返す。どの OS で読んでも絶対パスとして扱う (サニタイズの結果は同じ)
        if not (common.is_absolute() or re.match(r"^[A-Za-z]:[/\\]", text)):
            # シンボリックリンクや Windows の短い名前 (RUNNER~1 など) は、git が返すパスと同じく実体の名前にする
            common = Path(os.path.realpath(str(Path(directory) / common)))
        if common.name == ".git":
            return common.parent
    except MemsyncError:
        pass
    # サブモジュール (.git/modules/<名前>) や --separate-git-dir では、共通の git フォルダの親はルートではない
    try:
        return Path(git(directory, "rev-parse", "--show-toplevel"))
    except MemsyncError:
        raise MemsyncError("%s は git リポジトリではありません。--dir で git リポジトリのフォルダを指定してください" % directory)


def _read_setting(path, home):
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise MemsyncError("%s を読めません (%s)。JSON の書き方を直してください" % (path, e))
    value = data.get("autoMemoryDirectory") if isinstance(data, dict) else None
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if value == "~" or value.startswith("~/"):
        return Path(home) / value[2:]
    if not Path(value).is_absolute():
        raise MemsyncError("%s の autoMemoryDirectory は絶対パスか ~/ で始まるパスにしてください: %s" % (path, value))
    return Path(value)


def resolve_memory_dir(directory, home=None, git=_git):
    """手元の Claude Code が自動メモリを置くフォルダ。

    リポジトリルートの .claude/settings.local.json → ~/.claude/settings.json の autoMemoryDirectory、
    無ければ ~/.claude/projects/<サニタイズしたリポジトリルート>/memory。
    """
    home = Path(home) if home else Path.home()
    root = repo_root(directory, git)
    for settings in (root / ".claude" / "settings.local.json", home / ".claude" / "settings.json"):
        found = _read_setting(settings, home)
        if found:
            return found
    sanitized = sanitize_path(root)
    if len(sanitized) > SANITIZED_MAX:
        raise MemsyncError("リポジトリのパスが長く (%d 文字)、Claude Code のメモリのフォルダを決められません。"
                           "%s に autoMemoryDirectory を書いてください (例: \"autoMemoryDirectory\": \"~/.claude/memory/<名前>\")"
                           % (len(sanitized), root / ".claude" / "settings.local.json"))
    return home / ".claude" / "projects" / sanitized / "memory"


def state_file_for(state_dir, repo_id):
    return Path(state_dir) / "memory" / (repo_id.replace("/", "__") + ".json")


# ---------------------------------------------------------------- AWS CLI

def run_aws(args):
    """aws を実行して (終了コード, 標準出力, 標準エラー) を返す。差し替えられる (テストでスタブを使う)。"""
    env = dict(os.environ)
    env["AWS_PAGER"] = ""
    env["AWS_CLI_OUTPUT_ENCODING"] = "UTF-8"
    p = subprocess.run(["aws", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    return p.returncode, p.stdout, p.stderr


def parse_uri(uri):
    m = re.match(r"^s3://([^/]+)/(.+)$", uri or "")
    if not m:
        raise MemsyncError("--uri は s3://<バケット>/memory/<repo-id>/ の形で指定してください: %s" % uri)
    prefix = m.group(2)
    return m.group(1), prefix if prefix.endswith("/") else prefix + "/"


def normalize_bytes(data):
    return data.replace(b"\r\n", b"\n")


def md5_of(data):
    return hashlib.md5(data).hexdigest()


def has_share_false(data):
    """frontmatter (先頭の --- から次の --- まで) に share: false があるか。"""
    lines = data.decode("utf-8", errors="replace").split("\n")
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() == "---":
            return False
        if SHARE_FALSE_PATTERN.match(line.strip()):
            return True
    return False


def safe_name(name):
    """メモリのフォルダの中にそのまま置ける名前か (Windows で別のフォルダやストリームを指さない)。"""
    return (bool(name) and not name.startswith(".") and not re.search(r"[\\:<>\"|?*\x00-\x1f]", name))


def check_memory_name(name):
    """rm / revert の名前を確かめる。"""
    if (not name or "/" in name or not name.endswith(".md") or name == INDEX_NAME or not safe_name(name)):
        raise MemsyncError("メモリの名前は「user_role.md」のような .md のファイル名で指定してください (%s は使えません)。"
                           "名前は mvm memory status で確かめられます" % (name or "未指定"))


def utc_now():
    return datetime.now(timezone.utc)


def _as_utc(dt):
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def stamp_of(dt):
    """履歴のキーに入れる時刻 (20261124T101503Z)。"""
    return _as_utc(dt).strftime("%Y%m%dT%H%M%SZ")


def iso_of(dt):
    return _as_utc(dt).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(text):
    """20261124T101503Z、または ISO 8601 の日時 (2026-11-24T10:00:00Z、2026-11-24T19:00:00+09:00、2026-11-24)。

    UTC の datetime を返す。タイムゾーンが無いものは UTC として扱う。解釈できなければ ValueError。
    """
    t = (text or "").strip()
    try:
        if re.fullmatch(r"\d{8}T\d{6}Z", t):
            return datetime.strptime(t, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if t[-1:] in ("Z", "z"):
            t = t[:-1] + "+00:00"
        return _as_utc(datetime.fromisoformat(t))
    except OverflowError as e:     # 0001-01-01T00:00:00+09:00 など、UTC にすると範囲を外れる日時
        raise ValueError(str(e))


def parse_since(text, now=None):
    """--since の値。30m / 2h / 3d の相対指定 (今からさかのぼる) か、parse_time の形の日時。UTC の datetime を返す。"""
    t = (text or "").strip()
    m = re.fullmatch(r"(\d+)([mhd])", t, re.IGNORECASE)
    try:
        if m:
            unit = {"m": "minutes", "h": "hours", "d": "days"}[m.group(2).lower()]
            return _as_utc(now or utc_now()) - timedelta(**{unit: int(m.group(1))})
        return parse_time(t)
    except (ValueError, OverflowError):     # 99999999999d のように、さかのぼると日時の範囲を外れるもの
        raise MemsyncError("--since の値を解釈できません: %s。30m / 2h / 3d の形か、2026-11-24T10:00:00Z や 2026-11-24 の形の日時で指定してください"
                           % (text or "空"))


def parse_history_key(rel):
    """prefix より後のキー (.history/<名前>/<ファイル名>) から {at, origin, name, md5, key} を取り出す。違う形なら None。"""
    if not rel.startswith(HISTORY_DIR):
        return None
    name, sep, fname = rel[len(HISTORY_DIR):].partition("/")
    m = HISTORY_FILE.match(fname)
    if not sep or not name or "/" in fname or not m:
        return None
    try:
        at = parse_time(m.group(1))
    except ValueError:
        return None
    return {"at": at, "origin": m.group(2), "name": name, "md5": m.group(3), "key": rel}


def parse_deleted(data):
    """.deleted.json の中身 → {名前: [印, ...]}。形が違えば ValueError。"""
    obj = json.loads(data.decode("utf-8"))
    files = obj.get("files") if isinstance(obj, dict) else None
    if not isinstance(files, dict):
        raise ValueError("files がありません")
    return {name: [e for e in entries if isinstance(e, dict) and isinstance(e.get("md5"), str)]
            for name, entries in files.items() if isinstance(name, str) and isinstance(entries, list)}


def line_key(line):
    m = LINK_PATTERN.search(line)
    if not m:
        return None
    target = m.group(1)
    return target[2:] if target.startswith("./") else target


def classify(local, remote, base):
    """3 者比較。local / remote / base は md5 (無ければ None)。"""
    if local == remote:
        return "same"
    if remote is None:
        return "local-only"
    if local is None:
        if base is None:
            return "remote-only"
        return "local-deleted" if base == remote else "remote-updated-after-delete"
    if base is None:
        return "conflict"
    if local == base:
        return "remote-newer"
    if remote == base:
        return "local-newer"
    return "conflict"


def sync_kind(local, remote, base, marks):
    """classify に削除の印を加えたもの。marks はその名前の消された中身の md5 の集合。

    S3 にあるのが消された内容 (印を読まない古い VM が送り直したもの) で、手元に別の内容があるときは、
    pull はそれを取得しないので、手元が新しいものとして扱う (push が手元の内容で上書きする)。
    こうしないと、push は「競合」や「先に pull」で飛ばし、pull は何もせず、どちらでも直らない。
    """
    if local is not None and remote is not None and remote in marks and local not in marks:
        return "local-newer"
    return classify(local, remote, base)


STATUS_LABELS = {
    "same": "同期済み", "local-only": "手元だけ", "remote-only": "S3 だけ", "local-deleted": "S3 だけ (手元で消した)",
    "remote-updated-after-delete": "S3 が新しい (手元で消した後に更新)", "local-newer": "手元が新しい",
    "remote-newer": "S3 が新しい", "conflict": "競合",
}


class Memsync:
    def __init__(self, directory, uri, state_path, origin="local", dry_run=False, force=False, runner=None, out=None,
                 now=None):
        self.dir = Path(directory)
        self.uri = uri
        self.bucket, self.prefix = parse_uri(uri)
        self.state_path = Path(state_path)
        self.origin = re.sub(r"[^A-Za-z0-9._-]", "-", origin or "local") or "local"
        self.dry_run = dry_run
        self.force = force
        self.runner = runner or run_aws
        self.out = out or (lambda msg: print(msg, flush=True))
        self.now = now or utc_now   # 履歴の時刻 (UTC の datetime を返す関数。テストで固定できる)
        self.counts = {"get": 0, "put": 0, "conflict": 0, "skipped": 0, "deleted": 0}
        self.fetched = []           # pull で取得したファイル名 (フックで会話に渡す)
        self.deleted = []           # pull で手元から消したファイル名 (他のマシンで rm されたもの。フックで会話に渡す)
        self.conflicts = []         # 競合して飛ばしたファイル名
        self._remote_index = None
        self._deleted_etag = None   # S3 の .deleted.json の ETag (一覧に無ければ None)
        self._marks = None          # 名前 → 消された中身の md5 の集合 (必要なときに読む)
        self._remote_etags = {}     # 名前 → S3 の ETag (一覧を取ったときの値。条件付き書き込みに使う)
        self._conditional = True    # AWS CLI が --if-match / --if-none-match を知らなければ False

    # ---- 入出力
    def say(self, msg):
        self.out(("[dry-run] " if self.dry_run else "") + msg)

    def warn(self, msg):
        self.out("警告: " + msg)

    def aws(self, *args):
        rc, out, err = self.runner(list(args))
        if rc != 0:
            self.aws_failed(args, err)
        return out

    def aws_failed(self, args, err):
        if "AccessDenied" in err or "Access Denied" in err:
            raise MemsyncError("MicroVM の実行ロール (手元なら操作用のポリシー) に memory/ の権限がありません。"
                               "mvm infra deploy でスタックを更新してください (%s)" % err.strip())
        raise MemsyncError("aws %s に失敗しました: %s。ネットワークと AWS の認証を確かめて、もう一度実行してください"
                           % (" ".join(args[:2]), err.strip()))

    def key_uri(self, name):
        return "s3://%s/%s%s" % (self.bucket, self.prefix, name)

    # ---- 状態
    def load_state(self):
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            files = data.get("files", {})
            return {k: v for k, v in files.items() if isinstance(k, str) and isinstance(v, str)}
        except (OSError, ValueError, AttributeError):
            return {}

    def save_state(self, files):
        if self.dry_run:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps({"files": files}, indent=1, sort_keys=True), encoding="utf-8")

    # ---- 手元と S3 の一覧
    def scan_local(self):
        """名前 → (md5, 正規化後の中身)。MEMORY.md は含めない。"""
        result = {}
        if self.dir.is_dir():
            for p in sorted(self.dir.glob("*.md")):
                if p.is_file() and p.name != INDEX_NAME:
                    data = normalize_bytes(p.read_bytes())
                    result[p.name] = (md5_of(data), data)
        return result

    def list_remote(self):
        """名前 → md5 (ETag)。ETag が MD5 でないもの (マルチパートなど) は一致しない値にする。MEMORY.md は含めない。"""
        result = {}
        self._remote_index = False
        self._remote_etags = {}
        self._deleted_etag = None
        self._marks = None
        for obj in self.list_objects(self.prefix, delimiter=True):   # .history/ などの下は取ってこない
            name = obj["Key"][len(self.prefix):]
            if name == DELETED_NAME:
                self._deleted_etag = str(obj.get("ETag", "")).strip('"') or "unknown"
                continue
            if "/" in name or not name.endswith(".md"):
                continue
            if not safe_name(name):
                self.warn("S3 の %s は手元に置けない名前なので無視します (\\ や : を含むか、. で始まる)" % name)
                continue
            etag = str(obj.get("ETag", "")).strip('"')
            self._remote_etags[name] = etag
            if name == INDEX_NAME:
                self._remote_index = True
                continue
            result[name] = etag if re.fullmatch(r"[0-9a-f]{32}", etag) else "unknown:" + etag
        return result

    def list_objects(self, prefix, delimiter=False):
        """prefix で始まる S3 のオブジェクト ({"Key", "ETag", ...}) を、ページをたどりながら返す。

        delimiter=True なら prefix 直下のキーだけ (下のフォルダは CommonPrefixes になり、無視する)。履歴が溜まっても一覧が遅くならない。
        """
        token = None
        while True:
            args = ["s3api", "list-objects-v2", "--bucket", self.bucket, "--prefix", prefix, "--output", "json"]
            if delimiter:
                args += ["--delimiter", "/"]
            if token:
                args += ["--continuation-token", token]
            text = self.aws(*args).strip()
            data = json.loads(text) if text else {}
            for obj in data.get("Contents") or []:
                yield obj
            token = data.get("NextContinuationToken")
            if not token:
                return

    def local_reasons(self, data):
        """送らない理由 (無ければ空)。"""
        reasons = []
        if has_share_false(data):
            reasons.append("手元専用 (frontmatter に share: false)")
        if SECRET_PATTERN.search(data):
            reasons.append("認証情報 (API キー、トークン、秘密鍵など) らしい文字列を含む")
        if len(data) > MAX_FILE_BYTES:
            reasons.append("大きすぎる (%d KB。1 ファイル 1MB まで)" % (len(data) // 1024))
        return reasons

    # ---- 取得と送信
    def fetch(self, name):
        """S3 のオブジェクトの (中身, ETag)。"""
        with _temp_dir() as tmp:
            dest = Path(tmp) / "download"
            text = self.aws("s3api", "get-object", "--bucket", self.bucket, "--key", self.prefix + name,
                            "--output", "json", str(dest)).strip()
            try:
                etag = str((json.loads(text) if text else {}).get("ETag", "")).strip('"')
            except (ValueError, AttributeError):
                etag = ""
            return normalize_bytes(dest.read_bytes()), etag

    def download(self, name):
        return self.fetch(name)[0]

    def upload(self, name, data, etag=None):
        """条件付きで送る。etag は一覧や取得のときの S3 の ETag (None なら「まだ無いこと」を条件にする)。

        条件が合わなかった (その後に他のマシンが書き換えた) ときは False を返す。
        """
        with _temp_dir() as tmp:
            src = Path(tmp) / "body"       # name に / が入ることがある (.history/...) ので、名前は使わない
            src.write_bytes(data)
            args = ["s3api", "put-object", "--bucket", self.bucket, "--key", self.prefix + name, "--body", str(src),
                    "--metadata", "pmva-origin=" + self.origin, "--output", "json"]
            if self._conditional:
                rc, _, err = self.runner(args + (["--if-match", etag] if etag else ["--if-none-match", "*"]))
                if rc == 0:
                    return True
                if "PreconditionFailed" in err or "ConditionalRequestConflict" in err:
                    return False
                if not ("Unknown options" in err and "--if-" in err):
                    self.aws_failed(args, err)
                self._conditional = False
                self.warn("この AWS CLI は条件付き書き込み (--if-match / --if-none-match) に対応していないため、"
                          "他のマシンが同時に書き込んだときに上書きすることがあります。AWS CLI を新しくしてください")
            self.aws(*args)
            return True

    def put_plain(self, rel, data):
        """条件なしで送る (履歴用)。(終了コード, 標準エラー) を返す。"""
        with _temp_dir() as tmp:
            src = Path(tmp) / "body"
            src.write_bytes(data)
            rc, _, err = self.runner(["s3api", "put-object", "--bucket", self.bucket, "--key", self.prefix + rel,
                                      "--body", str(src), "--metadata", "pmva-origin=" + self.origin, "--output", "json"])
            return rc, err

    def record_history(self, name, data):
        """送った中身の写しを .history/<名前>/ に置く。失敗しても同期は止めない (警告だけ)。dry-run では書かない。

        本体を送れた後に呼ぶ。ここで AccessDenied なら、memory/* の権限はあって .history/ だけが拒否されている
        (スタックの外の拒否。スタックを更新しても直らない) ので、aws_failed の「mvm infra deploy」の案内は使わない。
        """
        if self.dry_run:
            return
        rel = "%s%s/%s-%s-%s.md" % (HISTORY_DIR, name, stamp_of(self.now()), self.origin, md5_of(data)[:8])
        rc, err = self.put_plain(rel, data)
        if rc == 0:
            return
        if "AccessDenied" in err or "Access Denied" in err:
            reason = ("メモリ本体は送れましたが、%s%s への書き込みだけが拒否されました。スタックの更新では直りません。"
                      "実行ロール (手元なら操作用のポリシー) に後から付けたポリシー、バケットポリシー、SCP などが "
                      "s3:PutObject を拒否していないか確かめてください: %s" % (self.prefix, HISTORY_DIR, err.strip()))
        else:
            reason = "ネットワークと AWS の認証を確かめてください: %s" % err.strip()
        self.warn("%s の履歴を残せませんでした。同期は続けます (%s)" % (name, reason))

    def write_local(self, name, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        dest = self.dir / name
        tmp = self.dir / (name + ".pmva-tmp")
        tmp.write_bytes(data)
        try:
            # 置き換える先を他のプロセス (エディタなど) が開いていると、Windows では PermissionError になる
            _retry_busy(os.replace, str(tmp), str(dest))
        except OSError:
            try:
                os.remove(str(tmp))
            except OSError:
                pass
            raise

    # ---- MEMORY.md (行の和集合)
    def merge_pull_index(self, local_names):
        """手元の索引に、S3 の索引の行のうち手元にファイルがあって索引に無いものを足す。"""
        if not self._remote_index:
            return
        remote_lines = normalize_bytes(self.download(INDEX_NAME)).decode("utf-8", errors="replace").split("\n")
        path = self.dir / INDEX_NAME
        local_text = normalize_bytes(path.read_bytes()).decode("utf-8", errors="replace") if path.is_file() else None
        local_lines = local_text.split("\n") if local_text is not None else []
        if local_lines and local_lines[-1] == "":
            local_lines.pop()
        have = {line_key(l) for l in local_lines} - {None}
        added = []
        for line in remote_lines:
            key = line_key(line)
            if key is None:
                if local_text is None and line.strip():
                    added.append(line)      # 手元に索引がまだ無いときは、見出しなどの行も取る
            elif key not in have and key in local_names:
                added.append(line)
                have.add(key)
        if not added:
            return
        merged = local_lines + added
        self.say("%s に %d 行を足します" % (INDEX_NAME, len(added)))
        self.check_index_size(merged)
        if not self.dry_run:
            self.write_local(INDEX_NAME, ("\n".join(merged) + "\n").encode("utf-8"))

    def merge_push_index(self, remote_names, private_names):
        """S3 の索引に、手元の索引の行のうち S3 にファイルがあって索引に無いものを足して送る。"""
        path = self.dir / INDEX_NAME
        if not path.is_file():
            return
        local_lines = normalize_bytes(path.read_bytes()).decode("utf-8", errors="replace").split("\n")
        if local_lines and local_lines[-1] == "":
            local_lines.pop()
        for attempt in range(INDEX_PUT_ATTEMPTS):
            if attempt:
                self.say("%s: 送る間に他のマシンが書き換えました。取り直してマージし直します" % INDEX_NAME)
                self.list_remote()
            merged = self.merged_push_index(local_lines, remote_names, private_names, attempt == 0)
            if merged is None or self.dry_run:
                return
            if self.upload(INDEX_NAME, *merged):
                return
        self.warn("%s を送れませんでした (他のマシンと書き込みが %d 回ぶつかりました)。しばらくしてからもう一度 push してください"
                  % (INDEX_NAME, INDEX_PUT_ATTEMPTS))

    def merged_push_index(self, local_lines, remote_names, private_names, first):
        """S3 の索引に手元の行を足したもの (中身, 送るときの条件の ETag)。足す行が無ければ None。"""
        remote_text, etag = None, None
        if self._remote_index:
            data, etag = self.fetch(INDEX_NAME)
            remote_text = data.decode("utf-8", errors="replace")
        remote_lines = remote_text.split("\n") if remote_text is not None else []
        if remote_lines and remote_lines[-1] == "":
            remote_lines.pop()
        have = {line_key(l) for l in remote_lines} - {None}
        added = []
        for line in local_lines:
            key = line_key(line)
            if SECRET_PATTERN.search(line.encode("utf-8")):
                if first:
                    self.warn("%s の認証情報らしい文字列を含む行は送りません" % INDEX_NAME)
                continue
            if key is None:
                if remote_text is None and line.strip():
                    added.append(line)      # S3 に索引がまだ無いときは、見出しなどの行も送る
            elif key not in have and key in remote_names and key not in private_names:
                added.append(line)
                have.add(key)
        if not added:
            return None
        merged = remote_lines + added
        self.say("%s に %d 行を足して送ります" % (INDEX_NAME, len(added)))
        self.check_index_size(merged)
        return ("\n".join(merged) + "\n").encode("utf-8"), etag

    def check_index_size(self, lines):
        size = len(("\n".join(lines) + "\n").encode("utf-8"))
        if len(lines) > INDEX_MAX_LINES or size > INDEX_MAX_BYTES:
            self.warn("%s が %d 行 (%d バイト) になりました。Claude Code は先頭 %d 行 / 25KB までしか読みません。"
                      "古い項目をまとめるか消してください" % (INDEX_NAME, len(lines), size, INDEX_MAX_LINES))

    # ---- 索引の行の編集 (rm / revert / pull で消された行)
    def edit_local_index(self, transform):
        """手元の MEMORY.md の行を transform(行のリスト) -> 行のリスト で書き換える。変わらなければ書かない。変えたら True。"""
        path = self.dir / INDEX_NAME
        if not path.is_file():
            return False
        lines = normalize_bytes(path.read_bytes()).decode("utf-8", errors="replace").split("\n")
        new = transform(lines)
        if new == lines:
            return False
        if not self.dry_run:
            self.write_local(INDEX_NAME, "\n".join(new).encode("utf-8"))
        return True

    def edit_remote_index(self, transform):
        """S3 の MEMORY.md を条件付きで書き換える。他のマシンとぶつかったら取り直して INDEX_PUT_ATTEMPTS 回まで繰り返す。

        変わらなければ送らない。送れた (または送る必要が無かった) なら True。dry-run では何も送らない。
        """
        for attempt in range(INDEX_PUT_ATTEMPTS):
            text, etag = None, None
            if self._remote_index:
                data, etag = self.fetch(INDEX_NAME)
                text = data.decode("utf-8", errors="replace")
            lines = text.split("\n") if text is not None else []
            new = transform(lines)
            if new == lines or self.dry_run:
                return True
            body = "\n".join(new)
            if text is None and not body.endswith("\n"):
                body += "\n"
            if self.upload(INDEX_NAME, body.encode("utf-8"), etag):
                self._remote_index = True
                return True
            self.say("%s: 送る間に他のマシンが書き換えました。取り直します" % INDEX_NAME)
        return False

    # ---- 削除の印 (.deleted.json)
    def read_deleted(self):
        """S3 の .deleted.json → ({名前: [印]}, ETag)。list_remote で見つからなければ ({}, None)。壊れていれば ValueError。"""
        if not self._deleted_etag:
            return {}, None
        data, etag = self.fetch(DELETED_NAME)
        return parse_deleted(data), etag

    def deleted_marks(self):
        """名前 → 消された中身の md5 の集合。.deleted.json があるときだけ S3 から読む (壊れていれば警告して無いものとする)。"""
        if self._marks is None:
            self._marks = {}
            try:
                files, _ = self.read_deleted()
                self._marks = {n: {e["md5"] for e in entries} for n, entries in files.items() if entries}
            except ValueError:
                self.warn("S3 の %s を読めません (JSON の形が違います)。削除の印は無視します" % DELETED_NAME)
        return self._marks

    def update_deleted(self, mutate):
        """.deleted.json を mutate(files) で書き換えて条件付きで送る (ぶつかったら取り直す)。送れたら True。

        mutate は files ({名前: [印]}) をその場で直し、変えたら True を返す。変えなければ送らない。
        """
        for attempt in range(INDEX_PUT_ATTEMPTS):
            if attempt:
                self.say("%s: 送る間に他のマシンが書き換えました。取り直します" % DELETED_NAME)
                self.list_remote()
            try:
                files, etag = self.read_deleted()
            except ValueError as e:
                raise MemsyncError("S3 の %s を読めません (%s)。中身を確かめて、直すか aws s3 rm で消してから、もう一度実行してください"
                                   % (DELETED_NAME, e))
            if not mutate(files):
                return True
            body = json.dumps({"files": files}, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
            if self.upload(DELETED_NAME, body, etag):
                self._deleted_etag = self._deleted_etag or "written"
                self._marks = None
                return True
        return False

    # ---- コマンド
    def summary(self):
        c = self.counts
        self.out(("[dry-run] " if self.dry_run else "") +
                 "取得 %d、送信 %d、競合 %d、飛ばした %d" % (c["get"], c["put"], c["conflict"], c["skipped"]) +
                 ("、削除 %d" % c["deleted"] if c["deleted"] else ""))
        if c["conflict"]:
            self.out("競合したファイルは、--force で、pull なら S3 の内容、push なら手元の内容を採用できます")
        return 0

    def pull(self):
        local, remote, state = self.scan_local(), self.list_remote(), self.load_state()
        new_state = dict(state)
        local_names = set(local)
        marks = self.deleted_marks()
        removed = []
        for name in sorted(set(local) | set(remote) | set(state)):
            l = local[name][0] if name in local else None
            r = remote.get(name)
            base = state.get(name)
            mk = marks.get(name, ())
            if l is not None and l in mk and (r is None or r in mk):
                if has_share_false(local[name][1]):
                    self.say("%s: 他のマシンで消されたメモリですが、手元専用 (share: false) なので消しません" % name)
                    self.counts["skipped"] += 1
                    continue
                self.say("他のマシンで消されたメモリ: %s (手元からも消します)" % name)
                self.counts["deleted"] += 1
                local_names.discard(name)
                new_state.pop(name, None)
                removed.append(name)
                if not self.dry_run:
                    try:
                        _retry_busy(os.remove, str(self.dir / name))
                    except OSError as e:
                        self.warn("%s を消せませんでした (%s)" % (name, e))
                        continue
                    self.deleted.append(name)
                continue
            if r is not None and r in mk and (l is None or l not in mk):
                # 消された中身が S3 に残っている (古い VM が送り直した)。取得しない
                if l is None:
                    self.say("%s: mvm memory rm で消されたメモリの内容が S3 に残っています。取得しません" % name)
                    self.counts["skipped"] += 1
                    new_state.pop(name, None)
                continue
            kind = classify(l, r, base)
            if kind == "same":
                if l is None:
                    new_state.pop(name, None)
                else:
                    new_state[name] = l
                continue
            if kind == "local-deleted":
                self.say("%s: 手元で消したファイル (S3 には残っています)。取得しません" % name)
                self.counts["skipped"] += 1
                continue
            take = kind in ("remote-only", "remote-newer", "remote-updated-after-delete")
            if kind == "conflict":
                if not self.force:
                    self.say("%s: 競合 (手元と S3 の両方が変わっています)。飛ばします" % name)
                    self.counts["conflict"] += 1
                    self.conflicts.append(name)
                    continue
                take = True
            if not take:
                continue
            if l is not None and has_share_false(local[name][1]):
                self.say("%s: 手元専用 (share: false) なので上書きしません" % name)
                self.counts["skipped"] += 1
                continue
            self.say("取得: %s%s" % (name, " (競合を S3 の内容で上書き)" if kind == "conflict" else ""))
            self.counts["get"] += 1
            local_names.add(name)
            if not self.dry_run:
                data = self.download(name)
                self.write_local(name, data)
                new_state[name] = md5_of(data)
                self.fetched.append(name)
        if removed:
            names = set(removed)
            if self.edit_local_index(lambda lines: [l for l in lines if line_key(l) not in names]):
                self.say("%s から、消されたメモリの行を消します" % INDEX_NAME)
        self.merge_pull_index(local_names)
        self.save_state(new_state)
        return self.summary()

    def push(self):
        local, remote, state = self.scan_local(), self.list_remote(), self.load_state()
        new_state = dict(state)
        remote_names = set(remote)
        private_names = {n for n, (_, data) in local.items() if has_share_false(data)}
        marks = self.deleted_marks()
        for name in sorted(set(local) | set(remote) | set(state)):
            l = local[name][0] if name in local else None
            r = remote.get(name)
            base = state.get(name)
            if l is not None and l in marks.get(name, ()):
                private_names.add(name)         # 索引の行も送らない
                self.say("%s: mvm memory rm で消されたメモリなので送りません (手元のファイルは pull で消えます)" % name)
                self.counts["skipped"] += 1
                continue
            kind = sync_kind(l, r, base, marks.get(name, ()))
            if kind == "same":
                if l is None:
                    new_state.pop(name, None)
                else:
                    new_state[name] = l
                continue
            if kind == "remote-newer":
                self.say("%s: S3 のほうが新しい。先に pull してください。飛ばします" % name)
                self.counts["skipped"] += 1
                continue
            send = kind in ("local-only", "local-newer")
            if kind == "conflict":
                if not self.force:
                    self.say("%s: 競合 (手元と S3 の両方が変わっています)。飛ばします" % name)
                    self.counts["conflict"] += 1
                    self.conflicts.append(name)
                    continue
                send = True
            if not send:
                continue
            data = local[name][1]
            reasons = self.local_reasons(data)
            if reasons:
                self.say("%s: 送りません (%s)" % (name, "、".join(reasons)))
                self.counts["skipped"] += 1
                continue
            remote_names.add(name)
            if not self.dry_run and not self.upload(name, data, self._remote_etags.get(name) if r is not None else None):
                self.say("%s: 競合 (送る間に他のマシンが書き換えました)。飛ばします。pull してから確かめてください" % name)
                self.counts["conflict"] += 1
                self.conflicts.append(name)
                continue
            self.say("送信: %s%s" % (name, " (競合を手元の内容で上書き)" if kind == "conflict" else ""))
            self.counts["put"] += 1
            if not self.dry_run:
                new_state[name] = md5_of(data)
                self.record_history(name, data)
        for name in list(local):
            if self.local_reasons(local[name][1]):
                private_names.add(name)
        self.merge_push_index(remote_names, private_names)
        self.save_state(new_state)
        return self.summary()

    def status(self):
        local, remote, state = self.scan_local(), self.list_remote(), self.load_state()
        names = sorted(set(local) | set(remote))
        if not names:
            self.out("メモリのファイルはありません (手元: %s)" % self.dir)
        for name in names:
            l = local[name][0] if name in local else None
            r = remote.get(name)
            marks = self.deleted_marks().get(name, ())
            kind = sync_kind(l, r, state.get(name), marks)
            label = STATUS_LABELS[kind]
            if kind != classify(l, r, state.get(name)):
                label = "手元が新しい (S3 にあるのは mvm memory rm で消された内容が送り直されたもの。push で手元の内容に置き換えます)"
            if l is not None and l in marks:
                label = "消された (mvm memory rm)。pull で手元から消えます"
            elif l is None and r is not None and r in marks:
                # 印を読まない古い VM (イメージ 8.0 以前) が送り直したもの。pull は取得しない
                label = ("S3 だけ (mvm memory rm で消された内容が送り直されたもの。取得しません。"
                         "手元の PC で mvm memory rm %s を実行すると S3 から消せます)" % name)
            elif name in local and kind in ("local-only", "local-newer", "conflict"):
                reasons = self.local_reasons(local[name][1])
                if reasons:
                    label = "送らない: " + "、".join(reasons)
            self.out("%-40s %s" % (name, label))
        return 0

    # ---- 履歴・削除・戻す
    @property
    def lock_path(self):
        return Path(str(self.state_path) + ".lock")

    @contextlib.contextmanager
    def locked(self, wait, say_waiting=False):
        """同じ状態ファイルを使う同期 (会話のフック、mvm memory、VM の snapshot.sh) を 1 つずつにする。

        wait 秒待っても取れなければ LockBusy。持っている間は AWS CLI を呼ぶたびにロックの時刻を新しくして、
        長い同期 (Windows の aws CLI は 1 回 1〜2 秒かかる) が LOCK_STALE 秒を超えても他に奪われないようにする。
        """
        lock = self.lock_path
        if not _acquire_lock(lock, 0):
            if say_waiting:
                self.say("他の同期 (会話のフックなど) が終わるのを待っています (最長 %d 秒)" % wait)
            if not _acquire_lock(lock, wait):
                raise LockBusy(lock, wait)
        runner = self.runner

        def touching(args):
            try:
                os.utime(str(lock), None)
            except OSError:
                pass
            return runner(args)
        self.runner = touching
        try:
            yield
        finally:
            self.runner = runner
            _release_lock(lock)     # 残ると、LOCK_STALE 秒たつまで他の同期が待たされる

    def run_action(self, action, name=None, only_origin=None, since=None, as_json=False, to=None, lock_wait=None):
        """コマンド名から実行する (bin/mvm.py と main() が使う)。終了コードを返す。

        書き換える操作は、会話のフックと同じロックを取ってから行う (dry-run は何も書かないので取らない)。
        lock_wait はロックを待つ秒数 (既定 CLI_LOCK_WAIT)。
        """
        if action in LOCKED_ACTIONS and not self.dry_run:
            with self.locked(CLI_LOCK_WAIT if lock_wait is None else lock_wait, say_waiting=True):
                return self._run_action(action, name, only_origin, since, as_json, to)
        return self._run_action(action, name, only_origin, since, as_json, to)

    def _run_action(self, action, name, only_origin, since, as_json, to):
        if action == "log":
            return self.history_log(name, only_origin, since, as_json)
        if action == "revert":
            return self.revert(name, to)
        if action == "rm":
            return self.rm(name)
        return getattr(self, action)()

    def history_entries(self, name=None):
        """.history/ の一覧 (名前を指定すればその名前だけ)。{at, origin, name, md5 (8 文字), key, action: "write"} のリスト。"""
        entries = []
        for obj in self.list_objects(self.prefix + HISTORY_DIR + (name + "/" if name else "")):
            entry = parse_history_key(obj["Key"][len(self.prefix):])
            if entry:
                entry["action"] = "write"
                entries.append(entry)
        return entries

    def index_line(self, name):
        """S3 (無ければ手元) の MEMORY.md にある、この名前を指す行。無ければ None。"""
        texts = []
        if self._remote_index:
            texts.append(self.download(INDEX_NAME).decode("utf-8", errors="replace"))
        path = self.dir / INDEX_NAME
        if path.is_file():
            texts.append(normalize_bytes(path.read_bytes()).decode("utf-8", errors="replace"))
        for text in texts:
            for line in text.split("\n"):
                if line_key(line) == name:
                    return line
        return None

    def remote_md5(self, name, remote):
        r = remote.get(name)
        if r is not None and r.startswith("unknown:"):
            return md5_of(self.download(name))        # マルチパートの ETag は MD5 ではないので、中身から計算する
        return r

    def rm(self, name):
        """メモリを消す。削除の印 → S3 のファイル → S3 の索引の行 → 手元のファイルと索引の行、の順 (途中で止まっても、もう一度実行できる)。"""
        check_memory_name(name)
        local, remote, state = self.scan_local(), self.list_remote(), self.load_state()
        md5s = []
        for m in (local[name][0] if name in local else None, self.remote_md5(name, remote), state.get(name)):
            if m and m not in md5s:
                md5s.append(m)
        if not md5s:
            raise MemsyncError("そのメモリはありません: %s (手元にも S3 にも、同期の記録にもありません)。"
                               "名前は mvm memory status で確かめてください" % name)
        line = self.index_line(name)
        self.say("削除の印を付けます: %s (中身 %s)" % (name, "、".join(m[:8] for m in md5s)))
        if name in remote:
            self.say("S3 から消します: %s" % name)
        if line is not None:
            self.say("%s から行を消します: %s" % (INDEX_NAME, line))
        if name in local:
            self.say("手元から消します: %s" % (self.dir / name))
        if self.dry_run:
            return 0

        at = iso_of(self.now())

        def mark(files):
            kept = [e for e in files.get(name, []) if e["md5"] not in md5s]
            new = [{"md5": m, "at": at, "by": self.origin, "line": line} for m in md5s]
            files[name] = (new + kept)[:DELETED_KEEP]
            return True
        if not self.update_deleted(mark):
            raise MemsyncError("削除の印 (%s) を書けませんでした (他のマシンと書き込みが %d 回ぶつかりました)。"
                               "しばらくしてから、もう一度実行してください" % (DELETED_NAME, INDEX_PUT_ATTEMPTS))
        if name in remote:
            args = ["s3api", "delete-object", "--bucket", self.bucket, "--key", self.prefix + name]
            rc, _, err = self.runner(args)
            if rc != 0:
                if "AccessDenied" in err or "Access Denied" in err:
                    raise MemsyncError("S3 のメモリ %s を消す権限がありません。mvm memory rm は手元の PC (操作用の認証情報) で実行してください。"
                                       "VM の実行ロールには s3:DeleteObject を付けていません。"
                                       "削除の印は書き込み済みで、権限のある環境でもう一度実行すれば続きから消せます (%s)"
                                       % (name, err.strip()))
                self.aws_failed(args, err)
        if not self.edit_remote_index(lambda lines: [l for l in lines if line_key(l) != name]):
            self.warn("S3 の %s から行を消せませんでした (他のマシンと書き込みが %d 回ぶつかりました)。"
                      "もう一度 mvm memory rm %s を実行してください" % (INDEX_NAME, INDEX_PUT_ATTEMPTS, name))
        if name in local:
            try:
                _retry_busy(os.remove, str(self.dir / name))
            except OSError as e:
                self.warn("手元の %s を消せませんでした (%s)。手で消してください" % (name, e))
        self.edit_local_index(lambda lines: [l for l in lines if line_key(l) != name])
        state.pop(name, None)
        self.save_state(state)
        self.out("消しました: %s。ほかのマシンは、次の pull で手元からも消します" % name)
        return 0

    def history_log(self, name=None, only_origin=None, since=None, as_json=False):
        """.history/ と削除の印から、いつ・どのマシンが・どのメモリを書いた (消した) かを古い順に表示する。"""
        if name:
            check_memory_name(name)
        since_at = parse_since(since, self.now()) if since else None
        entries = self.history_entries(name)
        self._deleted_etag = None
        for obj in self.list_objects(self.prefix + DELETED_NAME):
            if obj["Key"] == self.prefix + DELETED_NAME:
                self._deleted_etag = str(obj.get("ETag", "")).strip('"') or "unknown"
        try:
            files, _ = self.read_deleted()
        except ValueError:
            self.warn("S3 の %s を読めません (JSON の形が違います)。削除の行は表示しません" % DELETED_NAME)
            files = {}
        for n, marks in files.items():
            for e in marks if not name or n == name else []:
                try:
                    at = parse_time(str(e.get("at")))
                except ValueError:
                    continue
                entries.append({"at": at, "origin": str(e.get("by") or "?"), "action": "delete", "name": n,
                                "md5": e["md5"], "key": DELETED_NAME})
        entries = [e for e in entries if (not only_origin or e["origin"] == only_origin)
                   and (since_at is None or e["at"] >= since_at)]
        entries.sort(key=lambda e: (e["at"], e["action"] == "delete", e["name"], e["key"]))
        if as_json:
            self.out(json.dumps([{"at": iso_of(e["at"]), "origin": e["origin"], "action": e["action"], "name": e["name"],
                                  "md5": e["md5"], "key": e["key"]} for e in entries], ensure_ascii=False, indent=1))
            return 0
        if not entries:
            self.out("履歴はありません。段階 3 より前に送られたメモリには履歴がありません "
                     "(履歴は、段階 3 の版で送ったときから残ります。次に変更を送ると記録が始まります)")
            return 0
        for e in entries:
            self.out("%s  %-14s %-4s %s" % (e["at"].strftime("%Y-%m-%d %H:%M:%SZ"), e["origin"],
                                            "消した" if e["action"] == "delete" else "書いた", e["name"]))
        return 0

    def revert(self, name, to=None):
        """メモリを履歴の版に戻す (既定は 1 つ前、--to で時刻を指定)。"""
        check_memory_name(name)
        target_time = None
        if to:
            try:
                target_time = parse_time(to)
            except ValueError:
                raise MemsyncError("--to の値を解釈できません: %s。20261124T101503Z (mvm memory log の時刻) か、"
                                   "2026-11-24T10:15:03Z の形で指定してください" % to)
        local, remote, state = self.scan_local(), self.list_remote(), self.load_state()
        l = local[name][0] if name in local else None
        r = self.remote_md5(name, remote)
        if l is not None and l != state.get(name) and l != r and not self.force:
            raise MemsyncError("%s は手元に、まだ S3 と同期していない変更があります。戻すとその変更が消えます。"
                               "先に mvm memory push で送るか、捨ててよければ --force を付けて、もう一度実行してください" % name)
        current = r if r is not None else l
        history = sorted(self.history_entries(name), key=lambda e: (e["at"], e["key"]), reverse=True)
        if not history:
            raise MemsyncError("%s の履歴がありません。履歴は、段階 3 の版で送ったメモリにだけ残ります。"
                               "mvm memory log %s で確かめてください" % (name, name))
        if target_time:
            candidates = [e for e in history if e["at"] <= target_time]
            if not candidates:
                raise MemsyncError("--to %s 以前の履歴がありません。mvm memory log %s で時刻を確かめてください" % (to, name))
        else:
            candidates = [e for e in history if current is None or not current.startswith(e["md5"])]
            if not candidates:
                raise MemsyncError("%s には、今の内容と違う履歴がありません。戻せる版がありません" % name)
        chosen = candidates[0]
        if current is not None and current.startswith(chosen["md5"]):
            self.out("%s は、すでにその版 (%s) の内容です。何もしません" % (name, stamp_of(chosen["at"])))
            return 0
        data = self.download(chosen["key"])
        full = md5_of(data)
        if not full.startswith(chosen["md5"]):
            raise MemsyncError("履歴 %s の中身が、キーの md5 と合いません。この版には戻せません。"
                               "mvm memory log %s で別の版を探し、--to で指定してください" % (chosen["key"], name))
        self.say("戻す版: %s (書いたマシン %s、md5 %s)" % (stamp_of(chosen["at"]), chosen["origin"], chosen["md5"]))
        if self.dry_run:
            for line in data.decode("utf-8", errors="replace").split("\n")[:5]:
                self.out("    | " + line)
            self.say("S3 と手元の %s をこの版にします。削除の印に同じ中身があれば外します" % name)
            return 0
        if not self.upload(name, data, self._remote_etags.get(name) if r is not None else None):
            raise MemsyncError("%s を送る間に他のマシンが書き換えました。mvm memory pull で確かめてから、もう一度実行してください" % name)
        self.record_history(name, data)
        self.write_local(name, data)
        state[name] = full
        self.save_state(state)
        lines = []

        def unmark(files):
            del lines[:]
            entries = files.get(name) or []
            hit = [e for e in entries if e["md5"] == full]
            if not hit:
                return False
            lines.extend(e["line"] for e in hit if e.get("line"))
            rest = [e for e in entries if e["md5"] != full]
            if rest:
                files[name] = rest
            else:
                del files[name]
            return True
        if not self.update_deleted(unmark):
            self.warn("削除の印 (%s) を外せませんでした。他のマシンが pull でこのメモリを消すかもしれません。"
                      "もう一度 mvm memory revert %s を実行してください" % (DELETED_NAME, name))
        if lines:
            line = lines[0]

            def add_line(old):
                if any(line_key(x) == name for x in old):
                    return old
                return old[:-1] + [line, ""] if old and old[-1] == "" else old + [line]
            if (self.dir / INDEX_NAME).is_file():
                self.edit_local_index(add_line)
            else:
                self.write_local(INDEX_NAME, (line + "\n").encode("utf-8"))
            if not self.edit_remote_index(add_line):
                self.warn("S3 の %s に行を戻せませんでした。もう一度 mvm memory push を実行してください" % INDEX_NAME)
        self.out("戻しました: %s を %s の版 (書いたマシン %s) にしました" % (name, stamp_of(chosen["at"]), chosen["origin"]))
        return 0


# ---------------------------------------------------------------- Claude Code のフック

def _same_dir(a, b):
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _acquire_lock(path, wait, stale=LOCK_STALE):
    """同じマシンで同時に動く同期 (複数の会話のフック) を 1 つにする。取れなければ False。

    同期の途中で落ちて残ったロックは、stale 秒より古ければ奪う。
    Windows では、他が外している最中 (削除待ち) のロックを作ろうとすると FileExistsError ではなく
    PermissionError になるので、どちらも「まだ他が持っている」として待って取り直す。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except (FileExistsError, PermissionError):
            try:
                if time.time() - path.stat().st_mtime > stale:
                    os.remove(str(path))
                    continue
            except OSError:
                pass        # もう外れた、または外している最中。少し待って取り直す
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


def _release_lock(path):
    try:
        _retry_busy(os.remove, str(path))
    except OSError:
        pass


def _hook_log(sync, event, msg):
    """フックの失敗を状態ファイルの隣 (<状態ファイル>.log) に残す (フックの標準エラーは利用者に見えないことが多い)。"""
    log = Path(str(sync.state_path) + ".log")
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        if log.is_file() and log.stat().st_size > HOOK_LOG_MAX:
            log.write_bytes(log.read_bytes()[-HOOK_LOG_MAX // 2:])
        with open(str(log), "a", encoding="utf-8") as f:
            f.write("%s %s: %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), event, msg))
    except OSError:
        pass


def hook_context(sync, with_conflicts):
    """pull で届いたメモリ (と競合) を、会話に渡す文章にする。何も無ければ空。"""
    parts, omitted = [], []
    head = ("[共有メモリ] 他のマシン (MicroVM や手元の PC) で保存・更新された Claude Code の自動メモリを S3 から取得しました"
            " (メモリのフォルダ: %s)。今後の作業ではこの内容も踏まえてください。" % sync.dir)
    used = len(head) + 600          # 末尾の案内の分を残す
    for name in sync.fetched:
        try:
            body = (sync.dir / name).read_text(encoding="utf-8", errors="replace").rstrip()
        except OSError:
            continue
        block = "\n\n## %s\n%s" % (name, body)
        if used + len(block) > HOOK_CONTEXT_MAX:
            omitted.append(name)
            continue
        parts.append(block)
        used += len(block)
    tail = []
    if omitted:
        names = "、".join(omitted)
        if len(names) > 300:
            names = names[:300] + " ほか"
        tail.append("\n\n文字数の上限のため本文を省いたファイル: %s。必要なら %s の中のこれらのファイルを読んでください"
                    % (names, sync.dir))
    if sync.deleted:
        names = "、".join(sync.deleted)
        if len(names) > 300:
            names = names[:300] + " ほか"
        tail.append("\n\n他のマシンで消されたメモリ: %s (手元からも消しました。この記憶には頼らないでください)" % names)
    if with_conflicts and sync.conflicts:
        tail.append("\n\n競合して同期していないメモリ: %s (手元と S3 の両方で変わっています)。"
                    "利用者に伝え、手元の PC で mvm memory status を実行して確かめるよう頼んでください" % "、".join(sync.conflicts))
    if not parts and not omitted and not tail:
        return ""
    return (head + "".join(parts) + "".join(tail))[:HOOK_CONTEXT_MAX]


def run_hook(event, sync, stdin_text, min_interval=0, now=time.time, lock_wait=10):
    """Claude Code のフックから呼ぶ同期。標準出力に書く応答 (JSON の文字列か空) を返し、例外は投げない。

      post-tool      PostToolUse (Write / Edit)。書いたファイルがメモリのフォルダの中なら push する
      session-start  SessionStart。pull して、届いたメモリと競合を会話に渡す
      prompt         UserPromptSubmit。前回の pull から min_interval 秒以上たっていれば pull して、届いたメモリを会話に渡す
    """
    try:
        payload = json.loads(stdin_text or "{}")
    except ValueError:
        payload = None
    if not isinstance(payload, dict):
        return ""
    if event == "post-tool":
        tool_input = payload.get("tool_input")
        path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
        if not isinstance(path, str) or not path.endswith(".md") or not _same_dir(Path(path).parent, sync.dir):
            return ""
    stamp = Path(str(sync.state_path) + ".pulled")
    if event == "prompt" and min_interval > 0:
        try:
            if now() - float(stamp.read_text(encoding="utf-8").strip()) < min_interval:
                return ""
        except (OSError, ValueError):
            pass
    try:
        # 送信は飛ばすと次に書くまで送られないので長めに待つ (async なので会話は待たせない)
        with sync.locked(lock_wait * 6 if event == "post-tool" else lock_wait):
            if event == "post-tool":
                sync.push()
                return ""
            stamp.write_text(str(now()), encoding="utf-8")
            sync.pull()
            text = hook_context(sync, with_conflicts=(event == "session-start"))
        if not text:
            return ""
        # ASCII だけの JSON にする (Windows の標準出力の文字コードに左右されない)
        return json.dumps({"hookSpecificOutput": {"hookEventName": HOOK_EVENTS[event], "additionalContext": text}})
    except LockBusy as e:
        sync.out("他の同期が動いているので、今回は飛ばします (%s)" % e.lock)
        return ""
    except Exception as e:      # フックの失敗で会話を止めない
        sync.out("error: %s" % e)
        _hook_log(sync, event, str(e))
        return ""


def main(argv=None, runner=None, out=None, stdin=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    hook = argv[:1] == ["hook"]
    p = argparse.ArgumentParser(prog="memsync.py", description="Claude Code の自動メモリを S3 と同期する")
    p.add_argument("action", choices=("pull", "push", "status", "hook", "log", "revert", "rm"))
    p.add_argument("event", nargs="?", metavar="event|名前",
                   help="hook のときのイベント (%s)。log / revert / rm のときはメモリの名前 (user_role.md など)" % " / ".join(HOOK_EVENTS))
    p.add_argument("--since", help="log で、この期間・日時より後だけ表示する (30m / 2h / 3d、または 2026-11-24T10:00:00Z、2026-11-24)")
    p.add_argument("--json", action="store_true", help="log を JSON の配列で表示する")
    p.add_argument("--to", help="revert で、戻す版の時刻 (20261124T101503Z や 2026-11-24T10:15:03Z。その時刻以前で最新の版)")
    p.add_argument("--min-interval", type=float, default=0,
                   help="hook prompt で、前回の pull からこの秒数がたつまでは S3 を見に行かない (既定 0)")
    p.add_argument("--lock-wait", type=float, default=None,
                   help="pull / push / rm / revert で、他の同期 (会話のフックなど) が終わるのを待つ秒数 (既定 %d)。"
                        "VM の終了時の保存は打ち切られるまでの時間が短いので、snapshot.sh は短くする" % CLI_LOCK_WAIT)
    p.add_argument("--region", help="AWS のリージョン (フックの環境に AWS_REGION が無いことがあるため明示する)")
    p.add_argument("--dir", required=True, help="メモリのフォルダ")
    p.add_argument("--uri", required=True, help="s3://<バケット>/memory/<repo-id>/")
    p.add_argument("--state", required=True, help="最後に同期したときの状態を置くファイル (メモリのフォルダの外)")
    p.add_argument("--origin", default=None,
                   help="S3 のオブジェクトに付ける送り元の名前 (既定 local)。log では、この名前のマシンが書いたものだけに絞る")
    p.add_argument("--dry-run", action="store_true", help="何も書き換えず、することだけ表示")
    p.add_argument("--force", action="store_true",
                   help="競合したときに、pull は S3、push は手元の内容を採用する。revert では手元の未同期の変更を捨てて戻す")
    try:
        args = p.parse_args(argv)
    except SystemExit:
        if hook:
            return 0        # フックが 2 で終わると、Claude Code は利用者の指示を止めてしまう
        raise
    if args.region:
        os.environ["AWS_REGION"] = os.environ["AWS_DEFAULT_REGION"] = args.region
    if hook:
        # 標準出力は会話に入るので、経過は標準エラーに出す
        out = out or (lambda msg: print(msg, file=sys.stderr, flush=True))
        try:
            sync = Memsync(Path(args.dir).expanduser(), args.uri, args.state, args.origin or "local", runner=runner, out=out)
        except MemsyncError as e:
            print("error: %s" % e, file=sys.stderr)
            return 0
        if args.event not in HOOK_EVENTS:
            print("error: hook のイベント (%s) を指定してください" % " / ".join(HOOK_EVENTS), file=sys.stderr)
            return 0
        text = stdin.read() if stdin else sys.stdin.buffer.read().decode("utf-8", errors="replace")
        result = run_hook(args.event, sync, text, min_interval=args.min_interval)
        if result:
            print(result)
        return 0
    if args.event and args.action not in ("log", "revert", "rm"):
        print("error: %s に余分な引数があります: %s" % (args.action, args.event), file=sys.stderr)
        return 1
    try:
        sync = Memsync(Path(args.dir).expanduser(), args.uri, args.state, (args.origin if args.action != "log" else None) or "local",
                       args.dry_run, args.force, runner, out)
        return sync.run_action(args.action, args.event, args.origin, args.since, args.json, args.to, args.lock_wait)
    except MemsyncError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
