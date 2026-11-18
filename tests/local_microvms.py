"""Lambda MicroVMs の代わりを、手元の Docker で動かす (tests/test_local_vm.py が使う)。

本物のイメージ (image/Dockerfile) のコンテナを MicroVM の代わりにし、mvm を AWS なしで通しで動かす。

- tests/aws_stub.py は、環境変数 PMVA_STUB_DOCKER_IMAGE があると lambda-microvms の操作 (起動・状態・
  サスペンド・レジューム・終了) をここに任せる。起動ではコンテナを作り、MicroVM と同じ順に
  validate → run フックを送る。終了では terminate フックを送ってからコンテナを消す
- FakeIngress は Lambda MicroVMs の受け口の代わり。トークン (X-aws-proxy-auth、または WebSocket の
  サブプロトコル) とポートを確かめ、そのコンテナのポートにつなぐ (HTTP も WebSocket もそのまま中継する)
- VM の中では aws の代わり (CONTAINER_AWS) が、スタブのディレクトリ (/opt/pmva-test に見せる) の
  secret-<名前> と s3/<バケット>/<キー> を読み書きする。mvm secret set で登録した値がそのまま VM に届く
"""

import json
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

HOOK = "/aws/lambda-microvms/runtime/v1/"
VM_PORTS = (8080, 9000, 7681)   # 状態 API、ライフサイクルフック、SSH over WebSocket ブリッジ
HANDLED = ("lambda-microvms run-microvm", "lambda-microvms get-microvm", "lambda-microvms suspend-microvm",
           "lambda-microvms resume-microvm", "lambda-microvms terminate-microvm")

CONTAINER_AWS = r"""#!/bin/bash
T=/opt/pmva-test
s3path() { echo "$T/s3/${1#s3://}"; }
case "$1 $2" in
  "secretsmanager get-secret-value")
    shift 2; id=""
    while [ $# -gt 0 ]; do [ "$1" = --secret-id ] && id="$2"; shift; done
    f="$T/secret-${id//\//_}"
    [ -f "$f" ] || { echo "ResourceNotFoundException: $id" >&2; exit 254; }
    cat "$f" ;;
  "s3 cp")
    args=(); for a in "${@:3}"; do [[ "$a" == --* ]] || args+=("$a"); done
    src="${args[0]}"; dst="${args[1]}"
    if [[ "$src" == s3://* ]]; then
      [ -f "$(s3path "$src")" ] || { echo "fatal error: (404) Not Found: $src" >&2; exit 1; }
      if [ "$dst" = - ]; then cat "$(s3path "$src")"; else cp "$(s3path "$src")" "$dst"; fi
    else
      # テストを動かす利用者 (CI では root ではない) も見られるよう、途中のフォルダも誰でも辿れる権限で作る
      out="$(s3path "$dst")"; (umask 000; mkdir -p "$(dirname "$out")")
      if [ "$src" = - ]; then cat > "$out"; else cp "$src" "$out"; fi
      chmod 644 "$out"
    fi ;;
  *) echo "fake aws in VM: $*" >&2; exit 1 ;;
esac
"""


def docker(*args, check=True, timeout=300):
    p = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        raise RuntimeError(f"docker {' '.join(args[:2])} failed: {p.stderr.strip()[-2000:]}")
    return p


def request(port, method, path, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def vm_file(d, vid):
    return Path(d) / f"vm-{vid}.json"


def load_vm(d, vid):
    try:
        return json.loads(vm_file(d, vid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def run_microvm(d, payload, args):
    d = Path(d)
    vid = "microvm-%04d" % len(list(d.glob("microvm-*.payload")))
    (d / f"{vid}.payload").write_text(payload, encoding="utf-8")
    (d / f"{vid}.args").write_text(json.dumps(args), encoding="utf-8")
    (d / "bin").mkdir(exist_ok=True)
    (d / "bin" / "aws").write_text(CONTAINER_AWS, encoding="utf-8")
    (d / "bin" / "aws").chmod(0o755)
    (d / "s3").mkdir(exist_ok=True)
    for p in [d, d / "bin", d / "s3", *d.glob("secret-*")]:
        p.chmod(0o777 if p.is_dir() else 0o644)   # VM の中の agent (uid 1000) が読み書きできるように

    extra = os.environ.get("PMVA_TEST_DOCKER_ARGS", "").split()
    host_network = "host" in extra
    name = f"pmva-local-{vid}-{os.getpid()}-{int(time.time())}"
    publish = [] if host_network else [x for port in VM_PORTS for x in ("-p", f"127.0.0.1::{port}")]
    path = "/opt/pmva-test/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    docker("run", "-d", "--name", name, *extra, *publish, "-v", f"{d}:/opt/pmva-test", "-e", f"PATH={path}",
           os.environ["PMVA_STUB_DOCKER_IMAGE"])
    ports = {}
    for port in VM_PORTS:
        ports[str(port)] = port if host_network else \
            int(docker("port", name, str(port)).stdout.strip().splitlines()[0].rsplit(":", 1)[1])
    vm_file(d, vid).write_text(json.dumps({"container": name, "ports": ports}), encoding="utf-8")

    # MicroVM と同じ順: supervisor が起動 → validate フック → (テスト用の準備) → run フック
    deadline = time.time() + 120
    while time.time() < deadline:
        try:
            if request(ports["9000"], "POST", HOOK + "validate", {}) == 200:
                break
        except OSError:
            pass
        time.sleep(0.5)
    else:
        raise RuntimeError("validate フックが通らない: " + docker("logs", name, check=False).stdout[-2000:])
    if (d / "prerun.sh").exists():   # LLM API の代わりを VM の中で動かすなど
        docker("exec", "-d", name, "bash", "/opt/pmva-test/prerun.sh")
        for _ in range(60):
            if (d / "prerun.ready").exists():
                break
            time.sleep(0.5)
    code = request(ports["9000"], "POST", HOOK + "run", {"microvmId": vid, "runHookPayload": payload})
    if code != 200:
        raise RuntimeError(f"run フックが失敗: HTTP {code}")
    return {"microvmId": vid, "state": "RUNNING", "endpoint": f"https://{vid}.local.example",
            "imageVersion": "1.0", "startedAt": time.time()}


def vm_state(d, vid):
    vm = load_vm(d, vid)
    if not vm or (Path(d) / f"{vid}.term").exists():
        return "TERMINATED"
    p = docker("inspect", "-f", "{{.State.Running}} {{.State.Paused}}", vm["container"], check=False)
    if p.returncode != 0:
        return "TERMINATED"
    running, paused = p.stdout.split()
    return "SUSPENDED" if paused == "true" else ("RUNNING" if running == "true" else "TERMINATED")


def handle(cmd, opt, d):
    """aws_stub.py から呼ばれる。応答の JSON (dict) を返す。"""
    vid = opt("--microvm-identifier")
    vm = load_vm(d, vid) if vid else None
    if cmd == "lambda-microvms run-microvm":
        payload = opt("--run-hook-payload")
        if payload.startswith("file://"):
            payload = Path(payload[len("file://"):]).read_text(encoding="utf-8")
        import sys
        return run_microvm(d, payload, sys.argv[1:])
    if cmd == "lambda-microvms get-microvm":
        return {"microvmId": vid, "state": vm_state(d, vid)}
    if vm is None:
        raise RuntimeError(f"ResourceNotFoundException: {vid}")
    hooks = vm["ports"]["9000"]
    if cmd == "lambda-microvms suspend-microvm":
        request(hooks, "POST", HOOK + "suspend", {})
        docker("pause", vm["container"])
    elif cmd == "lambda-microvms resume-microvm":
        if vm_state(d, vid) == "SUSPENDED":
            docker("unpause", vm["container"])
            request(hooks, "POST", HOOK + "resume", {})
    elif cmd == "lambda-microvms terminate-microvm":
        try:   # terminate フック (最大 60 秒) で、supervisor がワークスペースを保存する
            request(hooks, "POST", HOOK + "terminate", {}, timeout=60)
        except OSError:
            pass
        docker("rm", "-f", vm["container"], check=False)
        (Path(d) / f"{vid}.term").touch()
    return {}


class FakeIngress:
    """Lambda MicroVMs の受け口の代わり。トークンとポートを確かめて、そのコンテナのポートにつなぐ。"""

    def __init__(self, stub_dir):
        self.stub_dir = Path(stub_dir)
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        self.rejected = []   # 認証で断った要求 (テストで確かめる)
        self.stopped = False
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def close(self):
        self.stopped = True
        self.sock.close()

    def _accept_loop(self):
        while not self.stopped:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        try:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = conn.recv(65536)
                if not chunk:
                    return conn.close()
                head += chunk
            head, rest = head.split(b"\r\n\r\n", 1)
            lines = head.decode("latin-1").split("\r\n")
            headers = [(l.split(":", 1)[0].strip(), l.split(":", 1)[1].strip()) for l in lines[1:] if ":" in l]
            token = port = None
            kept = []
            for k, v in headers:
                low = k.lower()
                if low == "x-aws-proxy-auth":
                    token = v
                elif low == "x-aws-proxy-port":
                    port = v
                elif low == "sec-websocket-protocol":
                    # lambda-microvms.authentication.<トークン>, lambda-microvms, lambda-microvms.port.<ポート>
                    for item in (x.strip() for x in v.split(",")):
                        if item.startswith("lambda-microvms.authentication."):
                            token = item[len("lambda-microvms.authentication."):]
                        elif item.startswith("lambda-microvms.port."):
                            port = item[len("lambda-microvms.port."):]
                else:
                    kept.append((k, v))
            vid = token[len("TOKEN-"):] if token and token.startswith("TOKEN-") else None
            vm = load_vm(self.stub_dir, vid) if vid else None
            if not vm or port not in vm["ports"] or vm_state(self.stub_dir, vid) != "RUNNING":
                self.rejected.append((lines[0], token, port))
                conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                return conn.close()
            upstream = socket.create_connection(("127.0.0.1", vm["ports"][port]), timeout=30)
            upstream.settimeout(None)
            conn.settimeout(None)
            new_head = "\r\n".join([lines[0]] + [f"{k}: {v}" for k, v in kept]) + "\r\n\r\n"
            upstream.sendall(new_head.encode("latin-1") + rest)
        except OSError:
            return conn.close()

        def pipe(src, dst):
            try:
                while True:
                    data = src.recv(65536)
                    if not data:
                        break
                    dst.sendall(data)
            except OSError:
                pass
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

        threading.Thread(target=pipe, args=(upstream, conn), daemon=True).start()
        pipe(conn, upstream)
        conn.close()
        upstream.close()
