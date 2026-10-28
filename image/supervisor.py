#!/usr/bin/env python3
"""MicroVM 内の常駐プロセス。

- :9000 Lambda MicroVMs のライフサイクルフック (/ready /validate /run /resume /suspend /terminate)
- :8080 ローカル PC の mvm から呼ばれる状態/操作 API
    GET  /healthz
    GET  /status?lines=N   セッション情報と各エージェント画面の末尾 N 行
    POST /send             {"window": "claude", "text": "...", "enter": true} を tmux に送る
    POST /snapshot         ワークスペースを S3 に保存 (同期)

8080 へのアクセスは MicroVM のプロキシで IAM 由来の認証トークンが必須なので、ここでは認証しない。
外部ライブラリに依存しないよう標準ライブラリのみで実装している。
"""

import json
import os
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOME = os.path.expanduser("~")
PMVA_DIR = os.path.join(HOME, ".pmva")
SESSION_FILE = os.path.join(PMVA_DIR, "session.json")
STATE_FILE = os.path.join(PMVA_DIR, "state")
LAST_SNAPSHOT_FILE = os.path.join(PMVA_DIR, "last_snapshot")
BOOTSTRAP_LOG = os.path.join(PMVA_DIR, "bootstrap.log")
BOOTSTRAP = "/opt/pmva/bootstrap.sh"
SNAPSHOT = "/opt/pmva/snapshot.sh"
HOOK_PREFIX = "/aws/lambda-microvms/runtime/v1/"
TMUX_SESSION = "main"
HOOK_PORT = 9000
API_PORT = 8080

snapshot_lock = threading.Lock()


def log(msg):
    print(f"[pmva] {time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())} {msg}", flush=True)


def read_text(path, default=""):
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return default


def load_session():
    try:
        with open(SESSION_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def run(cmd, timeout=10):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def start_bootstrap():
    os.makedirs(PMVA_DIR, mode=0o700, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write("pending\n")
    logf = open(BOOTSTRAP_LOG, "ab")
    subprocess.Popen([BOOTSTRAP], stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    log("bootstrap started")


def snapshot(reason, timeout):
    """ワークスペースを S3 に保存する。同時実行はしない。"""
    if not snapshot_lock.acquire(timeout=timeout):
        return False, "another snapshot is running"
    try:
        rc, out = run([SNAPSHOT, reason], timeout=timeout)
        log(f"snapshot ({reason}) rc={rc}")
        return rc == 0, out.strip()[-2000:]
    finally:
        snapshot_lock.release()


def periodic_snapshot_loop():
    while True:
        interval = int(load_session().get("snapshotIntervalSeconds") or 0)
        if interval <= 0:
            time.sleep(60)
            continue
        time.sleep(interval)
        if read_text(STATE_FILE).startswith("ready"):
            snapshot("periodic", timeout=900)


def tmux_windows():
    rc, out = run(["tmux", "list-windows", "-t", TMUX_SESSION, "-F", "#{window_name}\t#{pane_current_command}"])
    if rc != 0:
        return []
    return [dict(zip(("name", "command"), line.split("\t", 1))) for line in out.splitlines() if line]


def capture(window, lines):
    rc, out = run(["tmux", "capture-pane", "-p", "-J", "-t", f"{TMUX_SESSION}:{window}", "-S", f"-{lines}"])
    if rc != 0:
        return ""
    return "\n".join(out.rstrip("\n").splitlines()[-lines:])


def status(lines):
    s = load_session()
    windows = tmux_windows()
    for w in windows:
        if w["name"] != "shell" and lines > 0:
            w["screen"] = capture(w["name"], lines)
    return {
        "name": s.get("name"),
        "agent": s.get("agent"),
        "claudeAuth": s.get("claudeAuth"),
        "codexAuth": s.get("codexAuth"),
        "repo": s.get("repo"),
        "branch": s.get("branch"),
        "bootstrap": read_text(STATE_FILE, "not-started"),
        "lastSnapshot": read_text(LAST_SNAPSHOT_FILE) or None,
        "uptimeSeconds": int(float(read_text("/proc/uptime", "0").split()[0])),
        "windows": windows,
    }


def send_keys(window, text, enter):
    target = f"{TMUX_SESSION}:{window}"
    if text:
        rc, out = run(["tmux", "send-keys", "-t", target, "-l", text])
        if rc != 0:
            return False, out
    if enter:
        # TUI 側で貼り付けと判定されないよう少し待ってから Enter を送る
        time.sleep(0.3)
        rc, out = run(["tmux", "send-keys", "-t", target, "Enter"])
        if rc != 0:
            return False, out
    return True, ""


class Handler(BaseHTTPRequestHandler):
    server_version = "pmva"

    def log_message(self, fmt, *args):
        pass

    def reply(self, code, body=None):
        data = json.dumps(body if body is not None else {}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def body(self):
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(length) if length else b""


class HookHandler(Handler):
    def do_POST(self):
        path = urlparse(self.path).path
        if not path.startswith(HOOK_PREFIX):
            return self.reply(404, {"error": "not found"})
        hook = path[len(HOOK_PREFIX):]
        payload = self.body()
        log(f"hook {hook}")

        if hook in ("ready", "resume", "suspend"):
            return self.reply(200)
        if hook == "validate":
            # スナップショットからの起動を検証しつつ、実行時に読むページをプリフェッチ対象にする
            ok = all(run([c, "--version"], timeout=20)[0] == 0 for c in ("claude", "codex", "tmux"))
            return self.reply(200 if ok else 500)
        if hook == "run":
            try:
                session = json.loads(payload or b"{}")
            except ValueError:
                return self.reply(400, {"error": "runHookPayload must be JSON"})
            os.makedirs(PMVA_DIR, mode=0o700, exist_ok=True)
            tmp = SESSION_FILE + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(session, f)
            os.replace(tmp, SESSION_FILE)
            start_bootstrap()
            return self.reply(200)
        if hook == "terminate":
            # terminate フックのタイムアウト (最大 60 秒) 内でベストエフォート保存
            snapshot("terminate", timeout=50)
            return self.reply(200)
        return self.reply(404, {"error": f"unknown hook {hook}"})


class ApiHandler(Handler):
    def do_GET(self):
        url = urlparse(self.path)
        if url.path == "/healthz":
            return self.reply(200, {"ok": True})
        if url.path == "/status":
            lines = int(parse_qs(url.query).get("lines", ["20"])[0])
            return self.reply(200, status(max(0, min(lines, 500))))
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/send":
            try:
                req = json.loads(self.body() or b"{}")
            except ValueError:
                return self.reply(400, {"error": "invalid json"})
            ok, err = send_keys(req.get("window") or "claude", req.get("text") or "", bool(req.get("enter", True)))
            return self.reply(200 if ok else 500, {"ok": ok, "error": err or None})
        if url.path == "/snapshot":
            ok, out = snapshot("manual", timeout=900)
            return self.reply(200 if ok else 500, {"ok": ok, "output": out})
        return self.reply(404, {"error": "not found"})


def serve(port, handler):
    ThreadingHTTPServer(("0.0.0.0", port), handler).serve_forever()


def main():
    os.makedirs(PMVA_DIR, mode=0o700, exist_ok=True)
    threading.Thread(target=serve, args=(HOOK_PORT, HookHandler), daemon=True).start()
    threading.Thread(target=periodic_snapshot_loop, daemon=True).start()
    log("supervisor started")
    serve(API_PORT, ApiHandler)


if __name__ == "__main__":
    main()
