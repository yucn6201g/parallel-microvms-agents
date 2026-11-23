#!/usr/bin/env python3
"""Claude Code の自動メモリ (auto memory) を S3 経由で手元 PC と MicroVM で共有する。

手元の `mvm memory` (bin/mvm.py が読み込む) と、MicroVM の bootstrap.sh / snapshot.sh (CLI) の両方がこの 1 つの実装を使う。
Python 標準ライブラリと AWS CLI だけで動く (Python 3.9 以上)。

  memsync.py {pull|push|status} --dir <メモリのフォルダ> --uri s3://<bucket>/memory/<repo-id>/
             --state <状態ファイル> [--origin <名前>] [--dry-run] [--force]
  memsync.py hook {session-start|prompt|post-tool} --dir ... --uri ... --state ... [--min-interval <秒>]
             Claude Code のフックから呼ぶ。イベントの JSON を標準入力で受け取り、応答の JSON だけを標準出力に書く

メモリは 1 件 1 ファイルの *.md と、索引の MEMORY.md。*.md は「最後に同期したときの内容」を基準にした 3 者比較
(手元 / S3 / 基準) で、MEMORY.md は行の和集合でマージする。削除は相手に伝えない。
S3 への書き込みは条件付き (--if-match / --if-none-match) にして、一覧を取ってから送るまでの間に他のマシンが
書き換えたものを黙って上書きしない (ファイルは競合として飛ばし、MEMORY.md は取り直してマージし直す)。
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

INDEX_NAME = "MEMORY.md"
MAX_FILE_BYTES = 1024 * 1024      # これを超えるファイルは送らない
INDEX_MAX_LINES = 200             # Claude Code が読む索引の上限 (先頭 200 行 / 25KB)
INDEX_MAX_BYTES = 25 * 1024
SANITIZED_MAX = 200               # これを超えると Claude Code はフォルダ名を切り詰めてハッシュを付ける (規則が不明)
INDEX_PUT_ATTEMPTS = 3            # MEMORY.md を送るときに、他のマシンと書き込みがぶつかったら取り直す回数
HOOK_CONTEXT_MAX = 9500           # フックで会話に渡す文字数 (Claude Code は 1 万文字を超えるとファイルに逃がす)
HOOK_LOG_MAX = 256 * 1024
# フックの名前 (memsync.py hook <名前>) → Claude Code のイベント名
HOOK_EVENTS = {"session-start": "SessionStart", "prompt": "UserPromptSubmit", "post-tool": "PostToolUse"}

# bin/mvm.py の SECRET_PATTERN と同じ正規表現 (認証情報らしい文字列)
SECRET_PATTERN = re.compile(
    rb"(sk-ant-[A-Za-z0-9_-]{16,}|sk-(proj-)?[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    rb"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[abprs]-[A-Za-z0-9-]{10,}"
    rb"|hooks\.slack\.com/services/[A-Za-z0-9/]+)")

LINK_PATTERN = re.compile(r"\]\(([^)\s#]+\.md)(?:#[^)]*)?\)")
SHARE_FALSE_PATTERN = re.compile(r"^share:\s*[\"']?false[\"']?\s*(#.*)?$", re.IGNORECASE)


class MemsyncError(Exception):
    """利用者に次の行動を伝えて終わるエラー。"""


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


STATUS_LABELS = {
    "same": "同期済み", "local-only": "手元だけ", "remote-only": "S3 だけ", "local-deleted": "S3 だけ (手元で消した)",
    "remote-updated-after-delete": "S3 が新しい (手元で消した後に更新)", "local-newer": "手元が新しい",
    "remote-newer": "S3 が新しい", "conflict": "競合",
}


class Memsync:
    def __init__(self, directory, uri, state_path, origin="local", dry_run=False, force=False, runner=None, out=None):
        self.dir = Path(directory)
        self.uri = uri
        self.bucket, self.prefix = parse_uri(uri)
        self.state_path = Path(state_path)
        self.origin = re.sub(r"[^A-Za-z0-9._-]", "-", origin or "local") or "local"
        self.dry_run = dry_run
        self.force = force
        self.runner = runner or run_aws
        self.out = out or (lambda msg: print(msg, flush=True))
        self.counts = {"get": 0, "put": 0, "conflict": 0, "skipped": 0}
        self.fetched = []           # pull で取得したファイル名 (フックで会話に渡す)
        self.conflicts = []         # 競合して飛ばしたファイル名
        self._remote_index = None
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
        result, token = {}, None
        self._remote_index = False
        self._remote_etags = {}
        while True:
            args = ["s3api", "list-objects-v2", "--bucket", self.bucket, "--prefix", self.prefix, "--output", "json"]
            if token:
                args += ["--continuation-token", token]
            text = self.aws(*args).strip()
            data = json.loads(text) if text else {}
            for obj in data.get("Contents") or []:
                name = obj["Key"][len(self.prefix):]
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
            token = data.get("NextContinuationToken")
            if not token:
                return result

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
        with tempfile.TemporaryDirectory() as tmp:
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
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / name
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

    def write_local(self, name, data):
        self.dir.mkdir(parents=True, exist_ok=True)
        dest = self.dir / name
        tmp = self.dir / (name + ".pmva-tmp")
        tmp.write_bytes(data)
        os.replace(str(tmp), str(dest))

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

    # ---- コマンド
    def summary(self):
        c = self.counts
        self.out(("[dry-run] " if self.dry_run else "") +
                 "取得 %d、送信 %d、競合 %d、飛ばした %d" % (c["get"], c["put"], c["conflict"], c["skipped"]))
        if c["conflict"]:
            self.out("競合したファイルは、--force で、pull なら S3 の内容、push なら手元の内容を採用できます")
        return 0

    def pull(self):
        local, remote, state = self.scan_local(), self.list_remote(), self.load_state()
        new_state = dict(state)
        local_names = set(local)
        for name in sorted(set(local) | set(remote) | set(state)):
            l = local[name][0] if name in local else None
            r = remote.get(name)
            base = state.get(name)
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
        self.merge_pull_index(local_names)
        self.save_state(new_state)
        return self.summary()

    def push(self):
        local, remote, state = self.scan_local(), self.list_remote(), self.load_state()
        new_state = dict(state)
        remote_names = set(remote)
        private_names = {n for n, (_, data) in local.items() if has_share_false(data)}
        for name in sorted(set(local) | set(remote) | set(state)):
            l = local[name][0] if name in local else None
            r = remote.get(name)
            base = state.get(name)
            kind = classify(l, r, base)
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
            kind = classify(l, remote.get(name), state.get(name))
            label = STATUS_LABELS[kind]
            if name in local and kind in ("local-only", "local-newer", "conflict"):
                reasons = self.local_reasons(local[name][1])
                if reasons:
                    label = "送らない: " + "、".join(reasons)
            self.out("%-40s %s" % (name, label))
        return 0


# ---------------------------------------------------------------- Claude Code のフック

def _same_dir(a, b):
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def _acquire_lock(path, wait, stale=120):
    """同じマシンで同時に動く同期 (複数の会話のフック) を 1 つにする。取れなければ False。

    同期の途中で落ちて残ったロックは、stale 秒より古ければ奪う。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + wait
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode("ascii"))
            os.close(fd)
            return True
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink()
                    continue
            except OSError:
                continue
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.1)


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
    lock = Path(str(sync.state_path) + ".lock")
    if not _acquire_lock(lock, lock_wait):
        sync.out("他の同期が動いているので、今回は飛ばします (%s)" % lock)
        return ""
    try:
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
    except Exception as e:      # フックの失敗で会話を止めない
        sync.out("error: %s" % e)
        _hook_log(sync, event, str(e))
        return ""
    finally:
        try:
            lock.unlink()
        except OSError:
            pass


def main(argv=None, runner=None, out=None, stdin=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    hook = argv[:1] == ["hook"]
    p = argparse.ArgumentParser(prog="memsync.py", description="Claude Code の自動メモリを S3 と同期する")
    p.add_argument("action", choices=("pull", "push", "status", "hook"))
    p.add_argument("event", nargs="?", choices=tuple(HOOK_EVENTS), help="hook のときのイベント")
    p.add_argument("--min-interval", type=float, default=0,
                   help="hook prompt で、前回の pull からこの秒数がたつまでは S3 を見に行かない (既定 0)")
    p.add_argument("--dir", required=True, help="メモリのフォルダ")
    p.add_argument("--uri", required=True, help="s3://<バケット>/memory/<repo-id>/")
    p.add_argument("--state", required=True, help="最後に同期したときの状態を置くファイル (メモリのフォルダの外)")
    p.add_argument("--origin", default="local", help="S3 のオブジェクトに付ける送り元の名前 (既定 local)")
    p.add_argument("--dry-run", action="store_true", help="何も書き換えず、することだけ表示")
    p.add_argument("--force", action="store_true", help="競合したときに、pull は S3、push は手元の内容を採用する")
    try:
        args = p.parse_args(argv)
    except SystemExit:
        if hook:
            return 0        # フックが 2 で終わると、Claude Code は利用者の指示を止めてしまう
        raise
    if hook:
        # 標準出力は会話に入るので、経過は標準エラーに出す
        out = out or (lambda msg: print(msg, file=sys.stderr, flush=True))
        try:
            sync = Memsync(Path(args.dir).expanduser(), args.uri, args.state, args.origin, runner=runner, out=out)
        except MemsyncError as e:
            print("error: %s" % e, file=sys.stderr)
            return 0
        if not args.event:
            print("error: hook のイベント (%s) を指定してください" % " / ".join(HOOK_EVENTS), file=sys.stderr)
            return 0
        text = stdin.read() if stdin else sys.stdin.buffer.read().decode("utf-8", errors="replace")
        result = run_hook(args.event, sync, text, min_interval=args.min_interval)
        if result:
            print(result)
        return 0
    try:
        sync = Memsync(Path(args.dir).expanduser(), args.uri, args.state, args.origin, args.dry_run, args.force, runner, out)
        return getattr(sync, args.action)()
    except MemsyncError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
