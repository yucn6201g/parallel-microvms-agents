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
import socket
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

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.protocols = []
        self.paths = []
        threading.Thread(target=self._serve, daemon=True).start()

    def close(self):
        self.sock.close()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
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
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
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
                self.reply({"ok": True, "error": None, "output": "saved"})

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


if __name__ == "__main__":
    unittest.main()
