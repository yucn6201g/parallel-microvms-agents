#!/usr/bin/env python3
"""mvm - AWS Lambda MicroVMs 上で Claude Code / Codex を並列実行するためのローカル操作 CLI。

手元 PC で動くのはこのスクリプトと AWS CLI / ssh だけで、エージェント本体・ビルド・テストは
すべて MicroVM 側で実行される。Windows / macOS / Linux 共通で、Python 3.9 以上の標準ライブラリのみ使う
(WebSocket クライアントも自前実装なので websocat などの追加ツールは不要)。

起動方法: macOS / Linux は bin/mvm、Windows は bin\\mvm.cmd (PowerShell / コマンドプロンプト)。
"""

import argparse
import base64
import collections
import errno
import getpass
import hashlib
import io
import json
import os
import re
import select
import shlex
import shutil
import socket
import ssl
import subprocess
import tarfile
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = Path(__file__).resolve()
IS_WINDOWS = os.name == "nt"
CONFIG_FILE = Path(os.environ.get("PMVA_CONFIG") or ROOT / "config.env")
STATE_DIR = Path(os.environ.get("PMVA_STATE_DIR") or ROOT / ".state")

API_PORT = 8080
SSH_BRIDGE_PORT = 7681
SHELL_PORT = 8022
HOOKS = {
    "port": 9000,
    "microvmImageHooks": {"ready": "ENABLED", "readyTimeoutInSeconds": 120,
                          "validate": "ENABLED", "validateTimeoutInSeconds": 60},
    "microvmHooks": {"run": "ENABLED", "runTimeoutInSeconds": 10,
                     "resume": "ENABLED", "resumeTimeoutInSeconds": 5,
                     "suspend": "ENABLED", "suspendTimeoutInSeconds": 10,
                     "terminate": "ENABLED", "terminateTimeoutInSeconds": 60},
}
DEFAULTS = {
    "AWS_REGION": "ap-northeast-1", "AWS_PROFILE": "", "PMVA_PREFIX": "pmva", "STACK_NAME": "pmva",
    "IMAGE_NAME": "pmva-agent", "BASE_IMAGE_ARN": "", "MEMORY_MIB": "4096", "MAX_AGENTS": "0",
    "MAX_DURATION_SECONDS": "28800", "IDLE_MAX_SECONDS": "1800", "IDLE_SUSPENDED_SECONDS": "21600",
    "IDLE_AUTO_RESUME": "true", "INGRESS_CONNECTORS": "HTTP_INGRESS SHELL_INGRESS",
    "SNAPSHOT_INTERVAL_SECONDS": "1800", "DEFAULT_AGENT": "claude", "DEFAULT_CLAUDE_AUTH": "oauth-token",
    "DEFAULT_CODEX_AUTH": "api-key", "DEFAULT_CLAUDE_ARGS": "", "DEFAULT_CODEX_ARGS": "",
    "BEDROCK_REGION": "", "CLAUDE_MODEL": "", "USE_GITHUB_TOKEN": "true", "ENABLE_BEDROCK": "true",
    "GIT_USER_NAME": "", "GIT_USER_EMAIL": "", "SSH_PUBLIC_KEY_FILE": "~/.ssh/id_ed25519.pub",
    "BUDGET_MONTHLY_USD": "0", "BUDGET_EMAIL": "", "KEEPALIVE_RATE_MINUTES": "5", "KEEPALIVE_ACTIVE_SECONDS": "900",
    "NOTIFY_WEBHOOK": "false", "NIGHTLY_STOP_TIME": "", "NIGHTLY_STOP_TIMEZONE": "Asia/Tokyo",
    "INFRA_TEMPLATE": "full", "SETUP_TIMEOUT_SECONDS": "1800", "IMAGE_BROWSER_DEPS": "true", "IMAGE_EXTRA_PACKAGES": "",
    "PROFILE": "", "ANTHROPIC_AWS_WORKSPACE_ID": "", "ANTHROPIC_AWS_REGION": "", "ENABLE_ANTHROPIC_AWS": "false",
}
# mvm infra deploy で使う CloudFormation テンプレート。minimal は mvm の動作に必要なリソースだけ
# (キープアライブ、毎晩の自動停止、予算アラートを含まない)
INFRA_TEMPLATES = {"full": "cloudformation.yaml", "minimal": "cloudformation-minimal.yaml"}
AGENTS = ("claude", "codex", "both", "none")
CLAUDE_AUTHS = ("api-key", "oauth-token", "bedrock", "bedrock-api-key", "mantle", "anthropic-aws",
                "anthropic-aws-api-key", "gateway", "custom", "interactive", "none")
CODEX_AUTHS = ("api-key", "chatgpt", "access-token", "bedrock", "bedrock-api-key", "gateway", "custom",
               "interactive", "none")
# 実行ロールはエージェントごとに分かれており、読めるシークレットを名前の接頭辞で区別する
# (infra/cloudformation.yaml)。別名で登録するときも、この接頭辞で始める必要がある
SECRET_NAME_PREFIXES = {
    "anthropic-api-key": ("anthropic-", "claude-"), "claude-oauth-token": ("anthropic-", "claude-"),
    "anthropic-gateway": ("anthropic-", "claude-"), "openai-api-key": ("openai-", "codex-"),
    "codex-auth-json": ("openai-", "codex-"), "github-token": ("github-",), "notify-webhook": ("notify-",),
    "claude-bedrock-api-key": ("anthropic-", "claude-"), "anthropic-aws-api-key": ("anthropic-", "claude-"),
    "codex-bedrock-api-key": ("openai-", "codex-"), "codex-access-token": ("openai-", "codex-"),
    "codex-gateway": ("openai-", "codex-"), "claude-custom-auth": ("anthropic-", "claude-"),
    "codex-custom-auth": ("openai-", "codex-"),
}
SECRET_KINDS = ("anthropic-api-key", "claude-oauth-token", "anthropic-gateway", "claude-bedrock-api-key",
                "anthropic-aws-api-key", "openai-api-key", "codex-auth-json", "codex-access-token",
                "codex-bedrock-api-key", "codex-gateway", "claude-custom-auth", "codex-custom-auth",
                "github-token", "notify-webhook")

# 認証方式 → 使うシークレットの種類 (既定の名前は <PMVA_PREFIX>/<種類>)。ここに無い方式はシークレットを使わない
CLAUDE_AUTH_SECRETS = {
    "api-key": "anthropic-api-key", "oauth-token": "claude-oauth-token", "gateway": "anthropic-gateway",
    "bedrock-api-key": "claude-bedrock-api-key", "anthropic-aws-api-key": "anthropic-aws-api-key",
    "custom": "claude-custom-auth",
}
CODEX_AUTH_SECRETS = {
    "api-key": "openai-api-key", "chatgpt": "codex-auth-json", "bedrock-api-key": "codex-bedrock-api-key",
    "access-token": "codex-access-token", "gateway": "codex-gateway", "custom": "codex-custom-auth",
}
# 実行ロールの権限 (mvm infra deploy の設定) が要る認証方式
CLAUDE_AUTHS_NEED_BEDROCK = ("bedrock", "mantle")
CODEX_AUTHS_NEED_BEDROCK = ("bedrock",)

# mvm をエージェントのサンドボックスの中で動かすときの案内 (mvm orchestrator install codex も同じ設定を表示する)
SANDBOX_HINT = (" (Codex なら ~/.codex/config.toml の [sandbox_workspace_write] に network_access = true と writable_roots。"
                "mvm orchestrator install codex が設定を表示します。docs/orchestrator.md)")

C = {}  # 読み込んだ設定


def default_secret(table, auth):
    kind = table.get(auth)
    return f"{C['PMVA_PREFIX']}/{kind}" if kind else ""


def die(msg):
    raise SystemExit(f"error: {msg}")


def log(msg):
    print(f"==> {msg}", file=sys.stderr, flush=True)


def debug(msg):
    """PMVA_DEBUG にファイル名を指定すると、接続の詳細 (切断理由など) を追記する。"""
    path = os.environ.get("PMVA_DEBUG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} [{os.getpid()}] {msg}\n")


# ---------------------------------------------------------------- 設定

def parse_env_file(path):
    """KEY=VALUE 形式 (行末の ' # コメント'、クォートに対応) を読む。シェルとして実行はしない。"""
    result = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, rest = line.split("=", 1)
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        rest = rest.strip()
        if rest[:1] in ("'", '"'):
            end = rest.find(rest[0], 1)
            value = rest[1:end] if end > 0 else rest[1:]
        else:
            value = rest
            for i, ch in enumerate(rest):
                if ch == "#" and (i == 0 or rest[i - 1] in " \t"):
                    value = rest[:i]
                    break
            value = value.strip()
        result[key] = value
    return result


def load_config():
    if not CONFIG_FILE.exists():
        die(f"{CONFIG_FILE} がありません。config.example.env をコピーして config.env を作成してください")
    C.update(DEFAULTS)
    C.update(parse_env_file(CONFIG_FILE))
    C["BASE_IMAGE_ARN"] = C["BASE_IMAGE_ARN"] or f"arn:aws:lambda:{C['AWS_REGION']}:aws:microvm-image:al2023-1"
    C["BEDROCK_REGION"] = C["BEDROCK_REGION"] or C["AWS_REGION"]
    C["ANTHROPIC_AWS_REGION"] = C["ANTHROPIC_AWS_REGION"] or C["AWS_REGION"]
    try:
        (STATE_DIR / "sessions").mkdir(parents=True, exist_ok=True)
        (STATE_DIR / "tokens").mkdir(parents=True, exist_ok=True)
        for p in (STATE_DIR, STATE_DIR / "tokens"):
            os.chmod(p, 0o700)
    except OSError as e:
        die(f"状態の保存先 {STATE_DIR} に書き込めません ({e.strerror})。"
            f"エージェント (Codex など) のサンドボックスの中で動かしているなら、writable_roots に {STATE_DIR} を足してください"
            f"{SANDBOX_HINT}")


def as_bool(v):
    return str(v).lower() in ("1", "true", "yes", "on")


def expand_path(p):
    return Path(os.path.expandvars(os.path.expanduser(p))) if p else None


# ---------------------------------------------------------------- AWS CLI

def aws_bin():
    exe = shutil.which("aws")
    if not exe:
        die("AWS CLI v2 が見つかりません https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html")
    return exe


def aws_env():
    env = dict(os.environ)
    env["AWS_REGION"] = env["AWS_DEFAULT_REGION"] = C["AWS_REGION"]
    env["AWS_PAGER"] = ""
    env["AWS_CLI_OUTPUT_ENCODING"] = "UTF-8"  # Windows でも出力を UTF-8 で受け取る
    if C.get("AWS_PROFILE"):
        env["AWS_PROFILE"] = C["AWS_PROFILE"]
    return env


THROTTLE_MARKERS = ("ThrottlingException", "TooManyRequestsException", "Rate exceeded")
_last_aws_error = [""]  # check=False で失敗したときの stderr (「存在しない」と「一時的な失敗」を区別するため)


def last_aws_error():
    return _last_aws_error[0]


def aws(*args, check=True, output="json", retries=5):
    """AWS CLI を実行して結果を返す。失敗時は check=True なら終了、False なら None。

    API のレート制限 (RunMicrovm は既定で 1 回/秒) に当たったら待ってやり直す。
    RunMicrovm は clientToken 付きで呼ぶので、やり直しても二重に起動しない。
    """
    cmd = [aws_bin(), *args]
    if output:
        cmd += ["--output", output]
    for attempt in range(retries + 1):
        p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", env=aws_env())
        if p.returncode == 0 or attempt == retries or not any(m in p.stderr for m in THROTTLE_MARKERS):
            break
        time.sleep(min(2 ** attempt, 16))
    _last_aws_error[0] = p.stderr if p.returncode != 0 else ""
    if p.returncode != 0:
        if check:
            if "ServiceQuotaExceededException" in p.stderr:
                die(f"AWS のクォータを超えました: {p.stderr.strip()}\n"
                    "  MicroVM のメモリ上限は mvm doctor で確認できます。並列数を増やすには Service Quotas で引き上げを申請してください")
            die(f"aws {' '.join(args[:2])} に失敗しました: {p.stderr.strip()}")
        return None
    out = p.stdout.strip()
    if output == "json":
        return json.loads(out) if out else {}
    return out


def aws_interactive(*args):
    """出力をそのまま端末に流す (logs tail / cloudformation deploy など)。"""
    return subprocess.call([aws_bin(), *args], env=aws_env())


def lmv(*args, **kw):
    return aws("lambda-microvms", *args, **kw)


def stack_output(key):
    cache = STATE_DIR / "stack-outputs.json"
    if not cache.exists():
        r = aws("cloudformation", "describe-stacks", "--stack-name", C["STACK_NAME"], check=False)
        if not r:
            die(f"スタック {C['STACK_NAME']} が見つかりません。先に mvm infra deploy を実行してください")
        cache.write_text(json.dumps(r["Stacks"][0].get("Outputs", [])), encoding="utf-8")
    for o in json.loads(cache.read_text(encoding="utf-8")):
        if o["OutputKey"] == key:
            return o["OutputValue"]
    die(f"スタック出力 {key} がありません")


MEMORY_QUOTA_CODE = "L-CD1C0CC4"  # Max allocated ARM_64 MicroVM memory (GB)
_memory_quota = []


def memory_quota_mib():
    """アカウントの MicroVM メモリ上限 (MiB)。権限がないなどで取得できなければ None。"""
    if not _memory_quota:
        r = aws("service-quotas", "get-service-quota", "--service-code", "lambda",
                "--quota-code", MEMORY_QUOTA_CODE, check=False, retries=0) or {}
        value = r.get("Quota", {}).get("Value")
        _memory_quota.append(int(float(value) * 1024) if value is not None else None)
    return _memory_quota[0]


def check_memory_quota(running, adding):
    """running 台が起動中のところへ adding 台を足しても上限内かを確かめる。"""
    quota = memory_quota_mib()
    need = (running + adding) * int(C["MEMORY_MIB"])
    if quota is not None and need > quota:
        fit = max(quota // int(C["MEMORY_MIB"]) - running, 0)
        die(f"MicroVM のメモリ上限を超えます (上限 {quota // 1024}GB、必要 {need // 1024}GB = "
            f"{running + adding} 台 x {int(C['MEMORY_MIB']) // 1024}GB)。今は あと {fit} 台まで起動できます。\n"
            f"  起動中のセッションを止めるか (mvm ls → mvm stop)、もっと同時に動かしたい場合は Service Quotas で\n"
            f"  「Max allocated ARM_64 MicroVM memory」({MEMORY_QUOTA_CODE}) の引き上げを申請してください")


def find_image_arn():
    r = lmv("list-microvm-images", "--name-filter", C["IMAGE_NAME"], check=False) or {}
    for item in r.get("items", []):
        if item.get("name") == C["IMAGE_NAME"]:
            return item["imageArn"]
    return ""


# ---------------------------------------------------------------- ローカルのセッション状態

def session_path(name):
    return STATE_DIR / "sessions" / f"{name}.json"


def load_session(name):
    p = session_path(name)
    if not p.exists():
        die(f"セッション {name} がありません (mvm ls で確認)")
    return json.loads(p.read_text(encoding="utf-8"))


def save_session(s):
    session_path(s["name"]).write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")


def session_names():
    return sorted(p.stem for p in (STATE_DIR / "sessions").glob("*.json"))


def vm_state(name):
    """MicroVM の状態。存在しなければ GONE。終了していればローカル状態にも反映する。

    権限エラーや通信エラーで状態を取れなかったときは UNKNOWN を返し、終了扱いにはしない
    (終了扱いにすると一覧から消え、infra delete も起動中の VM を見落とすため)。
    """
    s = load_session(name)
    r = lmv("get-microvm", "--microvm-identifier", s["microvmId"], check=False)
    if r:
        state = r.get("state", "GONE")
    elif "ResourceNotFound" in last_aws_error():
        state = "GONE"
    else:
        debug(f"get-microvm {name} failed: {last_aws_error().strip()}")
        return "UNKNOWN"
    if state in ("TERMINATED", "GONE") and not s.get("terminated"):
        s["terminated"] = True
        save_session(s)
    return state


def is_active(name):
    if not session_path(name).exists() or load_session(name).get("terminated"):
        return False
    return vm_state(name) not in ("TERMINATED", "TERMINATING", "GONE")


def active_names():
    return [n for n in session_names() if is_active(n)]


def clear_tokens(name):
    for p in (STATE_DIR / "tokens").glob(f"{name}.*"):
        p.unlink()


def auth_token(name, port):
    """アプリ用トークン (最大 60 分) をキャッシュして使い回す。"""
    f = STATE_DIR / "tokens" / f"{name}.{port}.json"
    now = int(time.time())
    if f.exists():
        try:
            cached = json.loads(f.read_text(encoding="utf-8"))
            if cached["exp"] > now + 120:
                return cached["token"]
        except (ValueError, KeyError):
            pass
    r = lmv("create-microvm-auth-token", "--microvm-identifier", load_session(name)["microvmId"],
            "--expiration-in-minutes", "60", "--allowed-ports", json.dumps([{"port": port}]))
    token = r["authToken"]["X-aws-proxy-auth"]
    f.write_text(json.dumps({"exp": now + 3600, "token": token}), encoding="utf-8")
    os.chmod(f, 0o600)
    return token


# ---------------------------------------------------------------- MicroVM 内 API (:8080)

def vm_api(name, method, path, body=None, timeout=30):
    s = load_session(name)
    # PMVA_API_BASE_URL はローカルでのテスト・デバッグ用 (例: http://127.0.0.1:8080)
    base = os.environ.get("PMVA_API_BASE_URL") or f"https://{s['endpoint']}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method, headers={
        "X-aws-proxy-auth": auth_token(name, API_PORT),
        "X-aws-proxy-port": str(API_PORT),
        "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        text = e.read().decode("utf-8", "replace")
        try:
            return json.loads(text)
        except ValueError:
            die(f"{name}: HTTP {e.code} {text[:200]}")
    except (urllib.error.URLError, OSError) as e:
        # サンドボックスがネットワークを止めていると、接続そのものが許されない (EPERM / EACCES)
        reason = getattr(e, "reason", e)
        blocked = isinstance(reason, OSError) and reason.errno in (errno.EPERM, errno.EACCES)
        hint = (f"。エージェント (Codex など) のサンドボックスの中で動かしているなら、ネットワークを許してください{SANDBOX_HINT}"
                if blocked else "")
        die(f"{name}: 接続できません ({e}){hint}")


# ---------------------------------------------------------------- WebSocket クライアント

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def open_tcp(host, port, timeout):
    """HTTPS_PROXY が設定されていれば CONNECT でトンネルを張る (社内プロキシ対応)。"""
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    no_proxy = [x.strip() for x in (os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or "").split(",") if x.strip()]
    if not proxy or any(host == n.lstrip(".") or host.endswith("." + n.lstrip(".")) for n in no_proxy):
        return socket.create_connection((host, port), timeout=timeout)
    u = urllib.parse.urlparse(proxy if "://" in proxy else "http://" + proxy)
    sock = socket.create_connection((u.hostname, u.port or 8080), timeout=timeout)
    req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n"
    if u.username:
        cred = f"{urllib.parse.unquote(u.username)}:{urllib.parse.unquote(u.password or '')}"
        req += "Proxy-Authorization: Basic " + base64.b64encode(cred.encode()).decode() + "\r\n"
    sock.sendall((req + "\r\n").encode())
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = sock.recv(4096)
        if not chunk:
            raise OSError("proxy closed connection")
        resp += chunk
    if b" 200" not in resp.split(b"\r\n", 1)[0]:
        raise OSError("proxy CONNECT failed: " + resp.split(b"\r\n", 1)[0].decode("latin-1"))
    return sock


def ws_target(endpoint):
    """WebSocket の接続先 (host, port, tls)。PMVA_WS_BASE_URL はテスト・デバッグ用 (例: ws://127.0.0.1:9001)。"""
    override = os.environ.get("PMVA_WS_BASE_URL")
    if override:
        u = urllib.parse.urlparse(override)
        tls = u.scheme == "wss"
        return u.hostname, u.port or (443 if tls else 80), tls
    return endpoint, 443, True


class WebSocket:
    """RFC 6455 の最小限のクライアント (バイナリフレームの送受信、ping/pong、close)。"""

    def __init__(self, host, path, protocols, timeout=30, port=443, tls=True):
        raw = open_tcp(host, port, timeout)
        self.sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host) if tls else raw
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {path} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Sec-WebSocket-Protocol: {', '.join(protocols)}\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise OSError("connection closed during handshake")
            resp += chunk
        head, self.buf = resp.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise OSError(f"WebSocket handshake failed: {lines[0]}")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()
        headers = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        if headers.get("sec-websocket-accept") != accept:
            raise OSError("WebSocket handshake failed: invalid Sec-WebSocket-Accept")
        # SSL ソケットは複数スレッドからの同時の読み書きに対して安全ではない (受信スレッドと
        # 送信・ping が同時に触ると接続が壊れることがあった)。そこでソケットをノンブロッキングにし、
        # 読み書きは self.lock の中で 1 回ずつ行い、待つときはロックを持たずに select で待つ。
        # ロックを持ったまま待つと、相手の受信待ち (送信バッファが一杯) と、こちらの受信待ち
        # (ロックが取れない) が互いを待って止まる (Windows の CI で発生)。
        # 送信は専用スレッドが行い、send() はキューに積むだけにする
        self.sock.setblocking(False)
        self.lock = threading.Lock()
        self.closed = False
        self.cond = threading.Condition()
        self.control, self.data = collections.deque(), collections.deque()  # 送信待ちのフレーム
        self.write_error = None
        self.stopping = False
        self.writer = threading.Thread(target=self._write_loop, daemon=True)
        self.writer.start()

    def _recv_chunk(self):
        """届いたデータを返す。相手が閉じたら b""。"""
        while True:
            with self.lock:
                try:
                    return self.sock.recv(65536)  # SSL の内部バッファに残っている分もここで読める
                except (BlockingIOError, InterruptedError, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    pass
            try:
                select.select([self.sock], [], [], 0.05)
            except (OSError, ValueError):  # 閉じたソケット
                return b""

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self._recv_chunk()
            if not chunk:
                raise EOFError
            self.buf += chunk
        data, self.buf = self.buf[:n], self.buf[n:]
        return data

    MAX_PENDING_FRAMES = 64  # これ以上たまったら send() を待たせる (標準入力を読みすぎない)

    def send(self, data, opcode=0x2):
        """フレームを送信キューに積む。制御フレーム (ping/pong/close) は待たずに積み、先に送る。"""
        n = len(data)
        header = bytes([0x80 | opcode])
        if n < 126:
            header += bytes([0x80 | n])
        elif n < 65536:
            header += bytes([0x80 | 126]) + n.to_bytes(2, "big")
        else:
            header += bytes([0x80 | 127]) + n.to_bytes(8, "big")
        mask = os.urandom(4)
        if n:
            m = (mask * (n // 4 + 1))[:n]
            data = (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")
        frame = header + mask + data
        with self.cond:
            if self.write_error is not None:
                raise OSError(f"websocket send failed: {self.write_error!r}")
            if opcode & 0x8:
                # 受信スレッドからの pong もここに来るので、待たせない (待たせると受信が止まる)
                self.control.append(frame)
            else:
                while len(self.data) >= self.MAX_PENDING_FRAMES and self.write_error is None:
                    self.cond.wait()
                if self.write_error is not None:
                    raise OSError(f"websocket send failed: {self.write_error!r}")
                self.data.append(frame)
            self.cond.notify_all()

    def _write_loop(self):
        while True:
            with self.cond:
                while not self.control and not self.data and not self.stopping:
                    self.cond.wait()
                if not self.control and not self.data:
                    return
                frame = self.control.popleft() if self.control else self.data.popleft()
                self.cond.notify_all()
            try:
                self._write(frame)
            except OSError as e:
                debug(f"websocket write failed: {e!r}")
                with self.cond:
                    self.write_error = e
                    self.control.clear()
                    self.data.clear()
                    self.cond.notify_all()
                return

    def _write(self, frame):
        """1 フレームを小分けに書く。書けないときはロックを手放し、受信スレッドが読めるようにする。"""
        view = memoryview(frame)
        while view:
            # SSL では、書けずに終わった書き込みは同じ内容でやり直す必要があるので、長さを固定して切り出す
            chunk = view[:16384]
            with self.lock:
                try:
                    written = self.sock.send(chunk)
                except (BlockingIOError, InterruptedError, ssl.SSLWantWriteError, ssl.SSLWantReadError):
                    written = 0
            view = view[written:]
            if not written:
                select.select([], [self.sock], [], 0.05)

    def recv(self):
        """データを返す。相手が閉じたら None。"""
        message = b""
        while True:
            try:
                b1, b2 = self._read(2)
                n = b2 & 0x7F
                if n == 126:
                    n = int.from_bytes(self._read(2), "big")
                elif n == 127:
                    n = int.from_bytes(self._read(8), "big")
                mask = self._read(4) if b2 & 0x80 else None
                payload = self._read(n) if n else b""
            except (EOFError, OSError) as e:
                debug(f"websocket closed while reading: {e!r}")
                return None
            if mask and payload:
                m = (mask * (n // 4 + 1))[:n]
                payload = (int.from_bytes(payload, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")
            opcode = b1 & 0x0F
            if opcode == 0x8:
                code = int.from_bytes(payload[:2], "big") if len(payload) >= 2 else None
                debug(f"websocket close frame from server: code={code} reason={payload[2:]!r}")
                self.close()
                return None
            if opcode == 0x9:
                self.send(payload, 0xA)
                continue
            if opcode == 0xA:
                continue
            message += payload
            if b1 & 0x80:
                return message

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.send(b"", 0x8)
        except OSError:
            pass
        with self.cond:
            self.stopping = True
            self.cond.notify_all()
        # 送信キューに残っているデータと close フレームを送り切ってから閉じる (受信スレッドから
        # 呼ばれたときは送信スレッドの終了を待たない)
        if threading.current_thread() is not self.writer:
            self.writer.join(5)
        try:
            self.sock.close()
        except OSError:
            pass


def ws_protocols(token, port):
    return [f"lambda-microvms.authentication.{token}", "lambda-microvms", f"lambda-microvms.port.{port}"]


def pump(ws, read_input, skip=None, on_close=None):
    """標準入力 → WebSocket、WebSocket → 標準出力 を中継する。どちらかが閉じたら終了。

    skip: 表示しないメッセージを判定する関数 (シェル接続の制御メッセージ用)
    on_close: 相手が切断したときに、プロセス終了前に呼ぶ関数 (端末設定の復元用)
    """
    def to_stdout():
        while True:
            data = ws.recv()
            if data is None:
                break
            if skip and skip(data):
                continue
            os.write(1, data)
        if on_close:
            on_close()
        os._exit(0)

    def keepalive():
        while not ws.closed:
            time.sleep(30)
            try:
                ws.send(b"", 0x9)
            except OSError as e:
                debug(f"ping failed: {e!r}")
                return

    threading.Thread(target=to_stdout, daemon=True).start()
    threading.Thread(target=keepalive, daemon=True).start()
    while True:
        data = read_input()
        if not data:
            debug("stdin closed")
            break
        try:
            ws.send(data)
        except OSError as e:
            debug(f"send failed: {e!r}")
            break
    ws.close()


# ---------------------------------------------------------------- 端末の raw モード (mvm shell)

class RawTerminal:
    def __enter__(self):
        self.restore = None
        if not sys.stdin.isatty():
            return self
        if IS_WINDOWS:
            import ctypes
            k32 = ctypes.windll.kernel32
            hin, hout = k32.GetStdHandle(-10), k32.GetStdHandle(-11)
            in_mode, out_mode = ctypes.c_uint32(), ctypes.c_uint32()
            k32.GetConsoleMode(hin, ctypes.byref(in_mode))
            k32.GetConsoleMode(hout, ctypes.byref(out_mode))
            old_cp, old_out_cp = k32.GetConsoleCP(), k32.GetConsoleOutputCP()
            k32.SetConsoleCP(65001)
            k32.SetConsoleOutputCP(65001)
            # 行入力・エコー・Ctrl-C 処理を無効にし、VT シーケンスで入出力する
            k32.SetConsoleMode(hin, (in_mode.value & ~0x0007) | 0x0200)
            k32.SetConsoleMode(hout, out_mode.value | 0x0004)

            def restore():
                k32.SetConsoleMode(hin, in_mode.value)
                k32.SetConsoleMode(hout, out_mode.value)
                k32.SetConsoleCP(old_cp)
                k32.SetConsoleOutputCP(old_out_cp)
            self.restore = restore
        else:
            import termios
            import tty
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            tty.setraw(fd)
            self.restore = lambda: termios.tcsetattr(fd, termios.TCSADRAIN, old)
        return self

    def __exit__(self, *exc):
        if self.restore:
            self.restore()


def set_binary_stdio():
    if IS_WINDOWS:
        import msvcrt
        msvcrt.setmode(0, os.O_BINARY)
        msvcrt.setmode(1, os.O_BINARY)


# ---------------------------------------------------------------- doctor / infra / secret

def cmd_doctor(_args):
    ok = True
    print(f"  ok   Python {sys.version.split()[0]} ({sys.platform})")
    for tool, note in (("aws", "AWS CLI v2"), ("ssh", "OpenSSH クライアント (mvm ssh 用)")):
        found = shutil.which(tool)
        print(f"  {'ok' if found else 'NG'}   {note}: {found or '見つかりません'}")
        ok = ok and bool(found or tool == "ssh")
    if not shutil.which("aws"):
        return 1
    print("  aws: " + (aws("--version", output=None, check=False) or "?"))
    ident = aws("sts", "get-caller-identity", check=False)
    if ident:
        print(f"  ok   AWS 認証: {ident.get('Arn')}")
        for line in credential_findings(ident.get("Arn", "")):
            print(line)
    else:
        print("  NG   AWS 認証情報がありません (aws configure / aws sso login)")
        ok = False
    p = subprocess.run([aws_bin(), "lambda-microvms", "list-managed-microvm-images", "--output", "json"],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", env=aws_env())
    if p.returncode == 0:
        print(f"  ok   {C['AWS_REGION']} で Lambda MicroVMs が利用可能:")
        for item in json.loads(p.stdout or "{}").get("items", []):
            print(f"         {item['imageArn']}")
    elif "Invalid choice" in p.stderr or "invalid choice" in p.stderr:
        print("  NG   AWS CLI が lambda-microvms に未対応です。AWS CLI v2 を最新版に更新してください")
        ok = False
    else:
        print(f"  NG   {C['AWS_REGION']} で list-managed-microvm-images に失敗: {p.stderr.strip()[:200]}")
        ok = False
    if ident:
        for line in infra_findings() + image_findings() + auth_findings():
            print(line)
            ok = ok and not line.startswith("  NG")
    quota = memory_quota_mib()
    if quota is None:
        print("  --   MicroVM のメモリ上限を取得できません (servicequotas:GetServiceQuota の権限が必要)")
    else:
        # 同時に動かせる台数はクォータで決まる。MAX_AGENTS (0 なら制限なし) は、さらに絞りたいときだけ使う
        fit = quota // int(C["MEMORY_MIB"])
        max_agents = int(C["MAX_AGENTS"])
        mark = "ok" if fit >= max(max_agents, 1) else "!!"
        limit = f"、MAX_AGENTS={max_agents}" if max_agents else ""
        print(f"  {mark}   MicroVM のメモリ上限 {quota // 1024}GB → {int(C['MEMORY_MIB']) // 1024}GB の VM を同時に {fit} 台まで"
              f"{limit}")
        if fit < max(max_agents, 1):
            want = max(max_agents, 1)
            print(f"         {want} 台を動かすには、MEMORY_MIB を小さくするか、Service Quotas で {MEMORY_QUOTA_CODE} を"
                  f" {want * int(C['MEMORY_MIB']) // 1024}GB 以上に引き上げてください")
    key = expand_path(C["SSH_PUBLIC_KEY_FILE"])
    if key and key.exists():
        print(f"  ok   SSH 公開鍵: {key}")
    else:
        print("  --   SSH 公開鍵なし (mvm ssh は使えません。mvm shell かコンソールの Connect を使用)")
    print("準備 OK" if ok else "上記の NG を解消してください")
    return 0 if ok else 1


def infra_findings():
    """AWS の構成 (CloudFormation のスタック) が、手元のテンプレートと config.env のとおりかを doctor 用の行で返す。

    テンプレートを更新した後や設定を変えた後に、mvm infra deploy のやり直しを忘れていないかを確かめる。
    """
    stack = C["STACK_NAME"]
    r = aws("cloudformation", "describe-stacks", "--stack-name", stack, check=False)
    if not r:
        return [f"  NG   AWS の構成 (スタック {stack}) がありません。mvm infra deploy を実行してください (管理者の権限が必要)"]
    info = r["Stacks"][0]
    lines = []
    status = info.get("StackStatus", "")
    if status and (not status.endswith("_COMPLETE") or "ROLLBACK" in status):
        lines.append(f"  !!   スタック {stack} の状態が {status} です。CloudFormation のコンソールで原因を確認し、"
                     "mvm infra deploy をやり直してください")
    template = infra_template()
    body = aws("cloudformation", "get-template", "--stack-name", stack, "--template-stage", "Original", check=False)
    if not body:
        return lines + ["  --   AWS の構成を手元と比べられません (操作用ポリシーに cloudformation:GetTemplate がありません。"
                        "mvm infra deploy をやり直すと付きます)"]

    def normalize(text):
        # CloudFormation は ASCII 以外の文字 (日本語のコメントや Description) を 1 文字ずつ ? にして保存するので、
        # 手元の側も同じようにそろえてから比べる (そろえないと deploy 直後でも「違う」と出る)
        text = "".join(c if ord(c) < 128 else "?" for c in str(text))
        return "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").strip().splitlines())
    if normalize(body.get("TemplateBody", "")) != normalize(template.read_text(encoding="utf-8")):
        lines.append(f"  !!   AWS の構成が、手元のテンプレート ({template.name}) と違います "
                     "(mvm を更新した、または INFRA_TEMPLATE を変えた)。mvm infra deploy を実行してください")
    deployed = {p["ParameterKey"]: p.get("ParameterValue", "") for p in info.get("Parameters", [])}
    declared = template_parameters(template)
    changed = []
    for item in infra_parameters():
        key, value = item.split("=", 1)
        if key in declared and key in deployed and deployed[key] != value:
            changed.append(f"{key} ({deployed[key] or '空'} → {value or '空'})")
    if changed:
        lines.append(f"  !!   AWS の構成に、設定の変更が反映されていません: {', '.join(changed)}。mvm infra deploy を実行してください")
    return lines or [f"  ok   AWS の構成 (スタック {stack}) は手元のテンプレートと設定のとおりです"]


IMAGE_BUILD_RECORD = "image-build.json"   # .state/ に置く、最後に mvm image build したときのイメージの元の指紋


def image_digest():
    """イメージの元 (image/ の中身、Dockerfile に書き込む config.env の値、メモリ、ベースイメージ) の指紋。"""
    h = hashlib.sha256()
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = Path(tmp) / "image.zip"
        build_artifact_zip(zip_path)
        with zipfile.ZipFile(zip_path) as z:
            for name in sorted(z.namelist()):
                h.update(name.encode("utf-8") + b"\0" + z.read(name) + b"\0")
    h.update(f"{C['MEMORY_MIB']}\0{C['BASE_IMAGE_ARN']}".encode("utf-8"))
    return h.hexdigest()


def image_findings():
    record = STATE_DIR / IMAGE_BUILD_RECORD
    try:
        built = json.loads(record.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ["  --   この PC からイメージをビルドした記録がありません (別の PC でビルドした場合は確認できません。"
                "まだなら mvm image build)"]
    if built.get("image") != C["IMAGE_NAME"] or built.get("digest") != image_digest():
        return ["  !!   イメージの元 (image/ と IMAGE_* などの設定) が、最後にビルドしたときから変わっています。"
                "mvm image build を実行してください (動いている VM には、mvm renew で反映)"]
    return [f"  ok   イメージは、最後にビルドしたときから変わっていません (バージョン {built.get('version', '?')})"]


def auth_findings():
    """既定の認証方式 (config.env) に要るシークレットと設定がそろっているかを doctor 用の行で返す。"""
    agent = C["DEFAULT_AGENT"]
    checks = []   # (設定の説明, 認証方式の表, 認証方式, 実行ロールに Bedrock が要る方式)
    if agent in ("claude", "both"):
        checks.append(("DEFAULT_CLAUDE_AUTH", CLAUDE_AUTH_SECRETS, C["DEFAULT_CLAUDE_AUTH"], CLAUDE_AUTHS_NEED_BEDROCK))
    if agent in ("codex", "both"):
        checks.append(("DEFAULT_CODEX_AUTH", CODEX_AUTH_SECRETS, C["DEFAULT_CODEX_AUTH"], CODEX_AUTHS_NEED_BEDROCK))
    lines, secrets = [], []
    for key, table, auth, need_bedrock in checks:
        setting = f"{key}={auth}"
        if auth in ("interactive", "none"):
            lines.append(f"  --   {setting} は起動後にログインします (mvm login)" if auth == "interactive"
                         else f"  --   {setting} (認証しない)")
        if table.get(auth):
            secrets.append((default_secret(table, auth), table[auth], setting, f"既定の認証方式 ({setting})"))
        if auth in need_bedrock and not as_bool(C["ENABLE_BEDROCK"]):
            lines.append(f"  !!   {setting} には ENABLE_BEDROCK=true (実行ロールの Bedrock の権限) が必要です。"
                         "config.env を直して mvm infra deploy を実行してください")
        if auth.startswith("anthropic-aws") and not C["ANTHROPIC_AWS_WORKSPACE_ID"]:
            lines.append(f"  !!   {setting} には ANTHROPIC_AWS_WORKSPACE_ID が必要です (docs/auth.md)")
        if auth == "anthropic-aws" and not as_bool(C["ENABLE_ANTHROPIC_AWS"]):
            lines.append(f"  !!   {setting} には ENABLE_ANTHROPIC_AWS=true (実行ロールの権限) が必要です。"
                         "config.env を直して mvm infra deploy を実行してください")
    if as_bool(C["USE_GITHUB_TOKEN"]):
        secrets.append((f"{C['PMVA_PREFIX']}/github-token", "github-token", "USE_GITHUB_TOKEN=true", "USE_GITHUB_TOKEN=true"))
    if as_bool(C["NOTIFY_WEBHOOK"]):
        secrets.append((f"{C['PMVA_PREFIX']}/notify-webhook", "notify-webhook", "NOTIFY_WEBHOOK=true", "NOTIFY_WEBHOOK=true"))
    for name, kind, setting, why in secrets:
        if aws("secretsmanager", "describe-secret", "--secret-id", name, check=False):
            lines.append(f"  ok   シークレット {name} ({setting})")
        else:
            lines.append(f"  !!   {why} のシークレット {name} がありません。mvm secret set {kind} で登録してください")
    return lines


ACCESS_KEY_MAX_AGE_DAYS = 90


def is_admin_policy(policies):
    return any(x.get("PolicyName") == "AdministratorAccess" or x.get("PolicyArn", "").endswith(":policy/AdministratorAccess")
               for x in policies)


def credential_findings(arn):
    """使っている認証情報の注意点 (ルートユーザー、管理者権限、古いアクセスキー) を doctor 用の行で返す。

    管理者権限の長期アクセスキーは、漏れたときの影響が大きいので、普段の操作には使わないよう促す。
    """
    import datetime
    see = "pmva-operator ポリシーだけを付けた IAM ユーザーで使ってください (docs/setup.md「操作用の IAM ユーザー」)"
    if arn.endswith(":root"):
        return [f"  !!   ルートユーザーの認証情報です。{see}"]
    # arn:aws:iam::<account>:user/<path>/<name>、arn:aws:sts::<account>:assumed-role/<role>/<session>
    kind, _, rest = (arn.split(":", 5)[5] if arn.count(":") >= 5 else "").partition("/")
    lines = []
    if kind == "user":
        name = rest.rsplit("/", 1)[-1]
        attached = aws("iam", "list-attached-user-policies", "--user-name", name, check=False)
        groups = aws("iam", "list-groups-for-user", "--user-name", name, check=False)
        if attached is None or groups is None:
            return ["  --   権限の確認は省略しました (iam:List* の権限なし。権限を絞ったユーザーなら正常です)"]
        policies = attached.get("AttachedPolicies", [])
        for g in groups.get("Groups", []):
            r = aws("iam", "list-attached-group-policies", "--group-name", g["GroupName"], check=False) or {}
            policies += r.get("AttachedPolicies", [])
        if is_admin_policy(policies):
            lines.append("  !!   管理者権限 (AdministratorAccess) の長期アクセスキーです。漏れたときの影響が大きいので、"
                         f"infra deploy 以外は {see}")
        keys = aws("iam", "list-access-keys", "--user-name", name, check=False) or {}
        now = datetime.datetime.now(datetime.timezone.utc)
        for k in keys.get("AccessKeyMetadata", []):
            created = datetime.datetime.fromisoformat(str(k.get("CreateDate", "")).replace("Z", "+00:00")) \
                if isinstance(k.get("CreateDate"), str) else None
            if k.get("Status") == "Active" and created and (now - created).days > ACCESS_KEY_MAX_AGE_DAYS:
                lines.append(f"  !!   アクセスキー {k['AccessKeyId']} は作成から {(now - created).days} 日経っています。"
                             "作り直して古いキーを削除してください")
    elif kind == "assumed-role":
        role = rest.split("/", 1)[0]
        attached = aws("iam", "list-attached-role-policies", "--role-name", role, check=False)
        if attached is None:
            return ["  --   権限の確認は省略しました (iam:List* の権限なし。権限を絞ったロールなら正常です)"]
        if is_admin_policy(attached.get("AttachedPolicies", [])):
            lines.append(f"  !!   管理者権限 (AdministratorAccess) のロールです。infra deploy 以外は {see}")
    return lines or ["  ok   認証情報: 管理者権限ではありません"]


def cmd_infra(args):
    if args.action == "deploy":
        template = infra_template()
        declared = template_parameters(template)
        params = [x for x in infra_parameters() if x.split("=", 1)[0] in declared]
        if C["INFRA_TEMPLATE"] == "minimal":
            log("最小構成のテンプレートで作成します (キープアライブ・毎晩の自動停止・予算アラートは作りません)")
            ignored = [k for k in ("BUDGET_EMAIL", "NIGHTLY_STOP_TIME") if C[k]]
            if ignored:
                log(f"{' / '.join(ignored)} は最小構成では使いません。使うには INFRA_TEMPLATE=full にしてください")
        prepare_new_stack()
        rc = aws_interactive(
            "cloudformation", "deploy",
            "--template-file", str(template),
            "--stack-name", C["STACK_NAME"],
            "--parameter-overrides", *params,
            "--capabilities", "CAPABILITY_NAMED_IAM", "--no-fail-on-empty-changeset")
        if rc != 0:
            return rc
        (STATE_DIR / "stack-outputs.json").unlink(missing_ok=True)
        cmd_infra(argparse.Namespace(action="outputs"))
        log("ローカルで mvm を使う IAM ユーザー/ロールに OperatorPolicyArn のポリシーをアタッチしてください")
    elif args.action == "outputs":
        (STATE_DIR / "stack-outputs.json").unlink(missing_ok=True)
        stack_output("ArtifactBucketName")
        for o in json.loads((STATE_DIR / "stack-outputs.json").read_text(encoding="utf-8")):
            print(f"{o['OutputKey']}\t{o['OutputValue']}")
    elif args.action == "delete":
        bucket = stack_output("ArtifactBucketName")
        ans = input(f"スタック {C['STACK_NAME']} と S3 バケット {bucket} の中身を削除します。よろしいですか? [y/N] ")
        if ans.strip().lower() != "y":
            die("中止しました")
        if active_names():
            die("起動中のセッションがあります。先に mvm stop-all を実行してください")
        # 終了処理中の VM は terminate フックでワークスペースを S3 に保存する。
        # バケットを空にした後に書き込まれると削除に失敗するので、終了しきるまで待つ (PoC で発生)
        wait_until_terminated()
        for attempt in range(2):
            aws_interactive("s3", "rm", f"s3://{bucket}", "--recursive", "--only-show-errors")
            aws_interactive("cloudformation", "delete-stack", "--stack-name", C["STACK_NAME"])
            if aws_interactive("cloudformation", "wait", "stack-delete-complete", "--stack-name", C["STACK_NAME"]) == 0:
                break
            log("スタックの削除に失敗しました。バケットを空にしてもう一度試します")
        else:
            retained = delete_stack_retaining_failed()
            if retained is None:
                die(f"スタック {C['STACK_NAME']} を削除できませんでした。CloudFormation コンソールで原因を確認してください")
            report_retained(retained)
        (STATE_DIR / "stack-outputs.json").unlink(missing_ok=True)
        log("削除しました。イメージは mvm image delete、シークレットは Secrets Manager で個別に削除してください")
    return 0


def delete_stack_retaining_failed(max_rounds=5):
    """削除に失敗したリソースだけを残して、スタックの残りを削除する。

    権限の不足などで Lambda 関数とその実行許可を削除できないと DELETE_FAILED になり、スタックごと消せなくなる。
    依存関係の順に 1 つずつ失敗するので、失敗したものを足しながら繰り返す。
    成功したら残したリソースの一覧を、繰り返しても消せなければ None を返す。
    """
    retained = {}
    for _ in range(max_rounds):
        r = aws("cloudformation", "describe-stack-resources", "--stack-name", C["STACK_NAME"], check=False) or {}
        failed = {x["LogicalResourceId"]: x for x in r.get("StackResources", [])
                  if x.get("ResourceStatus") == "DELETE_FAILED" and x["LogicalResourceId"] not in retained}
        if not failed:
            return None
        retained.update(failed)
        log(f"削除できないリソースを残して、スタックの残りを削除します: {', '.join(retained)}")
        aws_interactive("cloudformation", "delete-stack", "--stack-name", C["STACK_NAME"],
                        "--retain-resources", *retained)
        if aws_interactive("cloudformation", "wait", "stack-delete-complete", "--stack-name", C["STACK_NAME"]) == 0:
            return list(retained.values())
    return None


def report_retained(resources):
    log("次のリソースは削除できなかったため、スタックから外して残しました:")
    for x in resources:
        print(f"  {x.get('ResourceType')}\t{x.get('PhysicalResourceId')}", file=sys.stderr)
    functions = [x["PhysicalResourceId"] for x in resources if x.get("ResourceType") == "AWS::Lambda::Function"]
    for fn in functions:
        print(f"  削除: aws lambda delete-function --function-name {fn} --region {C['AWS_REGION']}", file=sys.stderr)
    if functions:
        print("  (Lambda 関数の実行許可は関数と一緒に消えます。削除できなかった原因 (権限など) を解消してから削除してください。"
              "残したままだと次の mvm infra deploy が名前の重複で失敗します)", file=sys.stderr)
    others = [x for x in resources if x.get("ResourceType") not in ("AWS::Lambda::Function", "AWS::Lambda::Permission")]
    if others:
        print("  上記のそれ以外のリソースは、AWS コンソールで確認して削除してください", file=sys.stderr)


def infra_template():
    name = C["INFRA_TEMPLATE"]
    if name not in INFRA_TEMPLATES:
        die(f"INFRA_TEMPLATE は {' | '.join(INFRA_TEMPLATES)} のいずれかを指定してください: {name}")
    return ROOT / "infra" / INFRA_TEMPLATES[name]


def template_parameters(path):
    """テンプレートの Parameters に書かれた名前。宣言していないパラメータは渡さない。"""
    import re
    names, inside = set(), False
    for line in path.read_text(encoding="utf-8").splitlines():
        if re.match(r"^\S", line):
            inside = line.startswith("Parameters:")
        elif inside:
            m = re.match(r"^  ([A-Za-z0-9]+):", line)
            if m:
                names.add(m.group(1))
    return names


def infra_parameters():
    params = [
        f"Prefix={C['PMVA_PREFIX']}",
        f"EnableBedrock={str(as_bool(C['ENABLE_BEDROCK'])).lower()}",
        f"EnableAnthropicAws={str(as_bool(C['ENABLE_ANTHROPIC_AWS'])).lower()}",
        f"KeepaliveRateMinutes={int(C['KEEPALIVE_RATE_MINUTES'])}",
        f"KeepaliveActiveSeconds={int(C['KEEPALIVE_ACTIVE_SECONDS'])}",
        f"BudgetMonthlyUsd={float(C['BUDGET_MONTHLY_USD']):g}",
    ]
    if C["BUDGET_EMAIL"]:
        params.append(f"BudgetEmail={C['BUDGET_EMAIL']}")
    params.append(f"ImageName={C['IMAGE_NAME']}")
    params.append(f"NightlyStopCron={nightly_stop_cron(C['NIGHTLY_STOP_TIME'])}")
    params.append(f"NightlyStopTimezone={C['NIGHTLY_STOP_TIMEZONE']}")
    return params


def nightly_stop_cron(hhmm):
    """'23:00' を EventBridge Scheduler の cron 式にする。空なら夜間の自動停止は作らない。"""
    if not hhmm:
        return ""
    import re
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        die(f"NIGHTLY_STOP_TIME は HH:MM 形式で指定してください: {hhmm}")
    return f"cron({int(m.group(2))} {int(m.group(1))} * * ? *)"


def prepare_new_stack():
    """スタックを新規に作る前に、前回の削除後に残ったものを片付ける (PoC で発生)。

    - 前回の作成が途中で失敗し、中身のないスタックが REVIEW_IN_PROGRESS のまま残っている
    - スタック削除の後に終了中の VM がログを書き、ロググループがスタック管理外で作り直されている
    """
    r = aws("cloudformation", "describe-stacks", "--stack-name", C["STACK_NAME"], check=False)
    status = (r or {}).get("Stacks", [{}])[0].get("StackStatus")
    if status == "REVIEW_IN_PROGRESS":
        log(f"途中で止まった空のスタック {C['STACK_NAME']} を削除します")
        aws("cloudformation", "delete-stack", "--stack-name", C["STACK_NAME"], output=None)
        aws("cloudformation", "wait", "stack-delete-complete", "--stack-name", C["STACK_NAME"], output=None)
        status = None
    if status:
        return
    # 前回の infra delete で消せずに残した Lambda 関数があると、同じ名前で作れずに失敗する
    for fn in (f"{C['PMVA_PREFIX']}-keepalive", f"{C['PMVA_PREFIX']}-nightly-stop"):
        if aws("lambda", "get-function", "--function-name", fn, check=False):
            die(f"前回の削除で残った Lambda 関数 {fn} があります。先に削除してください:\n"
                f"  aws lambda delete-function --function-name {fn} --region {C['AWS_REGION']}")
        if "AccessDenied" in last_aws_error():
            log(f"Lambda 関数 {fn} の有無を確認できませんでした (AccessDenied)。infra deploy は Lambda 関数を作るので、"
                "権限か、AWS Organizations の SCP を確認してください")
            break
    for group in (f"/aws/lambda-microvms/{C['PMVA_PREFIX']}", f"/aws/lambda/{C['PMVA_PREFIX']}-keepalive",
                  f"/aws/lambda/{C['PMVA_PREFIX']}-nightly-stop"):
        groups = aws("logs", "describe-log-groups", "--log-group-name-prefix", group, check=False) or {}
        if any(g.get("logGroupName") == group for g in groups.get("logGroups", [])):
            log(f"スタック管理外で残っているロググループ {group} を削除します (前回の削除後に作られたもの)")
            aws("logs", "delete-log-group", "--log-group-name", group, output=None)


def wait_until_terminated(timeout=300):
    """ローカルに記録のある MicroVM が TERMINATING の間は待つ。"""
    deadline = time.time() + timeout
    for name in session_names():
        vid = load_session(name)["microvmId"]
        while time.time() < deadline:
            r = lmv("get-microvm", "--microvm-identifier", vid, check=False)
            if not r or r.get("state") != "TERMINATING":
                break
            log(f"{name} の終了を待っています")
            time.sleep(5)


HEADER_NAME = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")


def check_codex_gateway(value):
    """--codex-auth gateway のシークレット (Codex から使う、OpenAI の Responses API と同じ形の接続先)。"""
    usage = ('{"baseUrl":"https://.../v1","apiKey":"...(任意)","apiKeyHeader":"api-key (任意)",'
             '"queryParams":{...(任意)},"headers":{...(任意。秘密の値は入れない)}} 形式の JSON を渡してください')
    try:
        v = json.loads(value)
    except ValueError:
        die(usage)
    if not isinstance(v, dict) or not re.match(r"https?://\S+$", str(v.get("baseUrl", ""))):
        die(usage)
    for key in ("apiKey", "apiKeyHeader"):
        if key in v and not isinstance(v[key], str):
            die(usage)
    if "apiKeyHeader" in v and not HEADER_NAME.fullmatch(v["apiKeyHeader"]):
        die(usage)
    for key in ("queryParams", "headers"):
        if key in v and not (isinstance(v[key], dict) and all(isinstance(x, str) for x in v[key].values())
                             and (key != "headers" or all(HEADER_NAME.fullmatch(h) for h in v[key]))):
            die(usage)


# --claude-auth custom / --codex-auth custom で上書きさせない環境変数 (VM の動作に要るもの)
CUSTOM_ENV_RESERVED = re.compile(r"(PATH|HOME|USER|SHELL|LANG|TERM|TMUX.*|LD_.*|PMVA_.*)")
CUSTOM_FILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]*")


def check_custom_auth(kind, value):
    """--claude-auth custom / --codex-auth custom のシークレット: 環境変数、ファイル、エージェントの設定の組み合わせ。"""
    agent_key = "claudeSettings" if kind == "claude-custom-auth" else "codexConfig"

    def bad(why):
        die(f"{kind} の内容が正しくありません: {why} (形式は docs/auth.md の「どの方式にも当てはまらないとき (custom)」)")
    try:
        v = json.loads(value)
    except ValueError:
        bad("JSON ではありません")
    if not isinstance(v, dict):
        bad("JSON のオブジェクト ({...}) にしてください")
    unknown = set(v) - {"env", "files", agent_key}
    if unknown:
        bad(f"使えない項目があります: {', '.join(sorted(unknown))} (使えるのは env、files、{agent_key})")
    env = v.get("env", {})
    if not isinstance(env, dict):
        bad("env は {名前: 値} にしてください")
    for name, val in env.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or CUSTOM_ENV_RESERVED.fullmatch(name):
            bad(f"環境変数 {name} は使えません")
        if not isinstance(val, str):
            bad(f"環境変数 {name} の値は文字列にしてください")
    files = v.get("files", {})
    if not isinstance(files, dict):
        bad("files は {ファイル名: 中身} にしてください")
    for name, val in files.items():
        if not CUSTOM_FILE_NAME.fullmatch(name):
            bad(f"ファイル名 {name} は使えません (英数字と . _ - だけ。. で始めない)")
        if not isinstance(val, str):
            bad(f"ファイル {name} の中身は文字列にしてください")
    if agent_key == "claudeSettings" and not isinstance(v.get(agent_key, {}), dict):
        bad("claudeSettings は ~/.claude/settings.json に足す {...} にしてください")
    if agent_key == "codexConfig" and not isinstance(v.get(agent_key, ""), str):
        bad("codexConfig は ~/.codex/config.toml に足す TOML の文字列にしてください")


def cmd_secret(args):
    prefix = C["PMVA_PREFIX"]
    if args.action == "ls":
        r = aws("secretsmanager", "list-secrets", "--filters", f"Key=name,Values={prefix}/")
        for s in r.get("SecretList", []):
            print(f"{s['Name']}\t{s.get('LastChangedDate', '')}")
        return 0
    kind = args.kind
    if kind not in SECRET_KINDS:
        die(f"kind は {' | '.join(SECRET_KINDS)} のいずれかを指定してください")
    name = args.name or f"{prefix}/{kind}"
    allowed = tuple(f"{prefix}/{x}" for x in SECRET_NAME_PREFIXES[kind])
    if not name.startswith(allowed):
        die(f"{kind} のシークレット名は {' / '.join(a + '...' for a in allowed)} で始めてください"
            " (MicroVM の実行ロールが読めるのはその名前だけ)")
    if args.file:
        value = expand_path(args.file).read_text(encoding="utf-8")
    elif sys.stdin.isatty():
        value = getpass.getpass(f"{kind} の値を入力 (表示されません): ")
    else:
        value = sys.stdin.read()
    value = value.strip()
    if not value:
        die("値が空です")
    if kind == "anthropic-gateway":
        try:
            v = json.loads(value)
            assert v.get("baseUrl") and v.get("authToken")
        except (ValueError, AssertionError, AttributeError):
            die('{"baseUrl":"...","authToken":"..."} 形式の JSON を渡してください')
    if kind == "notify-webhook" and not value.startswith("https://"):
        die("Webhook の URL (https://...) を渡してください")
    if kind == "codex-gateway":
        check_codex_gateway(value)
    if kind in ("claude-custom-auth", "codex-custom-auth"):
        check_custom_auth(kind, value)
    if kind == "codex-access-token" and not re.fullmatch(r"at-\S+|[\w-]+\.[\w-]+\.[\w-]+", value):
        die("Codex のアクセストークン (at- で始まる個人用のトークン、またはエージェント用の JWT) を渡してください")
    if kind == "codex-auth-json":
        try:
            json.loads(value)
        except ValueError:
            die("ChatGPT ログイン済みの ~/.codex/auth.json (Windows は %USERPROFILE%\\.codex\\auth.json) の内容を渡してください")
    # 値をコマンドライン引数に載せない (プロセス一覧に出さない) ため一時ファイル経由で渡す
    fd, tmp = tempfile.mkstemp(dir=STATE_DIR, prefix="secret.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(value)
        uri = "file://" + tmp
        if aws("secretsmanager", "describe-secret", "--secret-id", name, check=False):
            aws("secretsmanager", "put-secret-value", "--secret-id", name, "--secret-string", uri)
            log(f"更新しました: {name}")
        else:
            aws("secretsmanager", "create-secret", "--name", name, "--secret-string", uri,
                "--tags", f"Key=pmva-kind,Value={kind}")
            log(f"作成しました: {name}")
    finally:
        os.unlink(tmp)
    return 0


# ---------------------------------------------------------------- image

def image_build_args():
    """config.env の値で書き換える Dockerfile の ARG。"""
    conf = {k: C.get(k, DEFAULTS[k]) for k in ("IMAGE_EXTRA_PACKAGES", "IMAGE_BROWSER_DEPS")}
    packages = " ".join(conf["IMAGE_EXTRA_PACKAGES"].split())
    if packages and not re.fullmatch(r"[A-Za-z0-9._+-]+( [A-Za-z0-9._+-]+)*", packages):
        die(f"IMAGE_EXTRA_PACKAGES はパッケージ名を空白で区切って書いてください: {conf['IMAGE_EXTRA_PACKAGES']}")
    return {"INSTALL_BROWSER_DEPS": "true" if as_bool(conf["IMAGE_BROWSER_DEPS"]) else "false", "EXTRA_PACKAGES": packages}


def apply_build_args(dockerfile, values):
    text = dockerfile.decode("utf-8")
    for key, value in values.items():
        text, n = re.subn(rf"^ARG {key}=.*$", lambda _m: f"ARG {key}={value}", text, flags=re.M)
        if n != 1:
            die(f"image/Dockerfile に ARG {key}= の行が見つかりません")
    return text.encode("utf-8")


def build_artifact_zip(dest):
    """image/ を zip にする。Windows で clone した場合に備え、改行を LF に、実行権限を付けて格納する。
    Dockerfile の一部の ARG は config.env の値に書き換える (Lambda MicroVMs のビルドには引数を渡せないため)。"""
    src = ROOT / "image"
    executables = {"supervisor.py", "bootstrap.sh", "snapshot.sh", "notify.sh", "agent-attach", "pmva-node"}
    build_args = image_build_args()
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(src.rglob("*")):
            if p.is_dir() or "__pycache__" in p.parts or p.suffix == ".pyc" or p.name == ".DS_Store":
                continue
            rel = p.relative_to(src).as_posix()
            info = zipfile.ZipInfo(rel, date_time=time.localtime(p.stat().st_mtime)[:6])
            info.external_attr = (0o100755 if p.name in executables else 0o100644) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            data = p.read_bytes().replace(b"\r\n", b"\n")
            if rel == "Dockerfile":
                data = apply_build_args(data, build_args)
            z.writestr(info, data)


# Dockerfile の ARG 名 → npm パッケージ名。イメージには ARG に書いた版がそのまま入る
AGENT_PACKAGES = {"CLAUDE_CODE_VERSION": "@anthropic-ai/claude-code", "CODEX_VERSION": "@openai/codex"}


def dockerfile_versions(path):
    import re
    text = path.read_text(encoding="utf-8")
    return {k: (re.search(rf"^ARG {k}=(\S*)", text, re.M) or [None, None])[1] for k in AGENT_PACKAGES}


def set_dockerfile_versions(path, versions):
    import re
    text = path.read_text(encoding="utf-8")
    for k, v in versions.items():
        if not re.fullmatch(r"[0-9A-Za-z.+-]+", v):
            die(f"バージョンの形式が正しくありません: {v}")
        text, n = re.subn(rf"^ARG {k}=\S*$", f"ARG {k}={v}", text, flags=re.M)
        if n != 1:
            die(f"{path} に ARG {k}= の行が見つかりません")
    path.write_bytes(text.encode("utf-8"))


def npm_latest(package):
    base = os.environ.get("PMVA_NPM_REGISTRY", "https://registry.npmjs.org").rstrip("/")
    url = f"{base}/{urllib.parse.quote(package, safe='@')}/latest"
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))["version"]
    except (OSError, ValueError, KeyError) as e:
        die(f"npm レジストリから {package} の最新版を取得できませんでした: {e}")


def cmd_image_versions(args):
    dockerfile = ROOT / "image" / "Dockerfile"
    pinned = dockerfile_versions(dockerfile)
    wanted = {"CLAUDE_CODE_VERSION": args.claude, "CODEX_VERSION": args.codex}
    latest = {k: wanted[k] or npm_latest(pkg) for k, pkg in AGENT_PACKAGES.items()}
    for k, pkg in AGENT_PACKAGES.items():
        mark = "" if pinned[k] == latest[k] else "  ← 更新あり"
        print(f"{pkg:28} 固定 {pinned[k] or '-':12} {'指定' if wanted[k] else '最新'} {latest[k]}{mark}")
    if args.action == "versions":
        return 0
    changed = {k: v for k, v in latest.items() if pinned[k] != v}
    if not changed:
        log("Dockerfile はすでにこの版です")
        return 0
    set_dockerfile_versions(dockerfile, changed)
    log(f"{dockerfile} を書き換えました。mvm image build で新しいイメージを作り、"
        "動作を確認してから commit してください (起動中の VM は mvm renew で新しい版に移れます)")
    return 0


def cmd_image(args):
    if args.action in ("versions", "update-versions"):
        return cmd_image_versions(args)
    if args.action == "base":
        for item in lmv("list-managed-microvm-images").get("items", []):
            print(item["imageArn"])
        return 0
    if args.action == "delete":
        arn = find_image_arn() or die(f"イメージ {C['IMAGE_NAME']} はありません")
        if not args.yes:
            ans = input(f"イメージ {arn} を全バージョンごと削除します。よろしいですか? [y/N] ")
            if ans.strip().lower() != "y":
                die("中止しました")
        lmv("delete-microvm-image", "--image-identifier", arn)
        log(f"削除を開始しました: {arn} (使うときは mvm image build で作り直せます)")
        return 0
    if args.action == "status":
        arn = find_image_arn() or die(f"イメージ {C['IMAGE_NAME']} はまだありません (mvm image build)")
        img = lmv("get-microvm-image", "--image-identifier", arn)
        print(f"{arn}\nstate={img.get('state')} latestActive={img.get('latestActiveImageVersion')} "
              f"latestFailed={img.get('latestFailedImageVersion')}")
        for v in lmv("list-microvm-image-versions", "--image-identifier", arn).get("items", []):
            print(f"  {v.get('imageVersion')}\t{v.get('state')}\t{v.get('status')}\t{v.get('createdAt')}")
        return 0

    bucket = stack_output("ArtifactBucketName")
    log_group = stack_output("LogGroupName")
    zip_path = STATE_DIR / "image-artifact.zip"
    build_artifact_zip(zip_path)
    key = f"images/{C['IMAGE_NAME']}/{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.zip"
    log(f"資材をアップロード: s3://{bucket}/{key}")
    aws("s3", "cp", "--only-show-errors", str(zip_path), f"s3://{bucket}/{key}", output=None)

    common = [
        "--base-image-arn", C["BASE_IMAGE_ARN"],
        "--build-role-arn", stack_output("BuildRoleArn"),
        "--code-artifact", json.dumps({"uri": f"s3://{bucket}/{key}"}),
        "--cpu-configurations", json.dumps([{"architecture": "ARM_64"}]),
        "--resources", json.dumps([{"minimumMemoryInMiB": int(C["MEMORY_MIB"])}]),
        "--hooks", json.dumps(HOOKS),
        "--logging", json.dumps({"cloudWatch": {"logGroup": log_group}}),
    ]
    arn = find_image_arn()
    if arn:
        log(f"新しいバージョンをビルド: {arn}")
        resp = lmv("update-microvm-image", "--image-identifier", arn, *common)
    else:
        log(f"イメージを作成: {C['IMAGE_NAME']}")
        resp = lmv("create-microvm-image", "--name", C["IMAGE_NAME"],
                   "--description", "Claude Code / Codex agent runtime (parallel-microvms-agents)", *common)
    arn, ver = resp["imageArn"], resp["imageVersion"]
    log(f"ビルド中: {arn} バージョン {ver} (数分〜十数分かかります)")
    deadline = time.time() + 3600
    while True:
        v = lmv("get-microvm-image-version", "--image-identifier", arn, "--image-version", ver, check=False) or {}
        state = v.get("state")
        if state == "SUCCESSFUL":
            print(file=sys.stderr)
            log(f"ビルド成功: バージョン {ver}")
            # mvm doctor が、イメージの元が変わったか (ビルドし直しが要るか) を確かめるのに使う
            (STATE_DIR / IMAGE_BUILD_RECORD).write_text(json.dumps(
                {"image": C["IMAGE_NAME"], "version": ver, "digest": image_digest(), "builtAt": int(time.time())}),
                encoding="utf-8")
            return 0
        if state == "FAILED":
            print(file=sys.stderr)
            builds = lmv("list-microvm-image-builds", "--image-identifier", arn, "--image-version", ver, check=False) or {}
            for b in builds.get("items", []):
                print(f"  {b.get('architecture')}\t{b.get('buildState')}\t{b.get('stateReason')}", file=sys.stderr)
            die(f"ビルド失敗: {v.get('stateReason')} (ログ: {log_group})")
        if time.time() > deadline:
            die("ビルドが 60 分以内に完了しませんでした")
        print(".", end="", file=sys.stderr, flush=True)
        time.sleep(15)


# ---------------------------------------------------------------- cost --actual

def cmd_cost_actual(args):
    """Cost Explorer の実績 (UnblendedCost) をサービス別に集計する。アカウント全体の金額であることに注意。"""
    import datetime
    if not 1 <= args.days <= 365:
        die("--days は 1〜365 で指定してください")
    end = datetime.datetime.now(datetime.timezone.utc).date() + datetime.timedelta(days=1)  # End は含まない
    start = end - datetime.timedelta(days=args.days)
    by_service, by_day, unit, token = {}, {}, "USD", None
    while True:
        extra = ["--next-page-token", token] if token else []
        r = aws("ce", "get-cost-and-usage", "--region", "us-east-1",  # Cost Explorer の API は us-east-1 のみ
                "--time-period", f"Start={start},End={end}", "--granularity", "DAILY",
                "--metrics", "UnblendedCost", "--group-by", "Type=DIMENSION,Key=SERVICE", *extra)
        for day in r.get("ResultsByTime", []):
            for g in day.get("Groups", []):
                m = g["Metrics"]["UnblendedCost"]
                amount, unit = float(m["Amount"]), m.get("Unit", unit)
                by_service[g["Keys"][0]] = by_service.get(g["Keys"][0], 0.0) + amount
                d = day["TimePeriod"]["Start"]
                by_day[d] = by_day.get(d, 0.0) + amount
        token = r.get("NextPageToken")
        if not token:
            break
    print(f"実績 {start} 〜 {end - datetime.timedelta(days=1)} (UTC、アカウント全体、{unit})")
    rows = sorted(((v, k) for k, v in by_service.items() if round(v, 2) != 0), reverse=True)
    for v, k in rows:
        print(f"  {k:48} {v:10.2f}")
    if not rows:
        print("  (0.01 以上の請求はまだありません)")
    print(f"  {'合計':46} {sum(by_service.values()):10.2f}")
    if args.daily:
        print("日ごとの合計:")
        for d in sorted(by_day):
            print(f"  {d}  {by_day[d]:10.2f}")
    print("※ Cost Explorer への反映は最大 24 時間ほど遅れます。試算は mvm cost (引数なし) を使ってください")
    return 0


# ---------------------------------------------------------------- up / fleet

def valid_name(name):
    import re
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,30}", name):
        die(f"名前は英小文字・数字・ハイフン (31 文字以内) にしてください: {name}")


def check_secret_name(option, value, kind):
    allowed = tuple(f"{C['PMVA_PREFIX']}/{x}" for x in SECRET_NAME_PREFIXES[kind])
    if value and not value.startswith(allowed):
        die(f"{option} は {' / '.join(a + '...' for a in allowed)} で始まる名前にしてください"
            " (MicroVM の実行ロールが読めるのはその名前だけ)")


def execution_role_output(payload):
    """使うエージェントの認証情報だけを読める実行ロール (スタック出力名) を選ぶ。"""
    claude = payload.get("claudeAuth", "none") != "none"
    codex = payload.get("codexAuth", "none") != "none"
    return {(True, True): "ExecutionRoleArn", (True, False): "ExecutionRoleClaudeArn",
            (False, True): "ExecutionRoleCodexArn", (False, False): "ExecutionRoleNoneArn"}[(claude, codex)]


def build_payload(name, a):
    agent = a.agent or C["DEFAULT_AGENT"]
    if agent not in AGENTS:
        die(f"--agent は {' | '.join(AGENTS)} のいずれかを指定してください")
    # 使わないエージェントの認証は (明示されない限り) 行わない
    claude_auth = a.claude_auth or (C["DEFAULT_CLAUDE_AUTH"] if agent in ("claude", "both") else "none")
    codex_auth = a.codex_auth or (C["DEFAULT_CODEX_AUTH"] if agent in ("codex", "both") else "none")
    if claude_auth not in CLAUDE_AUTHS:
        die(f"--claude-auth は {' | '.join(CLAUDE_AUTHS)} のいずれかを指定してください")
    if codex_auth not in CODEX_AUTHS:
        die(f"--codex-auth は {' | '.join(CODEX_AUTHS)} のいずれかを指定してください")
    prefix = C["PMVA_PREFIX"]
    claude_secret = a.claude_secret or default_secret(CLAUDE_AUTH_SECRETS, claude_auth)
    codex_secret = a.codex_secret or default_secret(CODEX_AUTH_SECRETS, codex_auth)
    # Claude Platform on AWS はワークスペース ID が必須 (リクエストごとに送る)
    anthropic_aws = {}
    if claude_auth.startswith("anthropic-aws"):
        workspace = C["ANTHROPIC_AWS_WORKSPACE_ID"]
        if not workspace:
            die("--claude-auth anthropic-aws には、config.env の ANTHROPIC_AWS_WORKSPACE_ID "
                "(Claude Platform on AWS のワークスペース ID、wrkspc_...) が必要です")
        if not re.fullmatch(r"wrkspc_[A-Za-z0-9]+", workspace):
            die(f"ANTHROPIC_AWS_WORKSPACE_ID は wrkspc_ で始まるワークスペース ID にしてください: {workspace}")
        anthropic_aws = {"anthropicAwsWorkspaceId": workspace, "anthropicAwsRegion": C["ANTHROPIC_AWS_REGION"]}
    if a.no_github:
        github_secret = ""
    else:
        github_secret = a.github_secret or (f"{prefix}/github-token" if as_bool(C["USE_GITHUB_TOKEN"]) else "")
    check_secret_name("--claude-secret", a.claude_secret, "claude-oauth-token")
    check_secret_name("--codex-secret", a.codex_secret, "openai-api-key")
    check_secret_name("--github-secret", a.github_secret, "github-token")
    notify = as_bool(C["NOTIFY_WEBHOOK"]) if a.notify is None else a.notify
    branch = a.branch or (f"pmva/{name}" if a.repo else "")
    bucket = stack_output("ArtifactBucketName")
    restore = f"s3://{bucket}/workspaces/{name}/latest.tar.gz" if a.restore == "latest" else (a.restore or "")
    profile = "" if a.no_profile else (a.profile or C["PROFILE"])
    profile_uri = ""
    if profile:
        check_profile_name(profile)
        profile_uri = f"s3://{bucket}/{PROFILE_PREFIX}{profile}.tar.gz"
        if not aws("s3api", "head-object", "--bucket", bucket, "--key", f"{PROFILE_PREFIX}{profile}.tar.gz", check=False):
            die(f"設定のまとまり {profile} がまだありません。mvm profile push --name {profile} で送ってください "
                "(使わないなら --no-profile)")
    ssh_key = ""
    key_file = expand_path(C["SSH_PUBLIC_KEY_FILE"])
    if key_file and key_file.exists():
        ssh_key = key_file.read_text(encoding="utf-8").strip().splitlines()[0]
    payload = {
        "name": name, "agent": agent, "region": C["AWS_REGION"],
        "claudeAuth": claude_auth, "claudeSecret": claude_secret,
        "claudeArgs": C["DEFAULT_CLAUDE_ARGS"] if a.claude_args is None else a.claude_args,
        "claudeModel": a.model or C["CLAUDE_MODEL"], "bedrockRegion": a.bedrock_region or C["BEDROCK_REGION"],
        "codexAuth": codex_auth, "codexSecret": codex_secret,
        "codexArgs": C["DEFAULT_CODEX_ARGS"] if a.codex_args is None else a.codex_args,
        "githubSecret": github_secret, "gitUserName": C["GIT_USER_NAME"], "gitUserEmail": C["GIT_USER_EMAIL"],
        "repo": a.repo or "", "branch": branch, "baseBranch": a.base_branch or "",
        "prompt": a.prompt or "", "restore": restore,
        "snapshotBucket": bucket, "snapshotIntervalSeconds": int(C["SNAPSHOT_INTERVAL_SECONDS"]),
        "sshPublicKey": ssh_key,
        "notifySecret": f"{prefix}/notify-webhook" if notify else "",
        "setupTimeoutSeconds": int(C["SETUP_TIMEOUT_SECONDS"]),
        "skipSetup": "true" if a.no_setup else "",
        "profile": profile_uri,
        **anthropic_aws,
    }
    return {k: v for k, v in payload.items() if v != ""}


def launch(name, payload, idle, max_duration):
    # 日本語は \uXXXX にエスケープし、Windows のコマンドライン経由でも文字化けしないようにする
    payload_json = json.dumps(payload, separators=(",", ":"))
    if len(payload_json.encode("utf-8")) > 4096:
        die("runHookPayload が 4096 バイトを超えています。長い指示は起動後に mvm send で送ってください")
    if is_active(name):
        die(f"セッション {name} は既に起動しています")
    running = len(active_names())
    if int(C["MAX_AGENTS"]) and running >= int(C["MAX_AGENTS"]):
        die(f"起動中のセッションが MAX_AGENTS ({C['MAX_AGENTS']}) に達しています")
    check_memory_quota(running, 1)
    arn = find_image_arn() or die(f"イメージ {C['IMAGE_NAME']} がありません。先に mvm image build を実行してください")
    ver = lmv("get-microvm-image", "--image-identifier", arn).get("latestActiveImageVersion")
    if not ver:
        die("利用可能なイメージバージョンがありません (mvm image status)")
    # 指示文などを含むペイロードはコマンドラインに載せずファイルで渡す
    # (Windows の aws.cmd 経由だと cmd.exe が & や | などの記号を解釈して壊れるため)
    payload_file = STATE_DIR / f"payload-{name}.json"
    connectors = [f"arn:aws:lambda:{C['AWS_REGION']}:aws:network-connector:aws-network-connector:{c}"
                  for c in C["INGRESS_CONNECTORS"].split()]
    args = [
        "--image-identifier", arn, "--image-version", ver,
        "--execution-role-arn", stack_output(execution_role_output(payload)),
        "--run-hook-payload", "file://" + str(payload_file),
        "--maximum-duration-in-seconds", str(max_duration),
        "--logging", json.dumps({"cloudWatch": {"logGroup": stack_output("LogGroupName")}}),
        "--client-token", f"pmva-{name}-{int(time.time())}",
    ]
    if connectors:
        args += ["--ingress-network-connectors", json.dumps(connectors)]
    if str(idle) not in ("off", "0"):
        args += ["--idle-policy", json.dumps({
            "maxIdleDurationSeconds": int(idle),
            "suspendedDurationSeconds": int(C["IDLE_SUSPENDED_SECONDS"]),
            "autoResumeEnabled": as_bool(C["IDLE_AUTO_RESUME"]),
        })]
    log(f"起動: {name} (image {ver}, {C['MEMORY_MIB']}MiB, idle={idle}, max={max_duration}s)")
    payload_file.write_text(payload_json, encoding="utf-8")
    try:
        resp = lmv("run-microvm", *args)
    finally:
        payload_file.unlink()
    endpoint = resp["endpoint"].split("://", 1)[-1].rstrip("/")
    save_session({
        "name": name, "microvmId": resp["microvmId"], "endpoint": endpoint,
        "imageVersion": resp.get("imageVersion"), "startedAt": resp.get("startedAt"),
        "idle": str(idle), "maxDuration": str(max_duration), "payload": payload, "terminated": False,
    })
    clear_tokens(name)
    log(f"起動しました: {name} ({resp['microvmId']})")
    log(f"  様子を見る: mvm peek {name}   接続: mvm ssh {name} (または mvm shell {name})")


def cmd_up(args):
    valid_name(args.name)
    payload = build_payload(args.name, args)
    launch(args.name, payload, args.idle or C["IDLE_MAX_SECONDS"], args.max_duration or C["MAX_DURATION_SECONDS"])
    return 0


def cmd_fleet(args):
    if args.count < 1:
        die("台数は 1 以上にしてください")
    if int(C["MAX_AGENTS"]) and args.count > int(C["MAX_AGENTS"]):
        die(f"台数は MAX_AGENTS ({C['MAX_AGENTS']}) 以下にしてください")
    prompts = []
    if args.prompts_file:
        prompts = expand_path(args.prompts_file).read_text(encoding="utf-8").splitlines()
    names = [f"{args.prefix}-{i:02d}" for i in range(1, args.count + 1)]
    running = active_names()
    # 1 台目を起動する前に、全台分がクォータに収まるかを確かめる
    check_memory_quota(len(running), len([n for n in names if n not in running]))
    for i, name in enumerate(names, start=1):
        if name in running:
            log(f"{name} は起動済みのためスキップ")
            continue
        a = argparse.Namespace(**vars(args))
        a.name = name
        if i <= len(prompts) and prompts[i - 1].strip():
            a.prompt = prompts[i - 1].strip()
        try:
            cmd_up(a)
        except SystemExit as e:
            print(e.code, file=sys.stderr)
            log(f"{name} の起動に失敗しました")
    return 0


# ---------------------------------------------------------------- 一覧 / 停止

def print_json(obj):
    print(json.dumps(obj, ensure_ascii=False, indent=2))


def cmd_ls(args):
    fmt = "{:<16} {:<11} {:<7} {:<22} {:<22} {}"
    rows = []
    if not args.json:
        print(fmt.format("NAME", "STATE", "AGENT", "AUTH", "STARTED", "MICROVM"))
    for name in session_names():
        s = load_session(name)
        if s.get("terminated") and not args.all:
            continue
        state = vm_state(name)
        if state in ("TERMINATED", "GONE") and not args.all:
            continue
        p = s.get("payload", {})
        auth = f"{p.get('claudeAuth', '-')}/{p.get('codexAuth', '-')}"
        started = s.get("startedAt") or "-"
        if isinstance(started, (int, float)):  # AWS CLI v1 は UNIX 秒で返す
            started = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started))
        if args.json:
            rows.append({"name": name, "state": state, "agent": p.get("agent"), "claudeAuth": p.get("claudeAuth"),
                         "codexAuth": p.get("codexAuth"), "repo": p.get("repo"), "branch": p.get("branch"),
                         "startedAt": started if started != "-" else None, "microvmId": s["microvmId"]})
        else:
            print(fmt.format(name, state, p.get("agent", "-"), auth, started, s["microvmId"]))
    if args.json:
        print_json(rows)
    return 0


def save_workspace(name):
    log(f"保存中: {name}")
    r = vm_api(name, "POST", "/snapshot", {}, timeout=900)
    print(r.get("output") or r.get("error") or r)
    return bool(r.get("ok"))


def wait_running(name, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if vm_state(name) == "RUNNING":
            return True
        time.sleep(2)
    return False


def stop(name, save=True, require_save=False):
    """保存してから終了する。require_save=True なら、保存に失敗したときは終了せずに止める (renew 用)。"""
    state = vm_state(name)
    if state in ("TERMINATED", "GONE"):
        log(f"{name} は既に終了しています")
        return
    if require_save and state not in ("RUNNING", "SUSPENDED"):
        die(f"{name} の状態が {state} のため保存できません。作業内容を失わないよう、終了せずに止めました")
    if save and state in ("RUNNING", "SUSPENDED"):
        if state == "SUSPENDED":
            # サスペンド中は保存できないので再開する (自動レジュームが無効でも保存できるように明示的に)
            log(f"{name} はサスペンド中なので、保存のためにレジュームします")
            lmv("resume-microvm", "--microvm-identifier", load_session(name)["microvmId"], check=False)
            wait_running(name)
        try:
            saved = save_workspace(name)
        except SystemExit as e:
            print(e.code, file=sys.stderr)
            saved = False
        if not saved:
            if require_save:
                die(f"{name} の保存に失敗したので、終了せずに止めました (作業内容を失わないため)。"
                    f"原因を確認して再実行するか、保存せずに作り直すなら mvm stop {name} --no-save → "
                    f"mvm up {name} --restore latest (最後に保存できた時点に戻ります)")
            log("保存に失敗しました (terminate フックでもう一度試みます)")
    s = load_session(name)
    lmv("terminate-microvm", "--microvm-identifier", s["microvmId"])
    s["terminated"] = True
    save_session(s)
    clear_tokens(name)
    log(f"終了しました: {name}")


def cmd_stop(args):
    stop(args.name, save=not args.no_save)
    return 0


def cmd_stop_all(args):
    for name in session_names():
        if not load_session(name).get("terminated"):
            try:
                stop(name, save=not args.no_save)
            except SystemExit as e:
                print(e.code, file=sys.stderr)
    return 0


def cmd_renew(args):
    s = load_session(args.name)
    bucket = stack_output("ArtifactBucketName")
    payload = dict(s["payload"])
    payload["restore"] = f"s3://{bucket}/workspaces/{args.name}/latest.tar.gz"
    payload.pop("prompt", None)
    stop(args.name, require_save=True)  # 保存できないまま作り直すと、古いスナップショットに戻ってしまう
    launch(args.name, payload, s.get("idle") or C["IDLE_MAX_SECONDS"], s.get("maxDuration") or C["MAX_DURATION_SECONDS"])
    return 0


def cmd_suspend(args):
    lmv("suspend-microvm", "--microvm-identifier", load_session(args.name)["microvmId"])
    log(f"サスペンドしました: {args.name}")
    return 0


def cmd_resume(args):
    lmv("resume-microvm", "--microvm-identifier", load_session(args.name)["microvmId"])
    log(f"レジュームしました: {args.name}")
    return 0


def cmd_save(args):
    return 0 if save_workspace(args.name) else 1


# ---------------------------------------------------------------- 確認 / 操作

AGENT_STATE_LABELS = {
    "starting": "起動の準備中", "working": "作業中", "done": "応答が終わった", "waiting": "許可・入力待ち",
    "ready": "最初の指示待ち", "exited": "エージェントが終了した", "failed": "起動の準備に失敗", "unknown": "不明",
}


def gib(mib):
    return f"{(mib or 0) / 1024:.1f}"


def format_status(st):
    agents = st.get("agents") or {}
    states = " ".join(f"{a}={v.get('state')}" for a, v in agents.items())
    mem = st.get("memory") or {}
    memory = f"mem={gib(mem.get('usedMiB'))}/{gib(mem.get('totalMiB'))}GiB " if mem.get("totalMiB") else ""
    lines = [f"[{st.get('name')}] agent={st.get('agent')} {states + ' ' if states else ''}{memory}"
             f"bootstrap={st.get('bootstrap')} lastSnapshot={st.get('lastSnapshot') or '-'}"]
    if st.get("oomKills"):
        lines.append(f"!! メモリ不足でプロセスが {st['oomKills']} 回強制終了されました (mvm top {st.get('name')} で内訳を確認)")
    for w in st.get("windows", []):
        if w.get("name") == "shell":
            continue
        lines.append(f"--- {w.get('name')} ({w.get('command')}) ---")
        lines.append(w.get("screen") or "")
    return "\n".join(lines)


def cmd_peek(args):
    st = vm_api(args.name, "GET", f"/status?lines={args.lines}")
    if args.json:
        print_json(st)
    else:
        print(format_status(st))
    return 0


def session_agents(name, agent=None):
    if agent:
        return [agent]
    return {"claude": ["claude"], "codex": ["codex"], "both": ["claude", "codex"]}.get(
        load_session(name)["payload"].get("agent"), [])


def cmd_result(args):
    agents = session_agents(args.name, args.agent)
    if not agents:
        die(f"{args.name} ではエージェントを起動していません (--agent none)")
    results = []
    for agent in agents:
        r = vm_api(args.name, "GET", f"/result?agent={agent}")
        if "agent" not in r:
            die(f"{args.name}: 最後の応答を取得できません ({r.get('error') or r})。"
                "イメージが古い場合は mvm image build で作り直してください")
        results.append(r)
    if args.json:
        print_json(results if len(results) > 1 else results[0])
    else:
        for r in results:
            if len(results) > 1:
                print(f"--- {r['agent']} ({r.get('state')}) ---")
            print(r.get("text") if r.get("text") else f"(まだ {r['agent']} の応答がありません)")
    return 0 if all(r.get("text") for r in results) else 1


# ---------------------------------------------------------------- 会話記録の流れ (mvm follow)

FOLLOW_LABELS = {"prompt": "指示", "thinking": "思考", "text": "応答", "tool": "実行", "output": "結果"}
FOLLOW_OUTPUT_LINES = 5   # --full なしでは、ツールの結果は先頭の数行だけ表示する


def format_event(prefix, ev, full):
    label = FOLLOW_LABELS.get(ev.get("kind"), ev.get("kind"))
    if ev.get("kind") == "output" and ev.get("error"):
        label = "エラー"
    text = (ev.get("text") or "").rstrip("\n")
    if ev.get("kind") == "tool":
        text = f"{ev.get('name')}: {text}"
    lines = text.splitlines() or [""]
    more = ""
    if ev.get("kind") == "output" and not full and len(lines) > FOLLOW_OUTPUT_LINES:
        more = f"… (残り {len(lines) - FOLLOW_OUTPUT_LINES} 行)"
        lines = lines[:FOLLOW_OUTPUT_LINES]
    if ev.get("truncated") and not more:
        more = "… (省略。全文は --full)"
    head = f"{prefix} {label}: "
    out = [head + lines[0]] + ["    " + ln for ln in lines[1:]]
    if more:
        out.append("    " + more)
    return "\n".join(out)


def cmd_follow(args):
    names = args.names or active_names()
    if not names:
        die("表示するセッションがありません (mvm ls で確認)")
    streams = []   # [セッション名, エージェント, 会話記録のファイル, 読んだ位置]
    for name in names:
        for agent in session_agents(name, args.agent):
            streams.append([name, agent, "", 0])
    if not streams:
        die("エージェントを起動していないセッションです (--agent none)")
    limit = 0 if args.full else args.chars
    width = max(len(f"[{n} {a}]") for n, a, _, _ in streams)
    failing = set()
    try:
        while streams:
            for st in list(streams):
                name, agent, file, offset = st
                query = urllib.parse.urlencode({"agent": agent, "file": file, "offset": offset,
                                                "tail": args.lines, "max": limit})
                try:
                    r = vm_api(name, "GET", f"/events?{query}", timeout=30)
                except SystemExit as e:
                    if vm_state(name) in ("TERMINATED", "TERMINATING", "GONE"):
                        log(f"{name} は終了しました")
                        streams.remove(st)
                    elif name not in failing:
                        log(f"{name}: 会話記録を取得できません。再試行します ({e.code})")
                        failing.add(name)
                    continue
                failing.discard(name)
                if "events" not in r:
                    die(f"{name}: 会話記録を取得できません ({r.get('error') or r})。"
                        "イメージが古い場合は mvm image build で作り直してください")
                prefix = f"[{name} {agent}]".ljust(width)
                if r.get("reset") and file and file != "-":
                    if args.json:
                        print(json.dumps({"session": name, "agent": agent, "kind": "reset"}, ensure_ascii=False), flush=True)
                    else:
                        print(f"{prefix} --- 新しい会話 ---", flush=True)
                for ev in r["events"]:
                    if args.json:
                        print(json.dumps(dict(ev, session=name, agent=agent), ensure_ascii=False), flush=True)
                    else:
                        print(format_event(prefix, ev, args.full), flush=True)
                # まだ会話の記録がなかったときは "-" を覚えておく。記録ができたら、末尾だけでなく最初から受け取る
                st[2], st[3] = r.get("file") or "-", r.get("offset") or 0
            if args.once:
                break
            time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    return 0


def cmd_top(args):
    names = args.names or active_names()
    if not names:
        die("表示するセッションがありません (mvm ls で確認)")
    results = []
    for name in names:
        vm = vm_state(name)
        r = {"name": name, "vm": vm}
        if vm == "RUNNING":
            try:
                st = vm_api(name, "GET", "/status?lines=0", timeout=15)
                r.update(memory=st.get("memory"), oomKills=st.get("oomKills"))
            except SystemExit as e:
                r["error"] = str(e.code)
        results.append(r)
    if args.json:
        print_json(results)
        return 0
    for r in results:
        mem = r.get("memory")
        if not mem:
            note = r.get("error") or ("状態を返さない古いイメージです (mvm image build で作り直してください)"
                                      if r["vm"] == "RUNNING" else "")
            print(f"[{r['name']}] {r['vm']} {note}".rstrip())
            continue
        print(f"[{r['name']}] 使用 {gib(mem['usedMiB'])} GiB / {gib(mem['totalMiB'])} GiB "
              f"(空き {gib(mem['availableMiB'])} GiB)")
        if r.get("oomKills"):
            print(f"  !! メモリ不足でプロセスが {r['oomKills']} 回強制終了されました")
        for g in mem.get("top") or []:
            print(f"  {g['name']:<20} {g['count']:>4} 個  {g['rssMiB']:>7} MiB")
    return 0


# ---------------------------------------------------------------- 手元のエージェントの設定を VM に渡す (mvm profile)

PROFILE_PREFIX = "profiles/"
# 渡すもの: ルール、skill、サブエージェント、コマンドなど。settings.json / config.toml (認証情報、MCP、手元のパス、
# フックを含みやすい) と、認証情報のファイルは渡さない
PROFILE_ITEMS = {
    "claude": ("CLAUDE.md", "skills", "agents", "commands", "output-styles"),
    "codex": ("AGENTS.md", "skills", "prompts"),
}
PROFILE_SKIP_NAMES = {".system", "mvm-orchestrator", ".git", "node_modules", "__pycache__",
                      ".venv", ".DS_Store", "Thumbs.db"}
PROFILE_MAX_FILE = 1024 * 1024          # 1 ファイルの上限
PROFILE_MAX_TOTAL = 20 * 1024 * 1024    # 合計の上限
PROFILE_TEXT_SUFFIXES = {".md", ".txt", ".sh", ".bash", ".py", ".js", ".mjs", ".cjs", ".ts", ".json", ".yaml", ".yml",
                         ".toml", ".ini", ".cfg", ".csv", ".html", ".css", ".sql", ""}
SECRET_PATTERN = re.compile(
    rb"(sk-ant-[A-Za-z0-9_-]{16,}|sk-(proj-)?[A-Za-z0-9_-]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}"
    rb"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}|-----BEGIN [A-Z ]*PRIVATE KEY-----|xox[abprs]-[A-Za-z0-9-]{10,}"
    rb"|hooks\.slack\.com/services/[A-Za-z0-9/]+)")


def check_profile_name(name):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", name):
        die(f"設定のまとまりの名前は英小文字・数字・ハイフンにしてください: {name}")


def agent_homes():
    return {"claude": Path.home() / ".claude", "codex": Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")}


def collect_profile(agents):
    """渡すファイルを集める。戻り値は ([(tar の中の名前, 中身, 実行権限)], [(場所, 渡さない理由)], [(場所, 注意)])。"""
    homes = agent_homes()
    files, skipped, notes = [], [], []
    home_text = str(Path.home()).encode("utf-8")
    total = 0
    for agent in agents:
        base = homes[agent]
        for item in PROFILE_ITEMS[agent]:
            top = base / item
            if not top.exists():
                continue
            candidates = [top] if top.is_file() else sorted(p for p in top.rglob("*"))
            for path in candidates:
                rel = path.relative_to(base)
                if any(part in PROFILE_SKIP_NAMES for part in rel.parts):
                    continue
                if path.is_symlink():
                    skipped.append((path, "シンボリックリンク"))
                    continue
                if path.is_dir():
                    continue
                size = path.stat().st_size
                if size > PROFILE_MAX_FILE:
                    skipped.append((path, f"大きすぎる ({size // 1024}KB。1 ファイル {PROFILE_MAX_FILE // 1024 // 1024}MB まで)"))
                    continue
                data = path.read_bytes()
                if SECRET_PATTERN.search(data):
                    skipped.append((path, "認証情報 (API キー、トークン、秘密鍵など) らしい文字列を含む"))
                    continue
                if path.suffix.lower() in PROFILE_TEXT_SUFFIXES and b"\0" not in data:
                    data = data.replace(b"\r\n", b"\n")   # Windows で書いたスクリプトも Linux で動くように
                    if home_text in data or (IS_WINDOWS and home_text.replace(b"\\", b"/") in data):
                        notes.append((path, "手元のパスを含む (VM では見つからない)"))
                total += len(data)
                if total > PROFILE_MAX_TOTAL:
                    die(f"渡すファイルの合計が {PROFILE_MAX_TOTAL // 1024 // 1024}MB を超えました。大きなファイルを含む skill を見直してください")
                executable = path.suffix in (".sh", ".bash", ".py") or data.startswith(b"#!")
                files.append((f"{agent}/{rel.as_posix()}", data, executable))
    return files, skipped, notes


def write_profile_tar(dest, files):
    with tarfile.open(dest, "w:gz", format=tarfile.PAX_FORMAT) as tar:
        for name, data, executable in files:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), 0o755 if executable else 0o644, int(time.time())
            tar.addfile(info, io.BytesIO(data))


def cmd_profile(args):
    agents = ["claude", "codex"] if args.agent == "both" else [args.agent]
    name = args.name
    check_profile_name(name)
    files, skipped, notes = collect_profile(agents)
    for path, reason in skipped:
        print(f"  渡さない: {path} ({reason})")
    for path, reason in notes:
        print(f"  注意:     {path} ({reason})")
    if args.action == "show" or args.dry_run:
        for arc, data, _ in files:
            print(f"  {arc}  ({len(data):,} バイト)")
        print(f"{len(files)} ファイルを渡します (まだ送っていません)")
        return 0
    if not files:
        die("渡すものがありません (" + ", ".join(f"{a}: {', '.join(PROFILE_ITEMS[a])}" for a in agents) + ")")
    bucket = stack_output("ArtifactBucketName")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tar_path = STATE_DIR / f"profile-{name}.tar.gz"
    write_profile_tar(tar_path, files)
    key = f"{PROFILE_PREFIX}{name}.tar.gz"
    aws("s3", "cp", "--only-show-errors", str(tar_path), f"s3://{bucket}/{key}", output=None)
    tar_path.unlink()
    log(f"{len(files)} ファイルを送りました: s3://{bucket}/{key}")
    if C["PROFILE"] != name:
        log(f"起動する VM で使うには、config.env に PROFILE={name} を書くか、mvm up に --profile {name} を付けてください")
    log("起動中の VM には反映されません (次に起動・renew したときから)")
    return 0


# ---------------------------------------------------------------- 手元のエージェントを司令塔にする

ORCH_SKILL = "mvm-orchestrator"
ORCH_READONLY = ("ls", "peek", "wait", "result", "follow", "top", "doctor")   # 確認なしで実行してよいコマンド
ORCH_BEGIN = "<!-- pmva:orchestrator (mvm orchestrator install が書いた部分。消すときは mvm orchestrator uninstall) -->"
ORCH_END = "<!-- /pmva:orchestrator -->"
ORCH_AGENTS_MD = """## この PC は司令塔

- 依存関係のインストール、ビルド、テスト、開発サーバー、ブラウザなどの重い処理は、この PC で動かさない。
  mvm-orchestrator の skill に従って、MicroVM のエージェントに任せる (この PC の RAM を使わないため)
- どうしてもこの PC で動かす必要があるときは、理由をユーザーに説明して了承を得てから動かす"""


def quote_path(path):
    text = Path(path).as_posix()
    return f'"{text}"' if IS_WINDOWS or " " in text else shlex.quote(text)


def mvm_invocation():
    """手元のエージェントが mvm を呼ぶときのコマンド。エイリアスはエージェントのシェルでは使えないので、絶対パスにする。"""
    if IS_WINDOWS:
        return f"{quote_path(sys.executable)} {quote_path(SCRIPT)}"
    return quote_path(ROOT / "bin" / "mvm")


def guard_command():
    return f"{quote_path(sys.executable)} {quote_path(ROOT / 'scripts' / 'orchestrator_guard.py')}"


def is_orchestrator_rule(rule):
    return isinstance(rule, str) and bool(re.match(r'^Bash\(.*(bin/mvm|mvm\.py)"? (' + "|".join(ORCH_READONLY) + r'):\*\)$', rule))


def is_guard_entry(entry):
    return isinstance(entry, dict) and any("orchestrator_guard.py" in str(h.get("command", ""))
                                           for h in entry.get("hooks") or [] if isinstance(h, dict))


def update_json(path, change):
    """JSON の設定ファイルを読み、change で書き換えて保存する。壊れたファイルは上書きしない。"""
    data = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
        except ValueError as e:
            die(f"{path} を JSON として読めないので、書き換えずに止めました ({e})。直してからもう一度実行してください")
        if not isinstance(data, dict):
            die(f"{path} の中身がオブジェクトではないので、書き換えずに止めました")
    change(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def upsert_block(path, body):
    """ファイルの pmva の部分 (目印の間) を書き換える。body が None なら消す。ほかの部分には触らない。"""
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    pattern = re.compile(re.escape(ORCH_BEGIN) + r".*?" + re.escape(ORCH_END) + r"\n?", re.S)
    text, removed = pattern.subn("", text)
    if removed and text.strip():
        text = text.rstrip("\n") + "\n"   # 書き足したときに空けた行も戻す
    if body is not None:
        text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + f"{ORCH_BEGIN}\n{body}\n{ORCH_END}\n"
    if text.strip() or path.exists():
        path.write_text(text, encoding="utf-8")


def orchestrator_paths(agent, folder):
    """skill を置く場所と、設定ファイル。--dir なしは利用者全体、ありはそのフォルダだけ。"""
    if agent == "claude":
        base = Path(folder) / ".claude" if folder else Path.home() / ".claude"
        settings = base / ("settings.local.json" if folder else "settings.json")
        return base / "skills" / ORCH_SKILL, settings
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    base = Path(folder) / ".agents" / "skills" if folder else codex_home / "skills"
    return base / ORCH_SKILL, Path(folder) / "AGENTS.md" if folder else None


def cmd_orchestrator(args):
    folder = Path(args.dir).resolve() if args.dir else None
    if folder and not folder.is_dir():
        die(f"フォルダ {folder} がありません")
    skill_dir, settings = orchestrator_paths(args.agent, folder)
    mvm = mvm_invocation()
    rules = [f"Bash({mvm} {sub}:*)" for sub in ORCH_READONLY]

    if args.action == "uninstall":
        if skill_dir.exists():
            shutil.rmtree(skill_dir)
        if args.agent == "claude" and settings.exists():
            def remove(data):
                perms = data.get("permissions") or {}
                if isinstance(perms.get("allow"), list):
                    perms["allow"] = [r for r in perms["allow"] if not is_orchestrator_rule(r)]
                hooks = data.get("hooks") or {}
                if isinstance(hooks.get("PreToolUse"), list):
                    hooks["PreToolUse"] = [e for e in hooks["PreToolUse"] if not is_guard_entry(e)]
            update_json(settings, remove)
        if args.agent == "codex" and settings and settings.exists():
            upsert_block(settings, None)
        log(f"{args.agent} の司令塔の設定を取り除きました ({skill_dir})")
        return 0

    skill_dir.mkdir(parents=True, exist_ok=True)
    template = (ROOT / "skills" / ORCH_SKILL / "SKILL.md").read_text(encoding="utf-8")
    (skill_dir / "SKILL.md").write_text(template.replace("{{MVM}}", mvm), encoding="utf-8")
    print(f"skill: {skill_dir / 'SKILL.md'}")
    if args.agent == "claude":
        def add(data):
            perms = data.setdefault("permissions", {})
            perms["allow"] = [r for r in perms.get("allow") or [] if not is_orchestrator_rule(r)] + rules
            if folder:
                hooks = data.setdefault("hooks", {})
                hooks["PreToolUse"] = [e for e in hooks.get("PreToolUse") or [] if not is_guard_entry(e)] + [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": guard_command()}]}]
        update_json(settings, add)
        print(f"設定: {settings}")
        print(f"  確認なしで実行できるようにしたコマンド: mvm {' / '.join(ORCH_READONLY)}"
              " (VM を起動・終了するコマンドは、これまでどおり毎回確認します)")
        if folder:
            print("  重い処理 (npm / pytest / ビルド / ブラウザなど) を止めるフックを入れました。"
                  f"このフォルダで起動した Claude Code だけが対象です")
    else:
        if folder:
            upsert_block(settings, ORCH_AGENTS_MD)
            print(f"AGENTS.md: {settings} (重い処理をこの PC で動かさない決まりを追記)")
        print("Codex のサンドボックス (workspace-write) で使うときは、mvm が AWS に接続し、状態を保存できるよう、"
              "~/.codex/config.toml に次を足してください:\n")
        print("[sandbox_workspace_write]\nnetwork_access = true\n"
              f"writable_roots = [{json.dumps(STATE_DIR.as_posix())}, {json.dumps((Path.home() / '.aws').as_posix())}]\n")
    print(f"{'Claude Code' if args.agent == 'claude' else 'Codex'} を{'このフォルダで' if folder else ''}起動し直すと使えます。"
          "例: 「このリポジトリの issue 3 件を、MicroVM で並列に直して」")
    return 0


# mvm wait が「終わった」とみなすエージェントの状態と、まとめるときの優先順 (先にあるものを代表にする)
WAIT_FINISHED = ("failed", "exited", "waiting", "ready", "done")


def wait_check(name, idle_limit):
    """1 台の状態を調べる。finished=True なら、作業が終わったか人の対応が必要で、待つのをやめてよい。"""
    r = {"name": name, "finished": False}
    vm = vm_state(name)
    r["vm"] = vm
    if vm in ("TERMINATED", "TERMINATING", "GONE"):
        return dict(r, state="terminated", finished=True)
    if vm == "SUSPENDED":
        # 作業中の VM はキープアライブとアイドル判定で起きているので、サスペンドしているなら作業はしていない
        return dict(r, state="suspended", finished=True)
    if vm == "UNKNOWN":
        return dict(r, state="unreachable")   # AWS から状態を取れなかった。次の確認でもう一度試す
    if vm != "RUNNING":
        return dict(r, state="starting")
    try:
        st = vm_api(name, "GET", "/status?lines=0", timeout=15)
    except SystemExit as e:
        return dict(r, state="unreachable", error=str(e.code))
    agents = st.get("agents")
    r.update(idleSeconds=st.get("idleSeconds"), bootstrap=st.get("bootstrap"))
    boot = str(st.get("bootstrap") or "")
    if boot.startswith("failed"):
        return dict(r, state="failed", agents=agents or {}, finished=True)
    if agents is None:
        # 古いイメージ (エージェントの状態を返さない): 画面と CPU が止まっているかだけで判断する
        idle = st.get("idleSeconds") or 0
        if boot.startswith("ready") and idle_limit and idle >= idle_limit:
            return dict(r, state="stalled", finished=True)
        return dict(r, state="working" if boot.startswith("ready") else "starting")
    r["agents"] = agents
    if not agents:
        return dict(r, state="ready" if boot.startswith("ready") else "starting", finished=boot.startswith("ready"))
    states = [a.get("state") for a in agents.values()]
    if all(x in WAIT_FINISHED for x in states):
        return dict(r, state=min(states, key=WAIT_FINISHED.index), finished=True)
    idle = st.get("idleSeconds") or 0
    if boot.startswith("ready") and idle_limit and idle >= idle_limit:
        # 作業中のはずなのに画面も CPU も動かない (Codex の承認待ちなど、フックで分からない待ち)
        return dict(r, state="stalled", finished=True)
    return dict(r, state="starting" if "starting" in states else "working")


def format_wait(r):
    line = f"{r['name']:<16} {r['state']}"
    agents = r.get("agents") or {}
    if agents:
        line += "  (" + ", ".join(f"{a}: {AGENT_STATE_LABELS.get(v.get('state'), v.get('state'))}"
                                  for a, v in agents.items()) + ")"
    if r["state"] == "stalled":
        line += f"  (画面と CPU が {r.get('idleSeconds')} 秒動いていません)"
    for a, v in agents.items():
        if v.get("state") == "waiting" and v.get("message"):
            line += f"\n    {a}: {v['message']}"
    return line


def cmd_wait(args):
    names = args.names or active_names()
    if not names:
        die("待つセッションがありません (mvm ls で確認)")
    for name in names:
        load_session(name)
    deadline = time.time() + args.timeout if args.timeout > 0 else None
    results, pending = {}, list(names)
    while True:
        for name in list(pending):
            r = results[name] = wait_check(name, args.idle)
            if r["finished"]:
                pending.remove(name)
                if not args.json:
                    print(format_wait(r), flush=True)
        enough = not pending or (args.any and len(pending) < len(names))
        if enough or (deadline is not None and time.time() >= deadline):
            break
        time.sleep(args.interval if deadline is None else max(0.0, min(args.interval, deadline - time.time())))
    if args.json:
        print_json({"finished": enough, "sessions": [results[n] for n in names]})
    elif pending:
        log(("時間切れです。" if not enough else "") + "まだ終わっていないセッション: " +
            ", ".join(f"{n} ({results[n]['state']})" for n in pending))
    return 0 if enough else 2


def cmd_send(args):
    text = " ".join(args.text)
    window = args.window or ("codex" if load_session(args.name)["payload"].get("agent") == "codex" else "claude")
    r = vm_api(args.name, "POST", "/send", {"window": window, "text": text, "enter": not args.no_enter})
    print("送信しました" if r.get("ok") else f"失敗: {r.get('error')}")
    return 0 if r.get("ok") else 1


def clear_screen():
    os.system("cls" if IS_WINDOWS else "clear")


def cmd_watch(args):
    try:
        while True:
            out = [time.strftime("%Y-%m-%d %H:%M:%S")]
            for name in session_names():
                if load_session(name).get("terminated"):
                    continue
                state = vm_state(name)
                out.append("")
                if state == "RUNNING":
                    try:
                        out.append(format_status(vm_api(name, "GET", "/status?lines=6", timeout=15)))
                    except SystemExit:
                        out.append(f"[{name}] RUNNING (状態 API 応答なし)")
                else:
                    out.append(f"[{name}] {state}")
            clear_screen()
            print("\n".join(out))
            print(f"\n({args.interval} 秒ごとに更新 / Ctrl-C で終了。表示中のセッションは自動サスペンドされません)", flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def cmd_shell(args):
    s = load_session(args.name)
    r = lmv("create-microvm-shell-auth-token", "--microvm-identifier", s["microvmId"], "--expiration-in-minutes", "15")
    token = r["authToken"]["X-aws-proxy-auth"]
    log("接続します。入ったら agent-attach でエージェントの tmux に接続できます (切断は exit)")
    try:
        host, port, tls = ws_target(s["endpoint"])
        ws = WebSocket(host, "/shell", ws_protocols(token, SHELL_PORT), port=port, tls=tls)
    except OSError as e:
        die(f"シェルに接続できません: {e}")
    # 接続直後に {"type":"session_init",...} という制御メッセージが届くので表示しない (PoC で確認)
    def is_control(data):
        return data.startswith(b'{"type":"session_') and data.rstrip().endswith(b"}")

    with RawTerminal() as term:
        pump(ws, lambda: os.read(0, 4096), skip=is_control,
             on_close=lambda: term.restore and term.restore())
    return 0


def proxy_command(name):
    py = Path(sys.executable).as_posix()
    return f'"{py}" "{SCRIPT.as_posix()}" _ssh-proxy {name}'


def ssh_options(name):
    return [
        "-o", f"ProxyCommand={proxy_command(name)}",
        "-o", "StrictHostKeyChecking=no", "-o", f"UserKnownHostsFile={os.devnull}",
        "-o", "LogLevel=ERROR", "-o", "ServerAliveInterval=30", "-o", "ServerAliveCountMax=4",
    ]


def ssh_binary(name):
    ssh = shutil.which("ssh") or die("ssh が見つかりません (Windows は「オプション機能」で OpenSSH クライアントを追加)")
    if not load_session(name)["payload"].get("sshPublicKey"):
        die(f"{name} は SSH 公開鍵なしで起動されています (SSH_PUBLIC_KEY_FILE を設定して mvm renew)")
    return ssh


def cmd_ssh(args):
    ssh = ssh_binary(args.name)
    # 名前の後ろはすべて remote_command に入る (argparse.REMAINDER)。-- より前の --no-reconnect はオプションとして扱い、
    # -- の後ろはそのままコマンドにする (mvm ssh a1 --no-reconnect、mvm ssh a1 --no-reconnect -- ls -la)
    rest = list(args.remote_command)
    while rest and rest[0] == "--no-reconnect":
        args.no_reconnect = True
        rest.pop(0)
    command = rest[1:] if rest[:1] == ["--"] else rest
    if command:
        return subprocess.call([ssh, *ssh_options(args.name), f"agent@pmva-{args.name}", *command])
    return attach_tmux(args.name, ["agent-attach"], args.no_reconnect)


def cmd_view(args):
    """エージェントの画面を読み取り専用で見る。キー入力はエージェントに届かない。"""
    ssh_binary(args.name)
    window = args.window or ("codex" if load_session(args.name)["payload"].get("agent") == "codex" else "claude")
    log(f"{args.name} の {window} を読み取り専用で表示します (入力はエージェントに届きません。抜けるのは Ctrl-b → d)")
    return attach_tmux(args.name, ["agent-attach", "--view", window], args.no_reconnect)


def parse_forward(spec):
    """「3000」「8080:3000」(手元のポート:VM のポート) を (手元, VM) にする。"""
    m = re.fullmatch(r"(?:(\d+):)?(\d+)", spec)
    if not m or not all(0 < int(x) < 65536 for x in m.groups() if x):
        die(f"ポートの指定が正しくありません: {spec} (例: 3000 または 8080:3000)")
    return int(m.group(1) or m.group(2)), int(m.group(2))


def cmd_forward(args):
    """VM の中のポート (開発サーバーなど) を、手元の PC のポートにつなぐ。"""
    pairs = [parse_forward(spec) for spec in args.ports]
    for local, remote in pairs:
        if not port_free(local):
            die(f"手元のポート {local} は使われています。別のポートを指定してください (例: {local + 1}:{remote})")
    forwards = []
    for local, remote in pairs:
        forwards += ["-L", f"127.0.0.1:{local}:127.0.0.1:{remote}"]
        log(f"http://localhost:{local}/ → {args.name} のポート {remote}")
    log("Ctrl-C で終了します (つないでいる間、この VM はサスペンドされません)")
    try:
        return attach_tmux(args.name, None, args.no_reconnect,
                           extra=["-N", "-o", "ExitOnForwardFailure=yes", *forwards])
    except KeyboardInterrupt:
        return 0


CODEX_LOGIN_PORTS = (1455, 1457)   # codex login (ブラウザ) が VM の中で戻り先を待つポート (1455 が使えないときは 1457)
LOGIN_URL = re.compile(r"https://auth\.openai\.com/\S+")
VM_SHELL = ". ~/.bashrc.pmva 2>/dev/null; cd \"${PMVA_WORKDIR:-$HOME}\"; "   # tmux のシェルと同じ環境で動かす


def port_free(port):
    """手元のポートで待ち受けられるか。ssh -L と同じく、直前の接続の後始末 (TIME_WAIT) が残っていても使えるとみなす。"""
    with socket.socket() as sock:
        if os.name != "nt":   # Windows の SO_REUSEADDR は使用中のポートも奪えてしまうので付けない
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def cmd_login(args):
    """VM の中のエージェントに、手元のブラウザでログインする。"""
    ssh = ssh_binary(args.name)
    agent = args.agent or {"claude": "claude", "codex": "codex"}.get(load_session(args.name)["payload"].get("agent"))
    if not agent:
        die("ログインするエージェント (claude か codex) を指定してください。例: mvm login " + args.name + " codex")
    host = f"agent@pmva-{args.name}"
    if agent == "claude":
        # Claude Code は URL を表示し、ブラウザに表示されたコードを貼り付ける方式 (端末が要る)
        log("表示される URL を手元のブラウザで開き、ログイン後に表示されるコードを貼り付けてください")
        rc = subprocess.call([ssh, *ssh_options(args.name), "-t", host,
                              VM_SHELL + "exec claude auth login" + (" --console" if args.console else "")])
        if rc == 0:
            log("ログインしました。claude のウィンドウで動いている Claude Code は、起動し直すと新しいログインを使います")
        return rc
    forwards = []
    if args.device:
        remote = "exec codex login --device-auth"
    else:
        # codex login は VM の中の 127.0.0.1:1455 で、ブラウザからの戻り (ログイン結果) を待つ。
        # 手元の同じポートを VM につなぐと、手元のブラウザでログインした結果が VM の Codex に届く
        if not port_free(CODEX_LOGIN_PORTS[0]):
            die(f"手元のポート {CODEX_LOGIN_PORTS[0]} が使われています (手元で codex login を実行中など)。"
                "終わらせてからやり直すか、--device (コードを入力する方式) を使ってください")
        forwards = ["-o", "ExitOnForwardFailure=yes"]
        for port in CODEX_LOGIN_PORTS:
            if port_free(port):
                forwards += ["-L", f"127.0.0.1:{port}:127.0.0.1:{port}"]
        remote = "exec codex login"
    log(f"{args.name} の Codex にログインします (中止は Ctrl-C)")
    proc = subprocess.Popen([ssh, *ssh_options(args.name), *forwards, host, VM_SHELL + remote],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    opened = args.no_browser
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace")
            sys.stdout.write(line)
            sys.stdout.flush()
            m = LOGIN_URL.search(line)
            if m and not opened:
                opened = True
                if webbrowser.open(m.group(0)):
                    log("手元のブラウザで開きました。開かないときは上の URL をブラウザに貼り付けてください")
        rc = proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 130
    if rc == 0:
        log("ログインしました。codex のウィンドウでログインを待っている Codex は、終了して codex を起動し直してください "
            "(mvm ssh で接続)")
    return rc


def attach_tmux(name, remote, no_reconnect, extra=None):
    ssh = ssh_binary(name)
    host = f"agent@pmva-{name}"
    # tmux へのアタッチは、接続が切れたら (ssh の終了コード 255) 自動でつなぎ直す。
    # 途中の通信経路 (プロキシなど) が長時間の接続を切ることがある。作業は tmux の中で続いている
    failures = 0
    while True:
        started = time.time()
        command = [ssh, *ssh_options(name), *(extra or []), host] if remote is None else \
            [ssh, *ssh_options(name), "-t", host, *remote]
        rc = subprocess.call(command)
        if rc != 255 or no_reconnect:
            return rc
        failures = 1 if time.time() - started > 60 else failures + 1
        if failures > 5:
            die("接続できません。mvm ls で状態を確認してください")
        log(f"接続が切れました。{2 * failures} 秒後に再接続します (Ctrl-C で中止)")
        try:
            time.sleep(2 * failures)
        except KeyboardInterrupt:
            return rc


def cmd_ssh_proxy(args):
    """ssh の ProxyCommand から呼ばれる: 標準入出力を MicroVM 内の SSH ブリッジ (WebSocket) につなぐ。"""
    s = load_session(args.name)
    token = auth_token(args.name, SSH_BRIDGE_PORT)
    try:
        host, port, tls = ws_target(s["endpoint"])
        ws = WebSocket(host, "/", ws_protocols(token, SSH_BRIDGE_PORT), port=port, tls=tls)
    except OSError as e:
        die(f"SSH ブリッジに接続できません: {e}")
    set_binary_stdio()
    pump(ws, lambda: os.read(0, 65536))
    return 0


SSH_CONFIG_BEGIN = "# >>> pmva (generated by mvm ssh-config) >>>"
SSH_CONFIG_END = "# <<< pmva <<<"


def render_ssh_config():
    lines = [SSH_CONFIG_BEGIN]
    for name in session_names():
        if load_session(name).get("terminated"):
            continue
        lines += [
            f"Host pmva-{name}",
            f"  HostName pmva-{name}",
            "  User agent",
            f"  ProxyCommand {proxy_command(name)}",
            "  StrictHostKeyChecking no",
            f"  UserKnownHostsFile {os.devnull}",
            "  LogLevel ERROR",
            "  ServerAliveInterval 30",
            "  ServerAliveCountMax 4",
            "",
        ]
    lines.append(SSH_CONFIG_END)
    return "\n".join(lines) + "\n"


def cmd_ssh_config(args):
    block = render_ssh_config()
    if not args.install:
        print(block, end="")
        return 0
    # ~/.ssh/config の pmva ブロックだけを置き換える。Windows の OpenSSH も読めるよう UTF-8 (BOM なし) で書く
    path = Path.home() / ".ssh" / "config"
    path.parent.mkdir(mode=0o700, exist_ok=True)
    current = path.read_text(encoding="utf-8-sig") if path.exists() else ""
    if SSH_CONFIG_BEGIN in current and SSH_CONFIG_END in current:
        head, rest = current.split(SSH_CONFIG_BEGIN, 1)
        tail = rest.split(SSH_CONFIG_END, 1)[1].lstrip("\r\n")
        updated = head + block + tail
    else:
        updated = current + ("\n" if current and not current.endswith("\n") else "") + block
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(updated)
    log(f"{path} を更新しました (Host pmva-<name>)")
    return 0


def cmd_token(args):
    s = load_session(args.name)
    print(f"endpoint: https://{s['endpoint']}")
    print(f"port:     {args.port}")
    print(f"token:    {auth_token(args.name, args.port)}")
    return 0


def cmd_logs(args):
    """CloudWatch Logs を表示する (AWS CLI v1 には logs tail が無いので filter-log-events で実装)。"""
    group = stack_output("LogGroupName")
    start = int((time.time() - (600 if args.follow else 3600)) * 1000)
    seen = set()
    try:
        while True:
            token = None
            while True:
                extra = ["--next-token", token] if token else []
                r = aws("logs", "filter-log-events", "--log-group-name", group,
                        "--start-time", str(start), *extra, check=False) or {}
                for e in r.get("events", []):
                    if e["eventId"] in seen:
                        continue
                    seen.add(e["eventId"])
                    ts = time.strftime("%H:%M:%S", time.localtime(e["timestamp"] / 1000))
                    print(f"{ts} {e.get('logStreamName', '')} {e['message'].rstrip()}", flush=True)
                    start = max(start, e["timestamp"])
                token = r.get("nextToken")
                if not token:
                    break
            if not args.follow:
                return 0
            time.sleep(5)
    except KeyboardInterrupt:
        return 0


# ---------------------------------------------------------------- 引数

def add_up_options(p):
    p.add_argument("--agent", choices=AGENTS)
    p.add_argument("--claude-auth", choices=CLAUDE_AUTHS)
    p.add_argument("--codex-auth", choices=CODEX_AUTHS)
    p.add_argument("--claude-secret", help="Secrets Manager のシークレット名 (既定は認証方式から決まる)")
    p.add_argument("--codex-secret")
    p.add_argument("--github-secret")
    p.add_argument("--no-github", action="store_true", help="GitHub トークンを使わない")
    p.add_argument("--repo", help="clone する git リポジトリ URL")
    p.add_argument("--branch", help="作業ブランチ (既定 pmva/<name>)")
    p.add_argument("--base-branch", help="作業ブランチの起点")
    p.add_argument("--prompt", help="起動時にエージェントへ渡す最初の指示")
    p.add_argument("--restore", help="latest または s3://... のワークスペーススナップショットから復元")
    p.add_argument("--claude-args", help='claude の追加引数 (例: "--dangerously-skip-permissions")')
    p.add_argument("--codex-args", help="codex の追加引数")
    p.add_argument("--model", help="Claude のモデル / Bedrock の推論プロファイル ID")
    p.add_argument("--bedrock-region")
    p.add_argument("--idle", help="アイドルでサスペンドするまでの秒数。off で無効")
    p.add_argument("--max-duration", help="最大寿命 (秒、上限 28800)")
    p.add_argument("--no-setup", action="store_true", help="リポジトリの .pmva/setup.sh を実行しない")
    p.add_argument("--profile", help="手元から送った Claude Code / Codex の設定のまとまり (mvm profile push) を使う")
    p.add_argument("--no-profile", action="store_true", help="config.env の PROFILE を使わない")
    p.add_argument("--notify", dest="notify", action="store_true", default=None,
                   help="完了・入力待ちを <prefix>/notify-webhook の URL に通知する")
    p.add_argument("--no-notify", dest="notify", action="store_false", help="通知しない")


def build_parser():
    p = argparse.ArgumentParser(prog="mvm", description="Lambda MicroVMs で Claude Code / Codex を並列実行する")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("doctor", help="前提ツール・認証・リージョン対応を確認").set_defaults(func=cmd_doctor)
    x = sub.add_parser("infra", help="CloudFormation で S3 / IAM ロール / ログを作成・確認・削除")
    x.add_argument("action", choices=("deploy", "outputs", "delete"))
    x.set_defaults(func=cmd_infra)
    x = sub.add_parser("secret", help="認証情報を Secrets Manager に登録 (set) / 一覧 (ls)")
    x.add_argument("action", choices=("set", "ls"))
    x.add_argument("kind", nargs="?", help=" | ".join(SECRET_KINDS))
    x.add_argument("--name", help="シークレット名 (既定 <prefix>/<kind>)")
    x.add_argument("--file", help="値をファイルから読む (省略時は入力 / 標準入力)")
    x.set_defaults(func=cmd_secret)
    x = sub.add_parser("image", help="エージェント実行イメージをビルド / 状態確認 / ベースイメージ一覧 / 削除 / "
                                     "Claude Code・Codex の版の確認 (versions) と更新 (update-versions)")
    x.add_argument("action", choices=("build", "status", "base", "delete", "versions", "update-versions"))
    x.add_argument("--yes", action="store_true", help="delete の確認を省略する")
    x.add_argument("--claude", help="update-versions で使う Claude Code の版 (省略時は npm の最新)")
    x.add_argument("--codex", help="update-versions で使う Codex の版 (省略時は npm の最新)")
    x.set_defaults(func=cmd_image)

    x = sub.add_parser("up", help="MicroVM を 1 台起動してエージェントを開始")
    x.add_argument("name")
    add_up_options(x)
    x.set_defaults(func=cmd_up)
    x = sub.add_parser("fleet", help="<prefix>-01..NN をまとめて起動 (MicroVM メモリのクォータの範囲内で)")
    x.add_argument("count", type=int)
    x.add_argument("--prefix", default="agent")
    x.add_argument("--prompts-file", help="1 行に 1 つ、各エージェントへの最初の指示")
    add_up_options(x)
    x.set_defaults(func=cmd_fleet)
    x = sub.add_parser("ls", help="セッション一覧")
    x.add_argument("--all", action="store_true", help="終了済みも表示")
    x.add_argument("--json", action="store_true", help="JSON で出力")
    x.set_defaults(func=cmd_ls)
    x = sub.add_parser("stop", help="保存してから終了")
    x.add_argument("name")
    x.add_argument("--no-save", action="store_true")
    x.set_defaults(func=cmd_stop)
    x = sub.add_parser("stop-all", help="全セッションを保存してから終了")
    x.add_argument("--no-save", action="store_true")
    x.set_defaults(func=cmd_stop_all)
    for name, func, text in (("suspend", cmd_suspend, "サスペンド"), ("resume", cmd_resume, "レジューム"),
                             ("save", cmd_save, "ワークスペースを S3 に保存"),
                             ("renew", cmd_renew, "保存 → 終了 → 同じ設定で再起動して復元 (8 時間制限の延長)")):
        x = sub.add_parser(name, help=text)
        x.add_argument("name")
        x.set_defaults(func=func)

    x = sub.add_parser("peek", help="エージェント画面の末尾を表示 (接続せずに確認)")
    x.add_argument("name")
    x.add_argument("lines", nargs="?", type=int, default=30)
    x.add_argument("--json", action="store_true", help="状態 API の応答を JSON のまま出力")
    x.set_defaults(func=cmd_peek)
    x = sub.add_parser("wait", help="エージェントの応答が終わるか、許可・入力待ちになるまで待つ")
    x.add_argument("names", nargs="*", metavar="name", help="待つセッション (省略時は動いている全セッション)")
    x.add_argument("--timeout", type=float, default=0, help="最大の待ち時間 (秒)。過ぎたら終了コード 2 で返る。0 で無制限 (既定)")
    x.add_argument("--any", action="store_true", help="どれか 1 台が終わったら返る (既定は全台)")
    x.add_argument("--idle", type=int, default=300,
                   help="画面も CPU もこの秒数動かなければ、止まっている (stalled) とみなして返る。0 で無効 (既定 300)")
    x.add_argument("--interval", type=float, default=15, help="状態を確かめる間隔 (秒、既定 15)")
    x.add_argument("--json", action="store_true", help="JSON で出力")
    x.set_defaults(func=cmd_wait)
    x = sub.add_parser("follow", help="エージェントの会話の記録 (指示・思考・ツールの実行と結果・応答) を流れるように表示")
    x.add_argument("names", nargs="*", metavar="name", help="表示するセッション (省略時は動いている全セッション)")
    x.add_argument("--agent", choices=("claude", "codex"), help="--agent both で起動したときに片方だけ表示する")
    x.add_argument("--lines", type=int, default=20, help="最初に表示する、直前の出来事の数 (既定 20)")
    x.add_argument("--chars", type=int, default=500, help="1 件あたりの最大の文字数 (既定 500)")
    x.add_argument("--full", action="store_true", help="省略せずに全文を表示する")
    x.add_argument("--interval", type=float, default=2, help="新しい出来事を確かめる間隔 (秒、既定 2)")
    x.add_argument("--once", action="store_true", help="直前の出来事を表示して終わる")
    x.add_argument("--json", action="store_true", help="1 行に 1 件の JSON で出力")
    x.set_defaults(func=cmd_follow)
    x = sub.add_parser("forward", help="VM の中のポート (開発サーバーなど) を手元の PC のポートにつなぐ")
    x.add_argument("name")
    x.add_argument("ports", nargs="+", metavar="port", help="VM のポート (例: 3000)。手元のポートを変えるときは 8080:3000")
    x.add_argument("--no-reconnect", action="store_true", help="接続が切れても再接続しない")
    x.set_defaults(func=cmd_forward)
    x = sub.add_parser("login", help="VM の中のエージェントに、手元のブラウザでログインする")
    x.add_argument("name")
    x.add_argument("agent", nargs="?", choices=("claude", "codex"), help="省略時はセッションのエージェント")
    x.add_argument("--device", action="store_true", help="Codex: コードを入力する方式 (codex login --device-auth)")
    x.add_argument("--no-browser", action="store_true", help="Codex: 手元のブラウザを自動で開かない")
    x.add_argument("--console", action="store_true", help="Claude Code: Anthropic Console (API の従量課金) でログイン")
    x.set_defaults(func=cmd_login)
    x = sub.add_parser("view", help="エージェントの画面を読み取り専用で表示 (入力はエージェントに届かない)")
    x.add_argument("name")
    x.add_argument("--window", help="表示するウィンドウ (claude / codex / shell)")
    x.add_argument("--no-reconnect", action="store_true", help="接続が切れても再接続しない")
    x.set_defaults(func=cmd_view)
    x = sub.add_parser("top", help="VM のメモリの使用量と、多く使っているプロセスを表示")
    x.add_argument("names", nargs="*", metavar="name", help="表示するセッション (省略時は動いている全セッション)")
    x.add_argument("--json", action="store_true", help="JSON で出力")
    x.set_defaults(func=cmd_top)
    x = sub.add_parser("profile", help="手元の Claude Code / Codex のルール・skill・サブエージェント・コマンドを VM に渡す")
    x.add_argument("action", choices=("push", "show"))
    x.add_argument("--name", default="default", help="設定のまとまりの名前 (既定 default)")
    x.add_argument("--agent", choices=("claude", "codex", "both"), default="both")
    x.add_argument("--dry-run", action="store_true", help="push で、送るものを表示するだけにする")
    x.set_defaults(func=cmd_profile)
    x = sub.add_parser("orchestrator", help="手元の Claude Code / Codex を司令塔にする (MicroVM に作業を任せる skill と設定)")
    x.add_argument("action", choices=("install", "uninstall"))
    x.add_argument("agent", choices=("claude", "codex"))
    x.add_argument("--dir", help="このフォルダで起動したエージェントだけを司令塔にする (重い処理を止める設定も入れる)")
    x.set_defaults(func=cmd_orchestrator)
    x = sub.add_parser("result", help="エージェントの最後の応答を表示")
    x.add_argument("name")
    x.add_argument("--agent", choices=("claude", "codex"), help="--agent both で起動したときに片方だけ表示する")
    x.add_argument("--json", action="store_true", help="JSON で出力")
    x.set_defaults(func=cmd_result)
    x = sub.add_parser("send", help="エージェントに指示を送る (tmux に入力)")
    x.add_argument("name")
    x.add_argument("--window", help="送信先の tmux ウィンドウ (claude / codex / shell)")
    x.add_argument("--no-enter", action="store_true")
    # "-" で始まる指示は "--" の後に書く (例: mvm send a1 -- -h)
    x.add_argument("text", nargs="+")
    x.set_defaults(func=cmd_send)
    x = sub.add_parser("watch", help="全セッションの状態を定期表示")
    x.add_argument("interval", nargs="?", type=int, default=60)
    x.set_defaults(func=cmd_watch)
    x = sub.add_parser("ssh", help="SSH over WebSocket で接続 (既定で tmux にアタッチ。切れたら自動で再接続)")
    x.add_argument("name")
    x.add_argument("--no-reconnect", action="store_true", help="接続が切れても再接続しない")
    x.add_argument("remote_command", nargs=argparse.REMAINDER, metavar="command")
    x.set_defaults(func=cmd_ssh)
    x = sub.add_parser("_ssh-proxy")
    x.add_argument("name")
    x.set_defaults(func=cmd_ssh_proxy)
    x = sub.add_parser("ssh-config", help="~/.ssh/config 用の設定を出力 (VS Code Remote-SSH 等)")
    x.add_argument("--install", action="store_true", help="~/.ssh/config の pmva ブロックを書き換える")
    x.set_defaults(func=cmd_ssh_config)
    x = sub.add_parser("shell", help="AWS 標準のシェル接続 (SHELL_INGRESS)")
    x.add_argument("name")
    x.set_defaults(func=cmd_shell)
    x = sub.add_parser("token", help="エンドポイントと認証トークンを表示")
    x.add_argument("name")
    x.add_argument("port", nargs="?", type=int, default=API_PORT)
    x.set_defaults(func=cmd_token)
    x = sub.add_parser("logs", help="CloudWatch Logs を表示")
    x.add_argument("--follow", action="store_true")
    x.set_defaults(func=cmd_logs)
    x = sub.add_parser("cost", help="コスト試算 (scripts/estimate_cost.py の引数をそのまま渡す)。"
                                    "--actual [--days N] [--daily] で Cost Explorer の実績を表示", add_help=False)
    x.add_argument("rest", nargs=argparse.REMAINDER)
    x.set_defaults(func=None)
    return p


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    # "--claude-args --dangerously-skip-permissions" のように "-" で始まる値も受け付ける
    fixed, it = [], iter(sys.argv[1:] if argv is None else argv)
    for a in it:
        fixed.append(f"{a}={next(it, '')}" if a in ("--claude-args", "--codex-args") else a)
    argv = fixed
    parser = build_parser()
    if argv[:1] == ["cost"] and "--actual" in argv:
        cp = argparse.ArgumentParser(prog="mvm cost --actual", description="Cost Explorer の実績をサービス別に表示")
        cp.add_argument("--actual", action="store_true")
        cp.add_argument("--days", type=int, default=7, help="さかのぼる日数 (既定 7、今日を含む)")
        cp.add_argument("--daily", action="store_true", help="日ごとの合計も表示")
        load_config()
        return cmd_cost_actual(cp.parse_args(argv[1:]))
    if argv[:1] == ["cost"]:
        return subprocess.call([sys.executable, str(ROOT / "scripts" / "estimate_cost.py"), *argv[1:]])
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    if args.command == "secret" and args.action == "set" and not args.kind:
        parser.error("secret set には kind が必要です")
    if args.command != "orchestrator":   # 手元のエージェントの設定だけなので、config.env がなくても使える
        load_config()
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
