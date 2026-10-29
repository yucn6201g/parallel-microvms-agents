"""bin/mvm.py のテスト (Windows / macOS / Linux 共通、標準ライブラリのみ)。

AWS CLI はスタブ (tests/aws_stub.py) に差し替え、MicroVM 側の WebSocket / HTTP は
ローカルのテストサーバーで代用する。実行: python -m unittest discover -s tests -v
"""

import base64
import hashlib
import http.server
import importlib.util
import json
import os
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MVM = ROOT / "bin" / "mvm.py"
IS_WINDOWS = os.name == "nt"


def load_mvm():
    spec = importlib.util.spec_from_file_location("mvm", MVM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mvm = load_mvm()


# ---------------------------------------------------------------- テスト用サーバー

class EchoWebSocketServer:
    """受け取ったバイナリフレームをそのまま返す WebSocket サーバー (平文)。"""

    GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

    def __init__(self, ssl_context=None, flood=0, sockbuf=None):
        """flood: 接続直後に、相手から読む前にこのバイト数を送りつける。sockbuf: ソケットのバッファの大きさ。"""
        self.flood, self.sockbuf = flood, sockbuf
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.protocols = []
        self.paths = []
        self.ssl_context = ssl_context
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            if self.sockbuf:
                for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
                    conn.setsockopt(socket.SOL_SOCKET, opt, self.sockbuf)
            if self.ssl_context:
                try:
                    conn = self.ssl_context.wrap_socket(conn, server_side=True)
                except (OSError, ssl.SSLError):
                    continue
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    @staticmethod
    def _read(conn, buf, n):
        while len(buf) < n:
            chunk = conn.recv(65536)
            if not chunk:
                raise EOFError
            buf += chunk
        return buf[:n], buf[n:]

    def _handle(self, conn):
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += conn.recv(4096)
        head, buf = buf.split(b"\r\n\r\n", 1)
        lines = head.decode("latin-1").split("\r\n")
        headers = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        self.paths.append(lines[0].split()[1])
        self.protocols.append(headers.get("sec-websocket-protocol", ""))
        accept = base64.b64encode(hashlib.sha1((headers["sec-websocket-key"] + self.GUID).encode()).digest()).decode()
        conn.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {accept}\r\nSec-WebSocket-Protocol: lambda-microvms\r\n\r\n").encode())
        conn.sendall(bytes([0x89, 0]))  # クライアントの pong 応答を試すため ping を送る
        for _ in range(self.flood // 65536):
            conn.sendall(bytes([0x82, 127]) + (65536).to_bytes(8, "big") + os.urandom(65536))
        try:
            while True:
                hdr, buf = self._read(conn, buf, 2)
                n = hdr[1] & 0x7F
                if n == 126:
                    ext, buf = self._read(conn, buf, 2)
                    n = int.from_bytes(ext, "big")
                elif n == 127:
                    ext, buf = self._read(conn, buf, 8)
                    n = int.from_bytes(ext, "big")
                mask, buf = self._read(conn, buf, 4)
                data, buf = self._read(conn, buf, n)
                if n:
                    m = (mask * (n // 4 + 1))[:n]
                    data = (int.from_bytes(data, "big") ^ int.from_bytes(m, "big")).to_bytes(n, "big")
                opcode = hdr[0] & 0x0F
                if opcode == 0x8:
                    conn.sendall(bytes([0x88, 0]))
                    break
                if opcode in (0x1, 0x2):
                    if len(data) < 126:
                        frame = bytes([0x82, len(data)])
                    elif len(data) < 65536:
                        frame = bytes([0x82, 126]) + len(data).to_bytes(2, "big")
                    else:
                        frame = bytes([0x82, 127]) + len(data).to_bytes(8, "big")
                    conn.sendall(frame + data)
        except (EOFError, OSError):
            pass
        finally:
            conn.close()


class ConnectProxy:
    """HTTP CONNECT だけを扱う最小限のプロキシ。"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.targets = []
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                c, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(c,), daemon=True).start()

    def _handle(self, c):
        req = b""
        while b"\r\n\r\n" not in req:
            req += c.recv(4096)
        host, port = req.split()[1].decode().rsplit(":", 1)
        self.targets.append(f"{host}:{port}")
        u = socket.create_connection((host, int(port)))
        c.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")

        def pipe(a, b):
            try:
                while True:
                    d = a.recv(65536)
                    if not d:
                        break
                    b.sendall(d)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        threading.Thread(target=pipe, args=(c, u), daemon=True).start()
        pipe(u, c)


class StatusApi(http.server.ThreadingHTTPServer):
    """MicroVM 内の supervisor.py (:8080) の代わり。"""

    def __init__(self):
        self.received = []
        self.headers_seen = []
        self.snapshot_ok = True
        api = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, body):
                data = json.dumps(body, ensure_ascii=False).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                api.headers_seen.append({k.lower(): v for k, v in self.headers.items()})
                self.reply({"name": "a1", "agent": "claude", "bootstrap": "ready", "lastSnapshot": None,
                            "windows": [{"name": "shell", "command": "bash"},
                                        {"name": "claude", "command": "claude", "screen": "こんにちは"}]})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                api.received.append((self.path, json.loads(body or b"{}")))
                ok = api.snapshot_ok or self.path != "/snapshot"
                self.reply({"ok": ok, "error": None, "output": "saved" if ok else "tar failed"})

        super().__init__(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.serve_forever, daemon=True).start()


# ---------------------------------------------------------------- 単体テスト

class ParseEnvFileTest(unittest.TestCase):
    def test_parse(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.env"
            p.write_text(
                "# comment\n"
                "AWS_REGION=ap-northeast-1          # inline comment\n"
                'INGRESS_CONNECTORS="HTTP_INGRESS SHELL_INGRESS"\n'
                "DEFAULT_CLAUDE_ARGS=\"--dangerously-skip-permissions\"   # x\n"
                "export GIT_USER_NAME='山田 太郎'\n"
                "EMPTY=\n"
                "URL=https://example.com/#anchor\n",
                encoding="utf-8")
            r = mvm.parse_env_file(p)
        self.assertEqual(r["AWS_REGION"], "ap-northeast-1")
        self.assertEqual(r["INGRESS_CONNECTORS"], "HTTP_INGRESS SHELL_INGRESS")
        self.assertEqual(r["DEFAULT_CLAUDE_ARGS"], "--dangerously-skip-permissions")
        self.assertEqual(r["GIT_USER_NAME"], "山田 太郎")
        self.assertEqual(r["EMPTY"], "")
        self.assertEqual(r["URL"], "https://example.com/#anchor")


class WsTargetTest(unittest.TestCase):
    def test_default_and_override(self):
        self.assertEqual(mvm.ws_target("x.on.aws"), ("x.on.aws", 443, True))
        os.environ["PMVA_WS_BASE_URL"] = "ws://127.0.0.1:9001"
        try:
            self.assertEqual(mvm.ws_target("x.on.aws"), ("127.0.0.1", 9001, False))
        finally:
            del os.environ["PMVA_WS_BASE_URL"]


class ArtifactZipTest(unittest.TestCase):
    def test_lf_and_modes(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            img = root / "image"
            (img / "__pycache__").mkdir(parents=True)
            (img / "Dockerfile").write_bytes(b"FROM x\r\nRUN true\r\n")
            (img / "bootstrap.sh").write_bytes(b"#!/bin/bash\r\necho hi\r\n")
            (img / "__pycache__" / "x.pyc").write_bytes(b"\x00\r\n")
            old = mvm.ROOT
            mvm.ROOT = root
            try:
                mvm.build_artifact_zip(root / "a.zip")
            finally:
                mvm.ROOT = old
            with zipfile.ZipFile(root / "a.zip") as z:
                names = sorted(z.namelist())
                self.assertEqual(names, ["Dockerfile", "bootstrap.sh"])
                self.assertNotIn(b"\r\n", z.read("bootstrap.sh"))
                self.assertEqual((z.getinfo("bootstrap.sh").external_attr >> 16) & 0o777, 0o755)
                self.assertEqual((z.getinfo("Dockerfile").external_attr >> 16) & 0o777, 0o644)

    def test_repo_image_has_no_crlf(self):
        # .gitattributes で LF に固定していることの確認 (Windows で checkout しても壊れない)
        for p in (ROOT / "image").iterdir():
            if p.is_file():
                self.assertNotIn(b"\r\n", p.read_bytes(), p.name)


class WebSocketTest(unittest.TestCase):
    def test_roundtrip_large_and_small(self):
        server = EchoWebSocketServer()
        try:
            ws = mvm.WebSocket("127.0.0.1", "/", mvm.ws_protocols("TOK", 7681), port=server.port, tls=False)
            for size in (0 + 5, 200, 70000, 300000):
                data = os.urandom(size)
                ws.send(data)
                got = b""
                while len(got) < size:
                    got += ws.recv()
                self.assertEqual(got, data)
            ws.close()
            self.assertEqual(server.protocols[0],
                             "lambda-microvms.authentication.TOK, lambda-microvms, lambda-microvms.port.7681")
        finally:
            server.close()

    @unittest.skipUnless(shutil.which("openssl"), "openssl が必要")
    def test_tls_concurrent_send_and_receive(self):
        """受信スレッド・送信・ping を同時に動かしても TLS 上のデータが壊れないこと。"""
        with tempfile.TemporaryDirectory() as d:
            cert, key = Path(d) / "cert.pem", Path(d) / "key.pem"
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                            "-out", str(cert), "-days", "1", "-subj", "/CN=localhost",
                            "-addext", "subjectAltName=DNS:localhost"], check=True, capture_output=True)
            server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            server_ctx.load_cert_chain(str(cert), str(key))
            server = EchoWebSocketServer(ssl_context=server_ctx)
            original = mvm.ssl.create_default_context
            mvm.ssl.create_default_context = lambda: original(cafile=str(cert))
            try:
                ws = mvm.WebSocket("localhost", "/", ["lambda-microvms"], port=server.port, tls=True)
                chunks = [os.urandom(1000 + i * 37) for i in range(300)]
                expected = b"".join(chunks)
                got = bytearray()

                def reader():
                    while len(got) < len(expected):
                        data = ws.recv()
                        if data is None:
                            return
                        got.extend(data)

                def pinger():
                    for _ in range(200):
                        ws.send(b"", 0x9)

                def sender():
                    for c in chunks:
                        ws.send(c)

                threads = [threading.Thread(target=f, daemon=True) for f in (reader, pinger, sender)]
                for th in threads:
                    th.start()
                for th in threads:
                    th.join(60)
                self.assertFalse(any(th.is_alive() for th in threads), "送受信が止まった")
                self.assertEqual(bytes(got), expected)
                ws.close()
            finally:
                mvm.ssl.create_default_context = original
                server.close()

    def test_sending_does_not_stop_receiving(self):
        """相手が送り終えるまで読まないサーバーに、こちらも大量に送る。送信中に受信が止まると、
        互いに相手が読むのを待って止まる (以前の実装で Windows の CI が止まった)。"""
        size = 2 * 1024 * 1024
        server = EchoWebSocketServer(flood=size, sockbuf=65536)
        try:
            ws = mvm.WebSocket("127.0.0.1", "/", ["lambda-microvms"], port=server.port, tls=False)
            for opt in (socket.SO_SNDBUF, socket.SO_RCVBUF):
                ws.sock.setsockopt(socket.SOL_SOCKET, opt, 65536)
            received = [0]

            def reader():
                while received[0] < size * 2:  # 先に送りつけられた分 + こちらが送った分のエコー
                    data = ws.recv()
                    if data is None:
                        return
                    received[0] += len(data)

            def sender():
                chunk = os.urandom(65536)
                for _ in range(size // 65536):
                    ws.send(chunk)

            threads = [threading.Thread(target=f, daemon=True) for f in (reader, sender)]
            for th in threads:
                th.start()
            for th in threads:
                th.join(30)
            self.assertFalse(any(th.is_alive() for th in threads), f"止まった (受信 {received[0]} バイト)")
            self.assertEqual(received[0], size * 2)
            ws.close()
        finally:
            server.close()

    def test_through_connect_proxy(self):
        server, proxy = EchoWebSocketServer(), ConnectProxy()
        saved = {k: os.environ.get(k) for k in ("HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy")}
        # Windows では環境変数名の大文字小文字を区別しないので、消してから設定する
        for k in ("https_proxy", "HTTPS_PROXY", "NO_PROXY", "no_proxy"):
            os.environ.pop(k, None)
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{proxy.port}"
        try:
            ws = mvm.WebSocket("127.0.0.1", "/", ["lambda-microvms"], port=server.port, tls=False)
            ws.send(b"via-proxy")
            self.assertEqual(ws.recv(), b"via-proxy")
            ws.close()
            self.assertEqual(proxy.targets, [f"127.0.0.1:{server.port}"])
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
            server.close()
            proxy.close()


# ---------------------------------------------------------------- CLI (サブプロセス) のテスト

class CliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = Path(self.tmp.name)
        self.stub_dir = t / "stub"
        bindir = t / "bin"
        bindir.mkdir()
        stub = ROOT / "tests" / "aws_stub.py"
        if IS_WINDOWS:
            (bindir / "aws.cmd").write_text(f'@"{sys.executable}" "{stub}" %*\r\n', encoding="utf-8")
        else:
            p = bindir / "aws"
            p.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub}" "$@"\n', encoding="utf-8")
            p.chmod(0o755)
        self.home = t / "home"
        (self.home / ".ssh").mkdir(parents=True)
        (self.home / ".ssh" / "id_ed25519.pub").write_text("ssh-ed25519 AAAATEST test@pc\n", encoding="utf-8")
        cfg = t / "config.env"
        cfg.write_text(
            "AWS_REGION=ap-northeast-1\nMEMORY_MIB=4096\nMAX_AGENTS=10\nUSE_GITHUB_TOKEN=true\n"
            "SSH_PUBLIC_KEY_FILE=~/.ssh/id_ed25519.pub\nGIT_USER_NAME=山田 太郎\n", encoding="utf-8")
        self.env = dict(os.environ)
        self.env.update({
            "PATH": str(bindir) + os.pathsep + os.environ.get("PATH", ""),
            "PMVA_CONFIG": str(cfg), "PMVA_STATE_DIR": str(t / "state"), "PMVA_STUB_DIR": str(self.stub_dir),
            "HOME": str(self.home), "USERPROFILE": str(self.home), "PYTHONIOENCODING": "utf-8",
        })
        for k in ("HTTPS_PROXY", "https_proxy", "PMVA_API_BASE_URL", "PMVA_WS_BASE_URL"):
            self.env.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()

    def mvm(self, *args, input=None, env=None, check=True):
        p = subprocess.run([sys.executable, str(MVM), *args], input=input, capture_output=True,
                           env=env or self.env, timeout=120)
        out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
        if check and p.returncode != 0:
            self.fail(f"mvm {' '.join(args)} failed ({p.returncode}):\n{out}\n{err}")
        return p.returncode, out, err

    def payloads(self):
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.stub_dir.glob("microvm-*.payload"))]

    def test_up_builds_payload(self):
        self.mvm("up", "a1", "--repo", "https://github.com/o/r.git", "--prompt", "日本語の指示 \"quote\" & | < > ^ %PATH%",
                 "--claude-args", "--dangerously-skip-permissions")
        p = self.payloads()[0]
        self.assertEqual(p["name"], "a1")
        self.assertEqual(p["prompt"], "日本語の指示 \"quote\" & | < > ^ %PATH%")
        self.assertEqual(p["claudeArgs"], "--dangerously-skip-permissions")
        self.assertEqual(p["claudeSecret"], "pmva/claude-oauth-token")
        self.assertEqual(p["githubSecret"], "pmva/github-token")
        self.assertEqual(p["branch"], "pmva/a1")
        self.assertEqual(p["gitUserName"], "山田 太郎")
        self.assertEqual(p["sshPublicKey"], "ssh-ed25519 AAAATEST test@pc")
        self.assertEqual(p["codexAuth"], "none")
        raw = next(self.stub_dir.glob("microvm-*.payload")).read_text(encoding="utf-8")
        self.assertTrue(raw.isascii(), "非 ASCII は \\uXXXX にエスケープしてコマンドラインに載せる")
        args = json.loads(next(self.stub_dir.glob("microvm-*.args")).read_text(encoding="utf-8"))
        self.assertIn("--client-token", args)
        self.assertTrue(args[args.index("--run-hook-payload") + 1].startswith("file://"))
        self.assertEqual(list(Path(self.env["PMVA_STATE_DIR"]).glob("payload-*")), [], "一時ファイルが残っていない")
        self.assertEqual(json.loads(args[args.index("--idle-policy") + 1])["maxIdleDurationSeconds"], 1800)

    def test_agent_and_auth_selection(self):
        self.mvm("up", "c1", "--agent", "codex", "--codex-auth", "chatgpt", "--no-github")
        self.mvm("up", "b1", "--agent", "both", "--claude-auth", "bedrock", "--idle", "off")
        c1, b1 = self.payloads()
        self.assertEqual((c1["claudeAuth"], c1["codexAuth"], c1["codexSecret"]), ("none", "chatgpt", "pmva/codex-auth-json"))
        self.assertNotIn("githubSecret", c1)
        self.assertEqual((b1["claudeAuth"], b1["codexAuth"]), ("bedrock", "api-key"))
        self.assertNotIn("claudeSecret", b1)
        args = json.loads(sorted(self.stub_dir.glob("microvm-*.args"))[1].read_text(encoding="utf-8"))
        self.assertNotIn("--idle-policy", args)

    def test_execution_role_matches_agents(self):
        self.env["PMVA_STUB_QUOTA_GB"] = "100"
        self.mvm("up", "r-claude")
        self.mvm("up", "r-codex", "--agent", "codex")
        self.mvm("up", "r-both", "--agent", "both")
        self.mvm("up", "r-none", "--agent", "none", "--no-github")
        roles = []
        for f in sorted(self.stub_dir.glob("microvm-*.args")):
            args = json.loads(f.read_text(encoding="utf-8"))
            roles.append(args[args.index("--execution-role-arn") + 1].rsplit("/", 1)[1])
        self.assertEqual(roles, ["exec-claude", "exec-codex", "exec", "exec-none"])

    def test_secret_names_must_match_role_prefixes(self):
        rc, _, err = self.mvm("up", "x1", "--claude-secret", "pmva/openai-api-key", check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("pmva/anthropic-", err)
        self.mvm("up", "x2", "--claude-secret", "pmva/claude-oauth-token-team")
        rc, _, _ = self.mvm("secret", "set", "openai-api-key", "--name", "pmva/claude-x", input=b"k", check=False)
        self.assertNotEqual(rc, 0)
        self.mvm("secret", "set", "openai-api-key", "--name", "pmva/openai-team", input=b"k")

    def test_infra_parameters_include_nightly_stop(self):
        cfg = Path(self.env["PMVA_CONFIG"])
        cfg.write_text(cfg.read_text(encoding="utf-8") + "NIGHTLY_STOP_TIME=23:30\n", encoding="utf-8")
        self.mvm("infra", "deploy", check=False)
        calls = [json.loads(l) for l in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        deploy = next(c for c in calls if c[:2] == ["cloudformation", "deploy"])
        self.assertIn("NightlyStopCron=cron(30 23 * * ? *)", deploy)
        self.assertIn("NightlyStopTimezone=Asia/Tokyo", deploy)
        self.assertIn("ImageName=pmva-agent", deploy)

    def test_infra_deploy_minimal_template(self):
        cfg = Path(self.env["PMVA_CONFIG"])
        cfg.write_text(cfg.read_text(encoding="utf-8") + "INFRA_TEMPLATE=minimal\nNIGHTLY_STOP_TIME=23:30\n",
                       encoding="utf-8")
        _, _, err = self.mvm("infra", "deploy")
        calls = [json.loads(l) for l in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        deploy = next(c for c in calls if c[:2] == ["cloudformation", "deploy"])
        self.assertTrue(deploy[deploy.index("--template-file") + 1].endswith("cloudformation-minimal.yaml"))
        overrides = {x.split("=", 1)[0] for x in deploy if "=" in x and not x.startswith("-")}
        self.assertEqual(overrides, {"Prefix", "EnableBedrock"}, "テンプレートに無いパラメータは渡さない")
        self.assertIn("NIGHTLY_STOP_TIME は最小構成では使いません", err)
        cfg.write_text(cfg.read_text(encoding="utf-8") + "INFRA_TEMPLATE=tiny\n", encoding="utf-8")
        self.assertNotEqual(self.mvm("infra", "deploy", check=False)[0], 0)

    def test_notify_option(self):
        self.mvm("up", "n1", "--notify")
        self.mvm("up", "n2")
        n1, n2 = self.payloads()
        self.assertEqual(n1["notifySecret"], "pmva/notify-webhook")
        self.assertNotIn("notifySecret", n2)
        rc, _, err = self.mvm("secret", "set", "notify-webhook", input=b"not-a-url", check=False)
        self.assertNotEqual(rc, 0)
        self.mvm("secret", "set", "notify-webhook", input=b"https://hooks.slack.com/services/T/B/X")

    def test_memory_quota_blocks_fleet_before_launch(self):
        rc, out, err = self.mvm("fleet", "3", check=False)  # 8GB の上限に 4GB x 3 は入らない
        self.assertNotEqual(rc, 0)
        self.assertIn("メモリ上限", err)
        self.assertEqual(self.payloads(), [])
        self.mvm("fleet", "2", "--prefix", "w")
        self.assertEqual([p["name"] for p in self.payloads()], ["w-01", "w-02"])
        rc, out, err = self.mvm("up", "w-03", check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("あと 0 台", err)

    def test_fleet_prompts_and_max_agents(self):
        env = dict(self.env, PMVA_STUB_QUOTA_GB="100")
        prompts = Path(self.tmp.name) / "prompts.txt"
        prompts.write_text("task one\ntask two\n", encoding="utf-8")
        self.mvm("fleet", "2", "--prefix", "f", "--prompts-file", str(prompts), env=env)
        self.assertEqual([p.get("prompt") for p in self.payloads()], ["task one", "task two"])
        rc, _, err = self.mvm("fleet", "11", env=env, check=False)
        self.assertNotEqual(rc, 0)

    def test_ls_stop_renew(self):
        self.mvm("up", "r1", "--prompt", "first")
        _, out, _ = self.mvm("ls")
        self.assertIn("r1", out)
        self.assertIn("RUNNING", out)
        self.assertRegex(out, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
        self.mvm("stop", "r1", "--no-save")
        _, out, _ = self.mvm("ls")
        self.assertNotIn("r1", out)
        _, out, _ = self.mvm("ls", "--all")
        self.assertIn("TERMINATED", out)
        self.mvm("renew", "r1")
        renewed = self.payloads()[-1]
        self.assertEqual(renewed["restore"], "s3://pmva-bucket/workspaces/r1/latest.tar.gz")
        self.assertNotIn("prompt", renewed)

    def test_infra_delete_waits_for_termination(self):
        self.mvm("up", "d1")
        self.mvm("stop", "d1", "--no-save")
        (self.stub_dir / "microvm-0000.terminating").write_text("1")
        rc, out, err = self.mvm("infra", "delete", input=b"y\n")
        calls = [json.loads(l) for l in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        names = [" ".join(c[:2]) for c in calls]
        last_get = max(i for i, n in enumerate(names) if n == "lambda-microvms get-microvm")
        self.assertLess(last_get, names.index("s3 rm"), "VM が終了しきってからバケットを空にする")
        self.assertIn("終了を待っています", err)

    def test_infra_delete_refuses_with_running_sessions(self):
        self.mvm("up", "d2")
        rc, _, err = self.mvm("infra", "delete", input=b"y\n", check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("stop-all", err)

    def test_secret_set_from_stdin(self):
        self.mvm("secret", "set", "github-token", input="ghp_example\n".encode())
        self.assertEqual((self.stub_dir / "secret-pmva_github-token").read_text(encoding="utf-8"), "ghp_example")
        state = Path(self.env["PMVA_STATE_DIR"])
        self.assertEqual(list(state.glob("secret.*")), [], "一時ファイルが残っていない")
        rc, _, err = self.mvm("secret", "set", "github-token", "--name", "other/x", input=b"v", check=False)
        self.assertNotEqual(rc, 0)

    def test_ssh_config_install_is_idempotent(self):
        self.mvm("up", "s1")
        cfg = self.home / ".ssh" / "config"
        cfg.write_text("Host github.com\n  User git\n", encoding="utf-8")
        self.mvm("ssh-config", "--install")
        self.mvm("ssh-config", "--install")
        text = cfg.read_bytes()
        self.assertFalse(text.startswith(b"\xef\xbb\xbf"), "BOM なし")
        text = text.decode("utf-8")
        self.assertEqual(text.count(">>> pmva"), 1)
        self.assertIn("Host github.com", text)
        self.assertIn("Host pmva-s1", text)
        self.assertIn("_ssh-proxy s1", text)
        self.assertIn(Path(sys.executable).as_posix(), text)

    def test_peek_and_send(self):
        api = StatusApi()
        try:
            self.mvm("up", "a1")
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            _, out, _ = self.mvm("peek", "a1", "5", env=env)
            self.assertIn("こんにちは", out)
            self.assertEqual(api.headers_seen[0].get("x-aws-proxy-auth"), "TOKEN-microvm-0000")
            self.assertEqual(api.headers_seen[0].get("x-aws-proxy-port"), "8080")
            self.mvm("send", "a1", "テストを実行して", env=env)
            self.assertEqual(api.received[-1], ("/send", {"window": "claude", "text": "テストを実行して", "enter": True}))
            # オプションを名前の後に書いても指示文に混ざらない。"-" で始まる指示は "--" の後に書く
            self.mvm("send", "a1", "--window", "shell", "--no-enter", "echo", "hi", env=env)
            self.assertEqual(api.received[-1][1], {"window": "shell", "text": "echo hi", "enter": False})
            self.mvm("send", "a1", "--", "-h", env=env)
            self.assertEqual(api.received[-1][1]["text"], "-h")
        finally:
            api.shutdown()

    def test_ssh_proxy_pipes_binary_stdio(self):
        """ssh の ProxyCommand と同じ使い方で、標準入出力がバイナリのまま中継されることを確かめる。"""
        server = EchoWebSocketServer()
        try:
            self.mvm("up", "p1")
            env = dict(self.env, PMVA_WS_BASE_URL=f"ws://127.0.0.1:{server.port}")
            data = os.urandom(200000) + b"\r\n\x00\x1a\r\n"
            proc = subprocess.Popen([sys.executable, str(MVM), "_ssh-proxy", "p1"], env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            got = bytearray()
            reader = threading.Thread(target=lambda: got.extend(proc.stdout.read(len(data))), daemon=True)
            reader.start()
            proc.stdin.write(data)
            proc.stdin.flush()
            reader.join(60)
            proc.stdin.close()
            proc.wait(30)
            proc.stdout.close()
            proc.stderr.close()
            self.assertEqual(bytes(got), data)
            self.assertIn("lambda-microvms.port.7681", server.protocols[0])
        finally:
            server.close()

    def test_doctor_reports_quota(self):
        rc, out, _ = self.mvm("doctor", check=False)
        self.assertIn("2 台まで", out)
        self.assertIn("40GB", out)

    def test_agent_count_follows_quota_by_default(self):
        """MAX_AGENTS を設定しなければ (0)、同時に動かせる台数はクォータだけで決まる。"""
        cfg = Path(self.env["PMVA_CONFIG"])
        cfg.write_text(cfg.read_text(encoding="utf-8").replace("MAX_AGENTS=10\n", ""), encoding="utf-8")
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("ok   MicroVM のメモリ上限 8GB → 4GB の VM を同時に 2 台まで", out)
        self.assertNotIn("MAX_AGENTS", out)
        rc, _, err = self.mvm("fleet", "3", "--no-github", check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("あと 2 台まで", err)
        self.assertEqual(self.payloads(), [], "クォータに収まらなければ 1 台も起動しない")
        self.mvm("fleet", "12", "--no-github", env=dict(self.env, PMVA_STUB_QUOTA_GB="100"))
        self.assertEqual(len(self.payloads()), 12, "MAX_AGENTS が 0 なら台数の上限はクォータだけ")

    def test_doctor_checks_credentials(self):
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("管理者権限ではありません", out)
        env = dict(self.env, PMVA_STUB_IAM="admin", PMVA_STUB_KEY_CREATED="2020-01-01T00:00:00+00:00")
        _, out, _ = self.mvm("doctor", env=env, check=False)
        self.assertIn("AdministratorAccess", out)
        self.assertIn("AKIAOLDKEY", out)
        _, out, _ = self.mvm("doctor", env=dict(self.env, PMVA_STUB_IAM="denied"), check=False)
        self.assertIn("権限の確認は省略しました", out)
        _, out, _ = self.mvm("doctor", env=dict(self.env, PMVA_STUB_ARN="arn:aws:iam::123456789012:root"), check=False)
        self.assertIn("ルートユーザー", out)
        arn = "arn:aws:sts::123456789012:assumed-role/AWSReservedSSO_AdministratorAccess_x/me@example.com"
        _, out, _ = self.mvm("doctor", env=dict(self.env, PMVA_STUB_ARN=arn, PMVA_STUB_IAM="admin"), check=False)
        self.assertIn("管理者権限 (AdministratorAccess) のロール", out)
        calls = (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8")
        self.assertIn('"--role-name", "AWSReservedSSO_AdministratorAccess_x"', calls)

    def test_api_error_does_not_mark_session_terminated(self):
        self.mvm("up", "u1")
        _, out, _ = self.mvm("ls", env=dict(self.env, PMVA_STUB_GET_DENIED="1"))
        self.assertIn("UNKNOWN", out)
        _, out, _ = self.mvm("ls")
        self.assertIn("RUNNING", out, "一時的なエラーで終了扱いにしない")
        rc, _, err = self.mvm("infra", "delete", input=b"y\n", env=dict(self.env, PMVA_STUB_GET_DENIED="1"), check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("stop-all", err)

    def test_renew_keeps_vm_when_save_fails(self):
        api = StatusApi()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "w1", env=env)
            api.snapshot_ok = False
            rc, _, err = self.mvm("renew", "w1", env=env, check=False)
            self.assertNotEqual(rc, 0)
            self.assertIn("終了せずに止めました", err)
            self.assertFalse((self.stub_dir / "microvm-0000.term").exists(), "保存できなければ終了しない")
            self.assertEqual(len(self.payloads()), 1)
            # 状態を取れないときも、保存できたか分からないので終了しない
            rc, _, err = self.mvm("renew", "w1", env=dict(env, PMVA_STUB_GET_DENIED="1"), check=False)
            self.assertNotEqual(rc, 0)
            self.assertIn("UNKNOWN", err)
            self.assertFalse((self.stub_dir / "microvm-0000.term").exists())
            api.snapshot_ok = True
            self.mvm("renew", "w1", env=env)
            self.assertTrue((self.stub_dir / "microvm-0000.term").exists())
            self.assertEqual(len(self.payloads()), 2)
        finally:
            api.shutdown()

    def test_stop_resumes_suspended_vm_to_save(self):
        api = StatusApi()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "z1", env=env)
            (self.stub_dir / "microvm-0000.suspended").touch()
            self.mvm("stop", "z1", env=env)
            calls = [json.loads(l)[:2] for l in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertIn(["lambda-microvms", "resume-microvm"], calls)
            self.assertIn(("/snapshot", {}), api.received)
        finally:
            api.shutdown()

    def test_infra_delete_retains_undeletable_lambda(self):
        env = dict(self.env, PMVA_STUB_UNDELETABLE="KeepalivePermission,KeepaliveFunction")
        rc, _, err = self.mvm("infra", "delete", input=b"y\n", env=env)
        retained = json.loads((self.stub_dir / "retained.json").read_text())
        self.assertEqual(retained, ["KeepalivePermission", "KeepaliveFunction"])
        self.assertIn("aws lambda delete-function --function-name pmva-keepalive", err)

    def test_deploy_stops_on_leftover_lambda(self):
        rc, _, err = self.mvm("infra", "deploy", env=dict(self.env, PMVA_STUB_LAMBDA="exists"), check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("前回の削除で残った Lambda 関数 pmva-keepalive", err)
        calls = (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8")
        self.assertNotIn('"cloudformation", "deploy"', calls)
        _, _, err = self.mvm("infra", "deploy", env=dict(self.env, PMVA_STUB_LAMBDA="denied"), check=False)
        self.assertIn("AccessDenied", err)

    def test_launchers(self):
        if IS_WINDOWS:
            p = subprocess.run(["cmd", "/c", str(ROOT / "bin" / "mvm.cmd"), "--help"], capture_output=True, env=self.env)
        else:
            p = subprocess.run([str(ROOT / "bin" / "mvm"), "--help"], capture_output=True, env=self.env)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("fleet", p.stdout.decode("utf-8", "replace"))

    def test_cost(self):
        _, out, _ = self.mvm("cost", "--agents", "2")
        self.assertIn("2 台", out)

    def test_cost_actual(self):
        _, out, _ = self.mvm("cost", "--actual", "--days", "3", "--daily")
        lines = out.splitlines()
        self.assertIn("AWS Lambda", lines[1])  # 金額の大きい順
        self.assertIn("3.75", lines[1])
        self.assertNotIn("Amazon S3", out)  # 0.01 未満は省く
        self.assertTrue(any("2026-10-28" in x and "1.25" in x for x in lines))
        calls = [json.loads(x) for x in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        ce = [c for c in calls if c[:2] == ["ce", "get-cost-and-usage"]]
        self.assertEqual(len(ce), 2)
        self.assertEqual(ce[0][ce[0].index("--region") + 1], "us-east-1")
        self.assertEqual(self.mvm("cost", "--actual", "--days", "0", check=False)[0], 1)

    def test_image_versions(self):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), NpmRegistryHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        env = dict(self.env, PMVA_NPM_REGISTRY=f"http://127.0.0.1:{server.server_address[1]}",
                   NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
        for k in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            env.pop(k, None)
        _, out, _ = self.mvm("image", "versions", env=env)
        pinned = mvm.dockerfile_versions(ROOT / "image" / "Dockerfile")
        self.assertRegex(pinned["CLAUDE_CODE_VERSION"], r"^\d+\.\d+\.\d+$", "Dockerfile では版を固定する")
        self.assertRegex(pinned["CODEX_VERSION"], r"^\d+\.\d+\.\d+$")
        self.assertIn("@anthropic-ai/claude-code", out)
        self.assertIn("99.0.1", out)
        self.assertIn("更新あり", out)
        self.assertEqual(sorted(NpmRegistryHandler.paths), ["/@anthropic-ai%2Fclaude-code/latest", "/@openai%2Fcodex/latest"])


class NpmRegistryHandler(http.server.BaseHTTPRequestHandler):
    paths = []

    def do_GET(self):
        NpmRegistryHandler.paths.append(self.path)
        body = json.dumps({"version": "99.0.1"}).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class DockerfileVersionsTest(unittest.TestCase):
    def test_rewrite(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "Dockerfile"
            p.write_bytes(b"FROM x\nARG CLAUDE_CODE_VERSION=1.0.0\nARG CODEX_VERSION=0.1.0\nRUN npm i\n")
            mvm.set_dockerfile_versions(p, {"CLAUDE_CODE_VERSION": "2.0.0"})
            self.assertEqual(p.read_bytes(), b"FROM x\nARG CLAUDE_CODE_VERSION=2.0.0\nARG CODEX_VERSION=0.1.0\nRUN npm i\n")
            self.assertEqual(mvm.dockerfile_versions(p), {"CLAUDE_CODE_VERSION": "2.0.0", "CODEX_VERSION": "0.1.0"})
            with self.assertRaises(SystemExit):
                mvm.set_dockerfile_versions(p, {"CODEX_VERSION": "1.0; rm -rf /"})


class InstallScriptTest(unittest.TestCase):
    """scripts/install.sh / install.ps1 が config.env を作り、エイリアスを 1 行だけ登録すること。"""

    def run_twice(self, cmd, rc, env):
        for _ in range(2):
            p = subprocess.run(cmd, capture_output=True, env=env, timeout=120)
            self.assertEqual(p.returncode, 0, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace"))
        text = rc.read_bytes()
        lines = text.decode("utf-8-sig").splitlines()
        self.assertEqual(lines[0], "# existing")
        self.assertEqual(sum(1 for x in lines if "mvm" in x), 1, lines)
        return text, lines[-1]

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, PMVA_CONFIG=str(self.tmp / "config.env"), HOME=str(self.tmp))

    @unittest.skipIf(IS_WINDOWS, "macOS / Linux 用")
    def test_install_sh(self):
        rc = self.tmp / ".zshrc"
        rc.write_text("# existing\n", encoding="utf-8")
        _, last = self.run_twice(["sh", str(ROOT / "scripts" / "install.sh"), "--rc", str(rc)], rc, self.env)
        self.assertEqual(last, f"alias mvm='{ROOT / 'bin' / 'mvm'}' # pmva")
        self.assertTrue((self.tmp / "config.env").exists())

    def test_install_ps1(self):
        shell = shutil.which("powershell") if IS_WINDOWS else None
        shell = shell or shutil.which("pwsh")
        if not shell:
            self.skipTest("PowerShell がありません")
        rc = self.tmp / "profile.ps1"
        rc.write_text("# existing\n", encoding="utf-8")
        cmd = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ROOT / "scripts" / "install.ps1"),
               "-ProfilePath", str(rc)]
        text, last = self.run_twice(cmd, rc, self.env)
        self.assertTrue(text.startswith(b"\xef\xbb\xbf"), "Windows PowerShell 5.1 向けに BOM 付きで書く")
        self.assertTrue(last.startswith("Set-Alias mvm '") and last.endswith("mvm.cmd' # pmva"), last)
        self.assertTrue((self.tmp / "config.env").exists())


if __name__ == "__main__":
    unittest.main()
