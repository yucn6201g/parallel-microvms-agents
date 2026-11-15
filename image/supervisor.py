#!/usr/bin/env python3
"""MicroVM 内の常駐プロセス。

- :9000 Lambda MicroVMs のライフサイクルフック (/ready /validate /run /resume /suspend /terminate)
- :8080 ローカル PC の mvm から呼ばれる状態/操作 API
    GET  /healthz
    GET  /status?lines=N   セッション情報、エージェントの状態 (作業中・完了・入力待ちなど)、各画面の末尾 N 行
    GET  /result?agent=X   エージェント (claude / codex) の最後の応答の文章
    GET  /events?agent=X&file=F&offset=N&tail=N&max=N
                           会話記録 (指示・思考・ツールの実行と結果・応答) を、前回の続きから返す (mvm follow)
    POST /send             {"window": "claude", "text": "...", "enter": true} を tmux に送る
    POST /snapshot         ワークスペースを S3 に保存 (同期)

8080 へのアクセスは MicroVM のプロキシで IAM 由来の認証トークンが必須なので、ここでは認証しない。
外部ライブラリに依存しないよう標準ライブラリのみで実装している。
"""

import glob
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
AGENT_STATE_DIR = os.path.join(PMVA_DIR, "agents")   # notify.sh (エージェントのフック) と bootstrap.sh が書く
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
        return p.returncode, p.stdout if p.returncode == 0 else (p.stderr or p.stdout).strip()
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)


def start_bootstrap():
    os.makedirs(PMVA_DIR, mode=0o700, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write("pending\n")
    logf = open(BOOTSTRAP_LOG, "ab")
    subprocess.Popen([BOOTSTRAP], stdout=logf, stderr=subprocess.STDOUT, start_new_session=True)
    log("bootstrap started")


VALIDATE_COMMANDS = (["claude", "--version"], ["codex", "--version"], ["tmux", "-V"], ["git", "--version"])
validation = {"thread": None, "result": None}
validation_lock = threading.Lock()


def start_validation():
    """検証を一度だけ裏で実行する。実行中は None、完了後は成否を返す。"""
    with validation_lock:
        if validation["thread"] is None:
            def worker():
                ok = True
                for cmd in VALIDATE_COMMANDS:
                    rc, out = run(cmd, timeout=120)
                    log(f"validate {' '.join(cmd)}: rc={rc} {out.strip()[:200]}")
                    ok = ok and rc == 0
                validation["result"] = ok
            validation["thread"] = threading.Thread(target=worker, daemon=True)
            validation["thread"].start()
        return validation["result"]


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


ACTIVITY_INTERVAL = 30          # 作業中かどうかを調べる間隔 (秒)
PUBLISH_INTERVAL = 60           # 状態を S3 に書き出す間隔 (秒)
CPU_BUSY_TICKS = 100            # ACTIVITY_INTERVAL の間にこれ以上 CPU を使ったら作業中 (100 tick = 1 秒)
activity = {"last": time.time(), "cpu": None, "screens": None}


def pane_cpu_ticks():
    """tmux の各ペインで動くプロセス (エージェント、ビルド、テスト) が使った CPU 時間の合計。"""
    rc, out = run(["tmux", "list-panes", "-a", "-F", "#{pane_pid}"])
    roots = {int(x) for x in out.split() if x.isdigit()} if rc == 0 else set()
    procs = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", encoding="utf-8") as f:
                fields = f.read().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        # ppid と utime + stime + cutime + cstime (終了した子プロセスの分も含める)
        procs[int(d)] = (int(fields[1]), sum(int(x) for x in fields[11:15]))
    children = {}
    for pid, (ppid, _) in procs.items():
        children.setdefault(ppid, []).append(pid)
    total, stack, seen = 0, list(roots), set()
    while stack:
        pid = stack.pop()
        if pid in seen or pid not in procs:
            continue
        seen.add(pid)
        total += procs[pid][1]
        stack.extend(children.get(pid, []))
    return total


def check_activity():
    """画面が変わったか、ペイン内のプロセスが CPU を使っていれば作業中とみなす。"""
    screens = tuple(capture(w["name"], 50) for w in tmux_windows())
    cpu = pane_cpu_ticks()
    busy = (activity["screens"] is not None and screens != activity["screens"]) or \
           (activity["cpu"] is not None and cpu - activity["cpu"] >= CPU_BUSY_TICKS)
    activity["screens"], activity["cpu"] = screens, cpu
    if busy:
        activity["last"] = time.time()
    return busy


def publish_status():
    """最後に作業した時刻を S3 に書く。AWS 側の定期処理 (keepalive) が、作業中の VM だけを起こし続けるのに使う。"""
    s = load_session()
    if not (s.get("snapshotBucket") and s.get("name") and s.get("microvmId")):
        return
    body = json.dumps({"name": s["name"], "microvmId": s["microvmId"],
                       "lastActivity": activity["last"], "updatedAt": time.time()})
    env = dict(os.environ, AWS_REGION=s.get("region") or "", AWS_DEFAULT_REGION=s.get("region") or "")
    try:
        p = subprocess.run(["aws", "s3", "cp", "--only-show-errors", "-",
                            f"s3://{s['snapshotBucket']}/status/{s['name']}.json"],
                           input=body, text=True, capture_output=True, timeout=60, env=env)
        if p.returncode != 0:
            log(f"publish status failed: {p.stderr.strip()[:200]}")
    except (OSError, subprocess.TimeoutExpired) as e:
        log(f"publish status failed: {e}")


def activity_loop():
    last_publish = 0.0
    while True:
        time.sleep(ACTIVITY_INTERVAL)
        if not read_text(STATE_FILE).startswith("ready"):
            continue
        check_activity()
        check_oom()
        if time.time() - last_publish >= PUBLISH_INTERVAL:
            publish_status()
            last_publish = time.time()


# ---------------------------------------------------------------- エージェントの状態と最後の応答

AGENT_WINDOWS = {"claude": ["claude"], "codex": ["codex"], "both": ["claude", "codex"]}
SHELLS = {"bash", "sh", "zsh", "fish", "dash"}
TRANSCRIPT_TAIL_BYTES = 4 * 1024 * 1024
STATUS_MESSAGE_CHARS = 300


def read_agent_state(agent):
    try:
        with open(os.path.join(AGENT_STATE_DIR, f"{agent}.json"), encoding="utf-8") as f:
            st = json.load(f)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def write_agent_state(agent, state):
    os.makedirs(AGENT_STATE_DIR, mode=0o700, exist_ok=True)
    path = os.path.join(AGENT_STATE_DIR, f"{agent}.json")
    with open(path + ".tmp", "w", encoding="utf-8") as f:
        json.dump({"state": state, "at": time.time()}, f)
    os.replace(path + ".tmp", path)


def agent_states(session, windows):
    """エージェントごとの状態。

    starting  起動の準備中         working  作業中         done     応答が終わった
    waiting   許可・入力待ち       ready    最初の指示待ち  exited   エージェントが終了した (シェルに戻った)
    failed    起動の準備に失敗     unknown  記録がない
    """
    boot = read_text(STATE_FILE, "not-started")
    commands = {w["name"]: w.get("command") for w in windows}
    result = {}
    for agent in AGENT_WINDOWS.get(session.get("agent"), []):
        st = read_agent_state(agent)
        state = st.get("state") or "unknown"
        if boot.startswith("failed"):
            state = "failed"
        elif not boot.startswith("ready"):
            state = "starting"
        elif commands.get(agent) is None or commands.get(agent) in SHELLS:
            state = "exited"
        info = {"state": state, "since": st.get("at")}
        message = st.get("message")
        if isinstance(message, str) and message:
            info["message"] = message[:STATUS_MESSAGE_CHARS]
        result[agent] = info
    return result


def read_tail_lines(path, limit=TRANSCRIPT_TAIL_BYTES):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - limit))
        data = f.read()
    lines = data.decode("utf-8", "replace").splitlines()
    return lines[1:] if size > limit else lines  # 途中から読んだときは、最初の行は切れているので捨てる


def last_claude_text(transcript):
    """Claude Code の会話記録 (JSONL) から、最後のツール結果・指示より後の応答の文章を取り出す。"""
    texts = []
    for line in reversed(read_tail_lines(transcript)):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        if rec.get("type") == "assistant":
            content = (rec.get("message") or {}).get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            blocks = [b.get("text") or "" for b in content or [] if isinstance(b, dict) and b.get("type") == "text"]
            texts[:0] = [t for t in blocks if t.strip()]
        elif rec.get("type") == "user" and not rec.get("isMeta"):
            break
    return "\n\n".join(texts) or None


def newest(pattern):
    files = glob.glob(pattern, recursive=True)
    return max(files, key=os.path.getmtime) if files else None


def codex_transcript():
    return newest(os.path.join(HOME, ".codex", "sessions", "**", "rollout-*.jsonl"))


def last_codex_text(transcript):
    """Codex の会話記録から、最後に完了した応答 (task_complete の last_agent_message) を取り出す。"""
    for line in reversed(read_tail_lines(transcript)):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        payload = rec.get("payload") if isinstance(rec, dict) else None
        if rec.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "task_complete":
            return payload.get("last_agent_message") or None
    return None


def claude_transcript(state):
    path = state.get("transcript")
    if isinstance(path, str) and path.endswith(".jsonl") and os.path.isfile(path):
        return path
    return newest(os.path.join(HOME, ".claude", "projects", "*", "*.jsonl"))


def agent_result(agent):
    st = read_agent_state(agent)
    # Claude Code は Stop フックの last_assistant_message、Codex は notify (agent-turn-complete) の
    # last-assistant-message を notify.sh が記録している
    text = st.get("message") or None
    if not text:
        # フックが最後の応答を渡さない版や、notify の設定がないときは、会話記録から取り出す
        transcript = claude_transcript(st) if agent == "claude" else codex_transcript()
        if transcript:
            try:
                text = (last_claude_text if agent == "claude" else last_codex_text)(transcript)
            except OSError as e:
                log(f"read transcript failed: {e}")
    return {"agent": agent, "state": st.get("state") or "unknown", "since": st.get("at"), "text": text}


# ---------------------------------------------------------------- 会話記録の流れ (mvm follow)

EVENTS_READ_BYTES = 1024 * 1024     # 1 回に読む最大の大きさ
EVENTS_MAX_CHARS = 100000           # max=0 (省略しない) でも、1 件をこれより長くはしない
TOOL_INPUT_KEYS = ("command", "cmd", "file_path", "path", "pattern", "url", "query", "prompt", "description")


def clip(text, limit):
    text = text if isinstance(text, str) else json.dumps(text, ensure_ascii=False)
    limit = limit if limit > 0 else EVENTS_MAX_CHARS
    return (text[:limit], True) if len(text) > limit else (text, False)


def tool_summary(name, args):
    """ツールの入力を 1 行にまとめる (Bash ならコマンド、Read ならファイル名)。"""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args
    if isinstance(args, dict):
        for key in TOOL_INPUT_KEYS:
            value = args.get(key)
            if isinstance(value, list):
                value = " ".join(str(v) for v in value)
            if isinstance(value, str) and value:
                return value
    return json.dumps(args, ensure_ascii=False)


def block_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") in ("text", "input_text", "output_text"))
    return ""


def claude_events(rec):
    """Claude Code の会話記録 1 行を、表示用の出来事に直す。"""
    kind, msg = rec.get("type"), rec.get("message") or {}
    content = msg.get("content")
    out = []
    if kind == "user" and not rec.get("isMeta"):
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        for b in content or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip() and not b["text"].lstrip().startswith("<"):
                out.append({"kind": "prompt", "text": b["text"]})
            elif b.get("type") == "tool_result":
                out.append({"kind": "output", "text": block_text(b.get("content")), "error": bool(b.get("is_error"))})
    elif kind == "assistant":
        for b in content or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                out.append({"kind": "text", "text": b["text"]})
            elif b.get("type") == "thinking" and b.get("thinking", "").strip():
                out.append({"kind": "thinking", "text": b["thinking"]})
            elif b.get("type") == "tool_use":
                out.append({"kind": "tool", "name": b.get("name"), "text": tool_summary(b.get("name"), b.get("input"))})
    return out


def codex_output(text):
    # exec_command の結果は「Chunk ID / Wall time / ... / Output:」の後に本文が続く
    head, sep, body = text.partition("\nOutput:\n")
    return body if sep and "Wall time" in head else text


def codex_events(rec):
    """Codex の会話記録 (rollout) 1 行を、表示用の出来事に直す。"""
    p = rec.get("payload")
    if rec.get("type") != "response_item" or not isinstance(p, dict):
        return []
    kind = p.get("type")
    if kind == "message":
        text = block_text(p.get("content"))
        if p.get("role") == "assistant" and text.strip():
            return [{"kind": "text", "text": text}]
        if p.get("role") == "user" and text.strip() and not text.lstrip().startswith("<"):
            return [{"kind": "prompt", "text": text}]
    elif kind == "reasoning":
        text = "\n".join(x.get("text") or "" for x in p.get("summary") or [] if isinstance(x, dict))
        if text.strip():
            return [{"kind": "thinking", "text": text}]
    elif kind in ("function_call", "custom_tool_call"):
        return [{"kind": "tool", "name": p.get("name"), "text": tool_summary(p.get("name"), p.get("arguments", p.get("input")))}]
    elif kind == "local_shell_call":
        return [{"kind": "tool", "name": "shell", "text": tool_summary("shell", (p.get("action") or {}))}]
    elif kind == "web_search_call":
        return [{"kind": "tool", "name": "web_search", "text": tool_summary("web_search", p.get("action") or {})}]
    elif kind in ("function_call_output", "custom_tool_call_output"):
        output = p.get("output")
        if isinstance(output, dict):
            output = output.get("content") or json.dumps(output, ensure_ascii=False)
        return [{"kind": "output", "text": codex_output(output if isinstance(output, str) else block_text(output))}]
    return []


def transcript_path(agent):
    return claude_transcript(read_agent_state("claude")) if agent == "claude" else codex_transcript()


def parse_events(agent, lines, limit):
    parse = claude_events if agent == "claude" else codex_events
    events = []
    for line in lines:
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        for ev in parse(rec):
            ev["text"], truncated = clip(ev.get("text") or "", limit)
            if truncated:
                ev["truncated"] = True
            ev["ts"] = rec.get("timestamp")
            events.append(ev)
    return events


def agent_events(agent, file, offset, tail, limit):
    """会話記録の出来事を、前回の続き (file と offset) から返す。

    file が今の会話記録と違えば (初回、エージェントの再起動で新しい会話になったなど)、
    初回は末尾の tail 件、それ以外は新しい会話の最初から返す。
    """
    current = transcript_path(agent)
    if not current:
        return {"agent": agent, "file": None, "offset": 0, "events": []}
    size = os.path.getsize(current)
    if not file:
        lines = read_tail_lines(current, EVENTS_READ_BYTES)
        return {"agent": agent, "file": current, "offset": size, "events": parse_events(agent, lines, limit)[-tail:] if tail > 0 else []}
    reset = file != current or offset > size
    if reset:
        offset = 0
    with open(current, "rb") as f:
        f.seek(offset)
        data = f.read(EVENTS_READ_BYTES)
    # 書きかけの最後の行は次回に回す
    end = data.rfind(b"\n") + 1
    lines = data[:end].decode("utf-8", "replace").splitlines()
    return {"agent": agent, "file": current, "offset": offset + end, "reset": reset,
            "events": parse_events(agent, lines, limit)}


# ---------------------------------------------------------------- メモリ

def read_proc_status(pid):
    info = {}
    try:
        with open(f"/proc/{pid}/status", encoding="utf-8", errors="replace") as f:
            for line in f:
                key, _, value = line.partition(":")
                if key in ("Name", "VmRSS"):
                    info[key] = value.strip()
    except OSError:
        pass
    return info


def memory_info(top=6):
    """VM 全体のメモリと、プロセス名ごとの使用量 (RSS の合計。共有分を重複して数えるので目安)。"""
    mem = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(":")
                mem[key] = int(value.split()[0]) if value.split() else 0
    except (OSError, ValueError):
        return None
    groups = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        st = read_proc_status(d)
        rss = st.get("VmRSS", "").split()
        if not rss:
            continue  # カーネルのスレッド
        g = groups.setdefault(st.get("Name") or "?", {"name": st.get("Name") or "?", "count": 0, "rssMiB": 0.0})
        g["count"] += 1
        g["rssMiB"] += int(rss[0]) / 1024
    ranked = sorted(groups.values(), key=lambda g: -g["rssMiB"])[:top]
    for g in ranked:
        g["rssMiB"] = round(g["rssMiB"])
    total, available = mem.get("MemTotal", 0) // 1024, mem.get("MemAvailable", 0) // 1024
    return {"totalMiB": total, "availableMiB": available, "usedMiB": total - available, "top": ranked}


def oom_kill_count():
    """メモリ不足でカーネルがプロセスを強制終了した回数 (起動してからの合計)。"""
    try:
        with open("/proc/vmstat", encoding="utf-8") as f:
            for line in f:
                key, _, value = line.partition(" ")
                if key == "oom_kill":
                    return int(value)
    except (OSError, ValueError):
        pass
    return 0


# イメージのスナップショットを取った時点の回数を引き継ぐので、セッションの開始 (run フック) で基準を取り直す
oom = {"base": oom_kill_count(), "seen": 0}


def session_oom_kills():
    return max(0, oom_kill_count() - oom["base"])


def check_oom():
    kills = session_oom_kills()
    if kills > oom["seen"]:
        oom["seen"] = kills
        log(f"oom kill detected (total {kills} in this session)")
        run(["/opt/pmva/notify.sh", "oom", str(kills)], timeout=30)


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
        "idleSeconds": int(time.time() - activity["last"]),
        "agents": agent_states(s, windows),
        "memory": memory_info(),
        "oomKills": session_oom_kills(),
        "windows": windows,
    }


def send_keys(window, text, enter):
    target = f"{TMUX_SESSION}:{window}"
    if text:
        # "--" を付けないと、"-" で始まる指示を tmux がオプションとして解釈してしまう
        rc, out = run(["tmux", "send-keys", "-t", target, "-l", "--", text])
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
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            log(f"client disconnected before response ({self.path})")

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
            # スナップショットからの起動を検証しつつ、実行時に読むページをプリフェッチ対象にする。
            # 時間がかかるので裏で実行し、終わるまでは 503 (まだ準備中) を即座に返す
            result = start_validation()
            if result is None:
                return self.reply(503)
            return self.reply(200 if result else 500)
        if hook == "run":
            # 本文は {"microvmId": "...", "runHookPayload": "<RunMicrovm に渡した文字列>"} の形で届く
            try:
                body = json.loads(payload or b"{}")
                inner = body.get("runHookPayload")
                session = json.loads(inner) if isinstance(inner, str) and inner else dict(inner or {})
            except (ValueError, AttributeError, TypeError):
                return self.reply(400, {"error": "runHookPayload must be JSON"})
            session["microvmId"] = body.get("microvmId")
            # 作業判定はイメージのスナップショット時点の値を引き継いでいるので、セッション開始時にリセットする
            activity.update(last=time.time(), cpu=None, screens=None)
            oom.update(base=oom_kill_count(), seen=0)
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
            try:
                lines = int(parse_qs(url.query).get("lines", ["20"])[0])
            except ValueError:
                return self.reply(400, {"error": "lines must be an integer"})
            return self.reply(200, status(max(0, min(lines, 500))))
        if url.path == "/events":
            q = parse_qs(url.query)
            agent = q.get("agent", [""])[0]
            if agent not in ("claude", "codex"):
                return self.reply(400, {"error": "agent must be claude or codex"})
            try:
                offset, tail, limit = (int(q.get(k, [d])[0]) for k, d in (("offset", "0"), ("tail", "20"), ("max", "2000")))
            except ValueError:
                return self.reply(400, {"error": "offset, tail and max must be integers"})
            try:
                return self.reply(200, agent_events(agent, q.get("file", [""])[0], max(0, offset), max(0, min(tail, 500)), max(0, limit)))
            except OSError as e:
                return self.reply(500, {"error": str(e)})
        if url.path == "/result":
            agent = parse_qs(url.query).get("agent", [""])[0]
            if agent not in ("claude", "codex"):
                return self.reply(400, {"error": "agent must be claude or codex"})
            return self.reply(200, agent_result(agent))
        return self.reply(404, {"error": "not found"})

    def do_POST(self):
        url = urlparse(self.path)
        if url.path == "/send":
            try:
                req = json.loads(self.body() or b"{}")
            except ValueError:
                return self.reply(400, {"error": "invalid json"})
            window, text, enter = req.get("window") or "claude", req.get("text") or "", bool(req.get("enter", True))
            ok, err = send_keys(window, text, enter)
            if ok and text and enter and window in ("claude", "codex"):
                # 指示を送ったら作業中にする (Codex には指示を受け取ったときのフックがないため。mvm wait の判定に使う)
                write_agent_state(window, "working")
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
    threading.Thread(target=activity_loop, daemon=True).start()
    log("supervisor started")
    serve(API_PORT, ApiHandler)


if __name__ == "__main__":
    main()
