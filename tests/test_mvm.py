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
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.parse
import zipfile
from pathlib import Path
from unittest import mock

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
        self._on_open(conn)
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
                if opcode in (0x1, 0x2) and not self._on_data(conn, data):
                    break
        except (EOFError, OSError):
            pass
        finally:
            conn.close()

    @staticmethod
    def _frame(data, opcode=0x2):
        if len(data) < 126:
            return bytes([0x80 | opcode, len(data)]) + data
        if len(data) < 65536:
            return bytes([0x80 | opcode, 126]) + len(data).to_bytes(2, "big") + data
        return bytes([0x80 | opcode, 127]) + len(data).to_bytes(8, "big") + data

    def _on_open(self, conn):
        pass

    def _on_data(self, conn, data):
        """受け取ったデータフレームの処理。False を返すと接続を閉じる。"""
        conn.sendall(self._frame(data))
        return True


class ShellWebSocketServer(EchoWebSocketServer):
    """Lambda MicroVMs のシェル接続 (/shell) の代わり。

    接続直後に制御メッセージ (session_init) を送り、受け取った行に少し遅れて応答する (本物も応答に時間がかかる)。
    exit の行か、行頭の Ctrl-D (\\x04) で接続を閉じる (本物はシェルが終わると閉じる。2026-11-20 に実機で確認)。
    """

    def __init__(self, delay=0.5):
        self.delay, self.inputs, self.pending = delay, [], {}
        super().__init__()

    def _on_open(self, conn):
        conn.sendall(self._frame(b'{"type":"session_init","session_id":"test"}', 0x1))
        conn.sendall(self._frame(b"$ "))
        self.pending[id(conn)] = b""

    def _on_data(self, conn, data):
        self.inputs.append(data)
        pending = self.pending[id(conn)] + data
        while b"\n" in pending:
            line, pending = pending.split(b"\n", 1)
            line = line.rstrip(b"\r")
            time.sleep(self.delay)
            if line in (b"exit", b"\x04"):
                conn.sendall(self._frame(b"exit\r\n") + bytes([0x88, 0]))
                return False
            conn.sendall(self._frame(b"OUT:" + line + b"\r\n$ "))
        if pending == b"\x04":
            time.sleep(self.delay)
            conn.sendall(self._frame(b"exit\r\n") + bytes([0x88, 0]))
            return False
        self.pending[id(conn)] = pending
        return True


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
        # /status の応答。複数あれば呼ばれるたびに先頭から順に返し、最後の 1 つは返し続ける
        self.statuses = [{"name": "a1", "agent": "claude", "bootstrap": "ready", "lastSnapshot": None,
                          "windows": [{"name": "shell", "command": "bash"},
                                      {"name": "claude", "command": "claude", "screen": "こんにちは"}]}]
        self.results = {}   # /result?agent=X の応答
        self.events = {}    # /events?agent=X の応答 (エージェントごとのリスト。呼ばれるたびに先頭から返し、尽きたら 500)
        self.event_queries = []
        self.status_calls = 0
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
                if self.path.startswith("/events"):
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    api.event_queries.append({k: v[0] for k, v in q.items()})
                    queue = api.events.get(q["agent"][0], [])
                    if not queue:
                        self.send_response(500)
                        self.send_header("Content-Length", "6")
                        self.end_headers()
                        return self.wfile.write(b"broken")
                    return self.reply(queue.pop(0))
                if self.path.startswith("/result"):
                    agent = self.path.split("agent=", 1)[-1]
                    return self.reply(api.results.get(agent, {"error": "not found"}))
                api.status_calls += 1
                self.reply(api.statuses.pop(0) if len(api.statuses) > 1 else api.statuses[0])

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
            (img / "Dockerfile").write_bytes(b"FROM x\r\nARG INSTALL_BROWSER_DEPS=true\r\nARG EXTRA_PACKAGES=\r\nRUN true\r\n")
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
                self.assertEqual(z.read("Dockerfile"), b"FROM x\nARG INSTALL_BROWSER_DEPS=true\nARG EXTRA_PACKAGES=\nRUN true\n")

    def test_config_is_written_into_dockerfile(self):
        """Lambda MicroVMs のビルドには引数を渡せないので、config.env の値を Dockerfile の ARG に書き込む。"""
        saved = dict(mvm.C)
        try:
            with tempfile.TemporaryDirectory() as d:
                mvm.C.update(IMAGE_BROWSER_DEPS="false", IMAGE_EXTRA_PACKAGES="  postgresql16-server   valkey ")
                mvm.build_artifact_zip(Path(d) / "a.zip")
                with zipfile.ZipFile(Path(d) / "a.zip") as z:
                    dockerfile = z.read("Dockerfile").decode("utf-8")
                    self.assertEqual((z.getinfo("pmva-node").external_attr >> 16) & 0o777, 0o755)
                self.assertIn("\nARG INSTALL_BROWSER_DEPS=false\n", dockerfile)
                self.assertIn("\nARG EXTRA_PACKAGES=postgresql16-server valkey\n", dockerfile)
                for bad in ("x; rm -rf /", "$(id)", "pkg|evil", "a&&b"):
                    mvm.C["IMAGE_EXTRA_PACKAGES"] = bad
                    with self.assertRaises(SystemExit):
                        mvm.build_artifact_zip(Path(d) / "b.zip")
        finally:
            mvm.C.clear()
            mvm.C.update(saved)

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


# ---------------------------------------------------------------- mvm watch (一覧ボード) の単体テスト

class DisplayWidthTest(unittest.TestCase):
    def test_width_counts_fullwidth_as_two(self):
        self.assertEqual(mvm.display_width("abc"), 3)
        self.assertEqual(mvm.display_width("作業中"), 6)
        self.assertEqual(mvm.display_width("a作b"), 4)
        self.assertEqual(mvm.display_width("Ａ１"), 4, "全角の英数 (F)")
        self.assertEqual(mvm.display_width("é"), 1, "結合文字は幅を持たない")
        self.assertEqual(mvm.display_width(""), 0)

    def test_cut_width_keeps_within_width_and_never_splits_a_character(self):
        self.assertEqual(mvm.cut_width("abc", 3), "abc")
        self.assertEqual(mvm.cut_width("作業中", 6), "作業中")
        self.assertEqual(mvm.cut_width("作業中abc", 7), "作業中…")
        self.assertEqual(mvm.cut_width("作業中", 5), "作業…", "全角が半端に入らない所では手前で切る")
        self.assertEqual(mvm.cut_width("作業中", 4), "作…")
        self.assertEqual(mvm.cut_width("abcdef", 4), "abc…")
        self.assertEqual(mvm.cut_width("abcdef", 0), "")
        for width in range(0, 12):
            self.assertLessEqual(mvm.display_width(mvm.cut_width("日本語 mixed 文字列 text", width)), width)

    def test_pad_width_aligns_mixed_text(self):
        for text in ("a1", "作業中", "a作"):
            self.assertEqual(mvm.display_width(mvm.pad_width(text, 8)), 8)
        self.assertEqual(mvm.pad_width("作業中", 4), "作業中", "足りているときは切らない")

    def test_format_elapsed(self):
        cases = [(None, "-"), (0, "0秒"), (45, "45秒"), (60, "1分"), (720, "12分"), (3599, "59分"),
                 (3900, "1時間5分"), (7200, "2時間"), (-5, "0秒")]
        for seconds, text in cases:
            self.assertEqual(mvm.format_elapsed(seconds), text)


def watch_row(name, state, **kw):
    """format_watch に渡す、台ごとの dict。"""
    row = {"name": name, "vm": "RUNNING", "state": state, "agents": {}, "sinceSeconds": None, "memory": None,
           "oomKills": 0, "lastEvent": "", "notes": []}
    row.update(kw)
    return row


class FormatWatchTest(unittest.TestCase):
    STAMP = "2026-11-24 15:00:12"

    def board_rows(self):
        mem = lambda used, peak: {"usedMiB": used, "totalMiB": 3891, "peakMiB": peak}   # noqa: E731
        return [
            watch_row("a1", "working", sinceSeconds=720, memory=mem(1229, 1536), lastEvent="実行: Bash: pytest -x"),
            watch_row("a2", "waiting", sinceSeconds=180, memory=mem(900, 900),
                      agents={"claude": {"state": "waiting", "message": "Bash の実行の許可を求めています"}},
                      lastEvent="Bash の実行の許可を求めています",
                      notes=["a2 (claude): 許可・入力待ち: Bash の実行の許可を求めています"]),
            watch_row("a3", "done", sinceSeconds=60, memory=mem(500, 800), lastEvent="応答: 原因はトークンの期限切れでした"),
            watch_row("a4", "SUSPENDED", vm="SUSPENDED"),
        ]

    def test_plain_board_has_header_columns_notes_and_footer(self):
        text = mvm.format_watch(self.board_rows(), 100, False, self.STAMP, 10)
        lines = text.splitlines()
        self.assertEqual(lines[0], "2026-11-24 15:00:12  4 台 (作業中 1 / 要対応 1 / 完了 1)  10 秒ごと / Ctrl-C で終了")
        self.assertTrue(lines[1].startswith("名前"))
        self.assertIn("メモリ (使用/全体 最大)", lines[1])
        self.assertRegex(lines[2], r"^a1 +作業中 +12分 +1\.2/3\.8G 最大1\.5G +実行: Bash: pytest -x$")
        self.assertRegex(lines[3], r"^a2 +許可・入力待ち +3分 ")
        self.assertRegex(lines[4], r"^a3 +応答が終わった +1分 ")
        self.assertRegex(lines[5], r"^a4 +SUSPENDED +- +- +-$")
        self.assertEqual(lines[6], "")
        self.assertEqual(lines[7], "!! a2 (claude): 許可・入力待ち: Bash の実行の許可を求めています")
        self.assertEqual(lines[-1], "(表示中のセッションは自動サスペンドされません)")
        self.assertNotIn("\x1b", text)

    def test_columns_line_up_by_display_width(self):
        lines = mvm.format_watch(self.board_rows(), 100, False, self.STAMP, 10).splitlines()[1:6]
        starts = {mvm.display_width(ln[:ln.index(key)]) for ln, key in
                  zip(lines[:4], ("メモリ", "1.2/3.8G", "0.9/3.8G", "0.5/3.8G"))}
        self.assertEqual(len(starts), 1, f"メモリの列の開始位置がそろう: {starts}")

    def test_lines_are_cut_to_terminal_width(self):
        for width in (40, 60, 80):
            text = mvm.format_watch(self.board_rows(), width, False, self.STAMP, 10)
            for line in text.splitlines():
                self.assertLessEqual(mvm.display_width(line), width, line)
        wide = mvm.format_watch([watch_row("a1", "working", lastEvent="長い" * 100)], 80, False, self.STAMP, 10)
        self.assertIn("…", wide)

    def test_oom_is_shown_in_row_and_note(self):
        rows = [watch_row("a1", "working", oomKills=2, notes=["a1: メモリ不足でプロセスが 2 回強制終了されました"],
                          memory={"usedMiB": 3800, "totalMiB": 3891, "peakMiB": 3850})]
        text = mvm.format_watch(rows, 100, False, self.STAMP, 10)
        self.assertIn("3.7/3.8G 最大3.8G !!OOM×2", text)
        self.assertIn("!! a1: メモリ不足", text)
        self.assertIn("要対応 1", text)

    def test_color_only_when_requested(self):
        rows = self.board_rows() + [watch_row("a5", "error", agents={"claude": {"state": "error"}}, notes=["a5 (claude): エラー"]),
                                    watch_row("a6", "working", oomKills=1, notes=["a6: OOM"])]
        plain = mvm.format_watch(rows, 100, False, self.STAMP, 10)
        self.assertNotIn("\x1b", plain)
        colored = mvm.format_watch(rows, 100, True, self.STAMP, 10)
        lines = colored.splitlines()
        self.assertNotIn("", lines[0] + lines[1] + lines[2], "見出しとふつうの台には色を付けない")
        for expected in ("[33ma2", "[32ma3", "[31ma5", "[31ma6", "[33m!! a2", "[31m!! a6"):
            self.assertIn(expected, colored)
        self.assertTrue(all(ln.endswith("[0m") for ln in lines if "[" in ln), "行ごとに色を戻す")

    def test_control_characters_in_text_are_neutralized(self):
        rows = [watch_row("a1", "working", lastEvent="結果: \x1b[2J\x07消えない")]
        text = mvm.format_watch(rows, 100, False, self.STAMP, 10)
        self.assertNotIn("\x1b", text)
        self.assertNotIn("\x07", text)

    def test_empty_board(self):
        text = mvm.format_watch([], 100, False, self.STAMP, 10)
        self.assertIn("0 台", text)
        self.assertIn("表示するセッションがありません", text)


class FakeFleet:
    """watch の台数分の AWS (get-microvm) と VM の API の代わり。呼ばれた回数と内容を記録する。"""

    def __init__(self, vms, statuses=None, events=None):
        self.vms = dict(vms)               # 名前 → get-microvm の状態
        self.statuses = dict(statuses or {})   # 名前 → /status の応答 (SystemExit なら失敗)
        self.events = {k: list(v) for k, v in (events or {}).items()}   # (名前, エージェント) → /events の応答
        self.vm_calls = []
        self.api_calls = []

    def vm_state(self, name):
        self.vm_calls.append(name)
        return self.vms[name]

    def vm_api(self, name, method, path, body=None, timeout=30):
        self.api_calls.append((name, path))
        if path.startswith("/status"):
            st = self.statuses[name]
            if isinstance(st, SystemExit):
                raise st
            return st
        q = urllib.parse.parse_qs(urllib.parse.urlparse(path).query)
        queue = self.events.get((name, q["agent"][0]))
        if not queue:
            raise SystemExit(f"{name}: HTTP 500")
        return queue.pop(0)

    def paths(self, prefix):
        return [p for _, p in self.api_calls if p.startswith(prefix)]


class WatchBoardTest(unittest.TestCase):
    NOW = 1_800_000_000

    def setUp(self):
        self.board = mvm.WatchBoard()

    def fleet(self, vms, statuses=None, events=None):
        f = FakeFleet(vms, statuses, events)
        for target, value in (("session_names", lambda: sorted(f.vms)), ("load_session", lambda n: {"terminated": False}),
                              ("vm_state", f.vm_state), ("vm_api", f.vm_api)):
            p = mock.patch.object(mvm, target, value)
            p.start()
            self.addCleanup(p.stop)
        return f

    def status(self, agents=None, **kw):
        st = {"bootstrap": "ready", "idleSeconds": 1, "oomKills": 0,
              "memory": {"usedMiB": 1000, "totalMiB": 3891, "availableMiB": 2800, "top": []}}
        if agents is not None:
            st["agents"] = agents
        st.update(kw)
        return st

    def collect(self, offset=0):
        return mvm.watch_collect(self.board, self.NOW + offset)

    def test_waiting_agent_gets_note_with_message_and_elapsed(self):
        self.fleet({"a2": "RUNNING"}, {"a2": self.status({"claude": {
            "state": "waiting", "since": self.NOW - 180, "message": "Bash の実行の許可を求めています"}})})
        [row] = self.collect()
        self.assertEqual((row["state"], row["sinceSeconds"]), ("waiting", 180))
        self.assertEqual(row["notes"], ["a2 (claude): 許可・入力待ち: Bash の実行の許可を求めています"])
        self.assertEqual(mvm.watch_level(row), "yellow")

    def test_error_agent_gets_note_with_hint(self):
        self.fleet({"a3": "RUNNING"}, {"a3": self.status({"claude": {
            "state": "error", "since": self.NOW - 5, "error": "authentication_failed", "message": "401"}})})
        [row] = self.collect()
        self.assertEqual(row["state"], "error")
        self.assertIn("a3 (claude): エラー authentication_failed: 401", row["notes"][0])
        self.assertIn(mvm.agent_error_hint("a3", "authentication_failed"), row["notes"][0])
        self.assertEqual(mvm.watch_level(row), "red")

    def test_suspended_vm_shows_vm_state_and_calls_no_api(self):
        f = self.fleet({"a4": "SUSPENDED"}, {"a4": self.status({})})
        [row] = self.collect()
        self.assertEqual((row["vm"], row["state"], row["memory"], row["lastEvent"]), ("SUSPENDED", "SUSPENDED", None, ""))
        self.assertEqual(f.api_calls, [], "SUSPENDED の VM に /status も /events も呼ばない (起こして課金されるため)")
        self.assertEqual(row["notes"], [])

    def test_unknown_vm_shows_reason_and_next_step(self):
        f = self.fleet({"a5": "UNKNOWN"})
        with mock.patch.dict(mvm._state_errors, {"a5": "AWS の認証が切れています → aws sso login でログインし直してください"}):
            [row] = self.collect()
        self.assertEqual(row["state"], "UNKNOWN")
        self.assertEqual(row["notes"], ["a5: AWS の認証が切れています → aws sso login でログインし直してください"])
        self.assertEqual(f.api_calls, [])
        self.assertEqual(mvm.watch_level(row), "yellow")

    def test_unknown_without_reason_uses_generic_hint(self):
        self.fleet({"a5": "UNKNOWN"})
        mvm._state_errors.pop("a5", None)
        [row] = self.collect()
        self.assertIn(mvm.aws_error_hint(""), row["notes"][0])

    def test_old_image_without_agents_is_working_and_has_no_events(self):
        f = self.fleet({"a6": "RUNNING"}, {"a6": self.status()})
        [row] = self.collect()
        self.assertEqual((row["state"], row["agents"], row["sinceSeconds"], row["lastEvent"]), ("working", {}, None, ""))
        self.assertEqual(f.paths("/events"), [])

    def test_stalled_note_has_idle_seconds(self):
        self.fleet({"a7": "RUNNING"}, {"a7": self.status({"claude": {"state": "working", "since": self.NOW - 900}},
                                                          idleSeconds=400)})
        [row] = self.collect()
        self.assertEqual(row["state"], "stalled")
        self.assertIn("400 秒", row["notes"][0])

    def test_failed_bootstrap_and_oom_are_noted(self):
        self.fleet({"a8": "RUNNING"}, {"a8": self.status({}, bootstrap="failed: setup", oomKills=3)})
        [row] = self.collect()
        self.assertEqual((row["state"], row["oomKills"]), ("failed", 3))
        self.assertTrue(any("起動の準備に失敗" in n for n in row["notes"]))
        self.assertTrue(any("3 回強制終了" in n for n in row["notes"]))

    def test_peak_memory_only_goes_up(self):
        st = {"a1": self.status({"claude": {"state": "working", "since": self.NOW}})}
        self.fleet({"a1": "RUNNING"}, st)
        peaks = []
        for used in (1000, 1500, 900):
            st["a1"]["memory"]["usedMiB"] = used
            [row] = self.collect()
            peaks.append((row["memory"]["usedMiB"], row["memory"]["peakMiB"]))
        self.assertEqual(peaks, [(1000, 1000), (1500, 1500), (900, 1500)])

    def test_get_microvm_is_called_once_per_minute_per_vm(self):
        agents = {"claude": {"state": "working", "since": self.NOW}}
        f = self.fleet({"a1": "RUNNING", "a2": "RUNNING"}, {"a1": self.status(agents), "a2": self.status(agents)})
        for t in range(0, 60, 10):
            self.collect(t)
        self.assertEqual(sorted(f.vm_calls), ["a1", "a2"], "60 秒の間は 1 台 1 回だけ")
        self.assertEqual(len(f.paths("/status")), 12, "その間も /status は毎回呼ぶ")
        self.collect(60)
        self.assertEqual(len(f.vm_calls), 4, "60 秒たったら呼び直す")

    def test_status_failure_triggers_get_microvm_next_round(self):
        agents = {"claude": {"state": "working", "since": self.NOW}}
        f = self.fleet({"a1": "RUNNING"}, {"a1": self.status(agents)})
        self.collect(0)
        f.statuses["a1"] = SystemExit("a1: 接続できません")
        [row] = self.collect(10)
        self.assertEqual(row["state"], "unreachable")
        self.assertEqual(len(row["notes"]), 1, "応答しない台は要対応として理由を出す")
        self.assertIn("接続できません", row["notes"][0])
        self.assertEqual(mvm.watch_level(row), "yellow")
        self.assertEqual(len(f.vm_calls), 1)
        f.statuses["a1"] = self.status(agents)
        [row] = self.collect(20)
        self.assertEqual(len(f.vm_calls), 2, "失敗の次の回は get-microvm で確かめ直す")
        self.assertEqual(row["state"], "working")
        self.collect(30)
        self.assertEqual(len(f.vm_calls), 2, "確かめた後はまた 60 秒空ける")

    def test_suspended_state_is_reused_for_a_minute_then_rechecked(self):
        f = self.fleet({"a1": "SUSPENDED"})
        for t in (0, 10, 50):
            self.collect(t)
        self.assertEqual(len(f.vm_calls), 1)
        f.vms["a1"] = "RUNNING"
        f.statuses["a1"] = self.status({"claude": {"state": "working", "since": self.NOW}})
        [row] = self.collect(60)
        self.assertEqual((len(f.vm_calls), row["state"]), (2, "working"))

    def test_terminated_sessions_are_skipped(self):
        self.fleet({"a1": "RUNNING", "a2": "RUNNING"}, {"a1": self.status(), "a2": self.status()})
        with mock.patch.object(mvm, "load_session", lambda n: {"terminated": n == "a2"}):
            self.assertEqual([r["name"] for r in self.collect()], ["a1"])

    def test_last_event_is_fetched_as_tail_then_by_offset(self):
        agents = {"claude": {"state": "working", "since": self.NOW}}
        f = self.fleet({"a1": "RUNNING"}, {"a1": self.status(agents)}, {("a1", "claude"): [
            {"file": "/t/1.jsonl", "offset": 10, "events": [{"kind": "tool", "name": "Bash", "text": "pytest -x"}]},
            {"file": "/t/1.jsonl", "offset": 40, "events": [{"kind": "output", "text": "1 passed\n2 failed"},
                                                            {"kind": "text", "text": "原因は\nトークンの期限切れでした"}]},
            {"file": "/t/1.jsonl", "offset": 40, "events": []},
            {"file": "/t/2.jsonl", "offset": 5, "reset": True, "events": []},
        ]})
        texts = [self.collect(t)[0]["lastEvent"] for t in (0, 10, 20, 30)]
        self.assertEqual(texts, ["実行: Bash: pytest -x", "応答: 原因は トークンの期限切れでした",
                                 "応答: 原因は トークンの期限切れでした", ""], "新しい会話になったら空に戻す")
        q = [urllib.parse.parse_qs(urllib.parse.urlparse(p).query, keep_blank_values=True) for p in f.paths("/events")]
        self.assertEqual((q[0]["file"], q[0]["offset"], q[0]["tail"]), ([""], ["0"], ["1"]))
        self.assertEqual((q[1]["file"], q[1]["offset"]), (["/t/1.jsonl"], ["10"]))
        self.assertEqual((q[2]["file"], q[2]["offset"]), (["/t/1.jsonl"], ["40"]))

    def test_events_failure_keeps_previous_event_and_row(self):
        agents = {"claude": {"state": "working", "since": self.NOW}}
        self.fleet({"a1": "RUNNING"}, {"a1": self.status(agents)}, {("a1", "claude"): [
            {"file": "/t/1.jsonl", "offset": 10, "events": [{"kind": "text", "text": "こんにちは"}]}]})
        self.assertEqual(self.collect(0)[0]["lastEvent"], "応答: こんにちは")
        self.assertEqual(self.collect(10)[0]["lastEvent"], "応答: こんにちは", "取得に失敗しても前回の出来事を残す")

    def test_both_agents_prefer_the_one_whose_state_changed(self):
        def both(codex_state):
            return self.status({"claude": {"state": "working", "since": self.NOW},
                                "codex": {"state": codex_state, "since": self.NOW}})
        f = self.fleet({"a1": "RUNNING"}, {"a1": both("working")}, {
            ("a1", "claude"): [{"file": "/c", "offset": 1, "events": [{"kind": "text", "text": "claude の応答"}]},
                               {"file": "/c", "offset": 2, "events": []}],
            ("a1", "codex"): [{"file": "/x", "offset": 1, "events": [{"kind": "text", "text": "codex の応答"}]},
                              {"file": "/x", "offset": 2, "events": []}]})
        self.assertEqual(self.collect(0)[0]["lastEvent"], "応答: claude の応答", "分からなければ claude")
        f.statuses["a1"] = both("done")
        self.assertEqual(self.collect(10)[0]["lastEvent"], "応答: codex の応答", "状態が変わった方")


class WatchLogTest(unittest.TestCase):
    def test_header_written_once_and_rows_appended(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "sub" / "mem.csv"
            mvm.watch_open_log(path)
            mvm.watch_open_log(path)
            rows = [watch_row("a1", "working", memory={"usedMiB": 100, "totalMiB": 3891, "peakMiB": 150}, oomKills=1),
                    watch_row("a2", "SUSPENDED", vm="SUSPENDED")]
            mvm.watch_append_log(path, rows, 1_800_000_000)
            mvm.watch_append_log(path, rows[:1], 1_800_000_010)
            lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "time,name,vm,state,usedMiB,totalMiB,peakMiB,oomKills")
        self.assertEqual(len(lines), 4)
        self.assertTrue(lines[1].endswith(",a1,RUNNING,working,100,3891,150,1"))
        self.assertTrue(lines[2].endswith(",a2,SUSPENDED,SUSPENDED,,,,0"))

    def test_append_writes_header_when_file_vanished(self):
        with tempfile.TemporaryDirectory() as t:
            path = Path(t) / "mem.csv"
            mvm.watch_append_log(path, [watch_row("a1", "working")], 1_800_000_000)
            self.assertTrue(path.read_text(encoding="utf-8").startswith("time,name,"))

    def test_unwritable_path_stops_with_next_step(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(SystemExit) as cm:
                mvm.watch_open_log(Path(t))
            self.assertIn("別のパス", str(cm.exception))
            with self.assertRaises(SystemExit) as cm:
                mvm.watch_append_log(Path(t), [], 0)
            self.assertIn("閉じてから", str(cm.exception))


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

    def test_aws_auth_methods(self):
        # Bedrock の API キー / Mantle / Codex の Bedrock
        self.env["PMVA_STUB_QUOTA_GB"] = "100"
        self.mvm("up", "k1", "--claude-auth", "bedrock-api-key", "--bedrock-region", "us-east-1")
        self.mvm("up", "m1", "--claude-auth", "mantle")
        self.mvm("up", "c1", "--agent", "codex", "--codex-auth", "bedrock")
        self.mvm("up", "c2", "--agent", "codex", "--codex-auth", "bedrock-api-key")
        k1, m1, c1, c2 = self.payloads()
        self.assertEqual((k1["claudeAuth"], k1["claudeSecret"], k1["bedrockRegion"]),
                         ("bedrock-api-key", "pmva/claude-bedrock-api-key", "us-east-1"))
        self.assertEqual(m1["claudeAuth"], "mantle")
        self.assertNotIn("claudeSecret", m1)
        self.assertEqual((c1["codexAuth"], c1["bedrockRegion"]), ("bedrock", "ap-northeast-1"))
        self.assertNotIn("codexSecret", c1)
        self.assertEqual(c2["codexSecret"], "pmva/codex-bedrock-api-key")
        for p in (k1, m1, c1, c2):
            self.assertNotIn("anthropicAwsWorkspaceId", p)

    def test_claude_platform_on_aws_needs_workspace(self):
        rc, _, err = self.mvm("up", "w1", "--claude-auth", "anthropic-aws", check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("ANTHROPIC_AWS_WORKSPACE_ID", err)
        cfg = Path(self.env["PMVA_CONFIG"])
        cfg.write_text(cfg.read_text(encoding="utf-8") + "ANTHROPIC_AWS_WORKSPACE_ID=bad id\n", encoding="utf-8")
        rc, _, err = self.mvm("up", "w1", "--claude-auth", "anthropic-aws", check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("wrkspc_", err)
        cfg.write_text(cfg.read_text(encoding="utf-8") + "ANTHROPIC_AWS_WORKSPACE_ID=wrkspc_01Test\n", encoding="utf-8")
        self.mvm("up", "w2", "--claude-auth", "anthropic-aws")
        cfg.write_text(cfg.read_text(encoding="utf-8") + "ANTHROPIC_AWS_REGION=us-east-1\n", encoding="utf-8")
        self.mvm("up", "w3", "--claude-auth", "anthropic-aws-api-key")
        w2, w3 = self.payloads()
        self.assertEqual((w2["anthropicAwsWorkspaceId"], w2["anthropicAwsRegion"]), ("wrkspc_01Test", "ap-northeast-1"))
        self.assertNotIn("claudeSecret", w2)
        self.assertEqual((w3["anthropicAwsRegion"], w3["claudeSecret"]), ("us-east-1", "pmva/anthropic-aws-api-key"))

    def test_bedrock_api_key_secrets_follow_role_prefixes(self):
        rc, _, _ = self.mvm("secret", "set", "codex-bedrock-api-key", "--name", "pmva/claude-x", input=b"k", check=False)
        self.assertNotEqual(rc, 0)
        self.mvm("secret", "set", "codex-bedrock-api-key", input=b"k")
        self.mvm("secret", "set", "claude-bedrock-api-key", input=b"k")
        self.mvm("secret", "set", "anthropic-aws-api-key", input=b"k")
        calls = [json.loads(l) for l in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        created = [c[c.index("--name") + 1] for c in calls if c[:2] == ["secretsmanager", "create-secret"]]
        self.assertEqual(created, ["pmva/codex-bedrock-api-key", "pmva/claude-bedrock-api-key", "pmva/anthropic-aws-api-key"])

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
        self.assertIn("EnableAnthropicAws=false", deploy, "Claude Platform on AWS の権限は既定では付けない")
        cfg.write_text(cfg.read_text(encoding="utf-8") + "ENABLE_ANTHROPIC_AWS=true\n", encoding="utf-8")
        self.mvm("infra", "deploy", check=False)
        calls = [json.loads(l) for l in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        deploy = [c for c in calls if c[:2] == ["cloudformation", "deploy"]][-1]
        self.assertIn("EnableAnthropicAws=true", deploy)

    def test_infra_deploy_minimal_template(self):
        cfg = Path(self.env["PMVA_CONFIG"])
        cfg.write_text(cfg.read_text(encoding="utf-8") + "INFRA_TEMPLATE=minimal\nNIGHTLY_STOP_TIME=23:30\n",
                       encoding="utf-8")
        _, _, err = self.mvm("infra", "deploy")
        calls = [json.loads(l) for l in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        deploy = next(c for c in calls if c[:2] == ["cloudformation", "deploy"])
        self.assertTrue(deploy[deploy.index("--template-file") + 1].endswith("cloudformation-minimal.yaml"))
        overrides = {x.split("=", 1)[0] for x in deploy if "=" in x and not x.startswith("-")}
        self.assertEqual(overrides, {"Prefix", "EnableBedrock", "EnableAnthropicAws"}, "テンプレートに無いパラメータは渡さない")
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

    def test_memory_option_in_payload(self):
        self.env["PMVA_STUB_QUOTA_GB"] = "64"
        repo = "https://x-access-token:ghp_FAKEFAKEFAKE@github.com/Owner/Repo.git"
        self.mvm("up", "m0", "--repo", "https://github.com/o/r.git")          # 既定では共有しない
        self.mvm("up", "m1", "--repo", repo, "--memory")
        rc, _, err = self.mvm("up", "m2", "--memory")                          # --repo が無ければ共有しない
        self.assertIn("--repo が無いのでメモリは共有しません", err)
        cfg = Path(self.env["PMVA_CONFIG"])
        cfg.write_text(cfg.read_text(encoding="utf-8") + "MEMORY_SYNC=true\n", encoding="utf-8")
        self.mvm("up", "m3", "--repo", "git@github.com:Owner/Repo.git")
        self.mvm("up", "m4", "--repo", "https://github.com/o/r.git", "--no-memory")
        _, err = self.mvm("up", "m5", "--repo", "/local/path/repo")[1:]
        self.assertIn("メモリは共有しません", err)
        m0, m1, m2, m3, m4, m5 = self.payloads()
        self.assertNotIn("memory", m0)
        self.assertEqual(m1["memory"], "s3://pmva-bucket/memory/github.com/owner/repo/")
        self.assertNotIn("ghp_", m1["memory"], "URL のトークンは使わない")
        self.assertNotIn("memory", m2)
        self.assertEqual(m3["memory"], "s3://pmva-bucket/memory/github.com/owner/repo/")
        self.assertNotIn("memory", m4)
        self.assertNotIn("memory", m5)

    def memory_repo(self):
        """origin 付きの git リポジトリと、メモリのフォルダ (autoMemoryDirectory で場所を固定する)。"""
        repo = Path(self.tmp.name) / "repo"
        (repo / ".claude").mkdir(parents=True)
        mem = Path(self.tmp.name) / "mem"
        mem.mkdir()
        (repo / ".claude" / "settings.local.json").write_text(
            json.dumps({"autoMemoryDirectory": str(mem)}), encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(repo)], check=True, env=self.env)
        subprocess.run(["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/Owner/Repo.git"],
                       check=True, env=self.env)
        return repo, mem

    def test_memory_push_pull_status(self):
        repo, mem = self.memory_repo()
        (mem / "user_role.md").write_bytes(b"---\nname: role\n---\r\nI am a tester\r\n")
        (mem / "private.md").write_text("---\nshare: false\n---\nmine\n", encoding="utf-8")
        (mem / "MEMORY.md").write_text("- [Role](user_role.md) - role\n- [P](private.md) - p\n", encoding="utf-8")
        rc, out, err = self.mvm("memory", "push", "--dir", str(repo))
        s3 = self.stub_dir / "s3" / "pmva-bucket" / "memory" / "github.com" / "owner" / "repo"
        self.assertEqual((s3 / "user_role.md").read_bytes(), b"---\nname: role\n---\nI am a tester\n")
        self.assertEqual((s3 / "MEMORY.md").read_text(encoding="utf-8"), "- [Role](user_role.md) - role\n")
        self.assertFalse((s3 / "private.md").exists())
        self.assertIn("s3://pmva-bucket/memory/github.com/owner/repo/", err)
        self.assertIn("送信 1", out)
        self.assertIn("MEMORY_SYNC=true", err, "VM にも使わせる方法を案内する")
        self.assertIn("install-hooks", err)
        self.assertTrue((Path(self.env["PMVA_STATE_DIR"]) / "memory" / "github.com__owner__repo.json").is_file())
        rc, out, err = self.mvm("memory", "push", "--dir", str(repo))
        self.assertNotIn("MEMORY_SYNC=true", err, "案内は最初の 1 回だけ")
        rc, out, _ = self.mvm("memory", "status", "--dir", str(repo))
        self.assertRegex(out, r"user_role\.md +同期済み")
        self.assertRegex(out, r"private\.md +送らない")
        # 別の PC で pull する: ファイルが無い状態から取得し、索引にも足される
        (mem / "user_role.md").unlink()
        (mem / "MEMORY.md").unlink()
        (Path(self.env["PMVA_STATE_DIR"]) / "memory" / "github.com__owner__repo.json").unlink()
        rc, out, _ = self.mvm("memory", "pull", "--dir", str(repo), "--dry-run")
        self.assertFalse((mem / "user_role.md").exists())
        self.mvm("memory", "sync", "--dir", str(repo))
        self.assertEqual((mem / "user_role.md").read_bytes(), b"---\nname: role\n---\nI am a tester\n")
        self.assertIn("(user_role.md)", (mem / "MEMORY.md").read_text(encoding="utf-8"))
        calls = [json.loads(l) for l in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(c[:2] == ["s3api", "list-objects-v2"] for c in calls))

    def test_memory_log_revert_rm(self):
        repo, mem = self.memory_repo()
        s3 = self.stub_dir / "s3" / "pmva-bucket" / "memory" / "github.com" / "owner" / "repo"
        (mem / "user_role.md").write_text("v1\n", encoding="utf-8")
        (mem / "MEMORY.md").write_text("- [Role](user_role.md) - role\n", encoding="utf-8")
        self.mvm("memory", "push", "--dir", str(repo))
        (mem / "user_role.md").write_text("v2\n", encoding="utf-8")
        self.mvm("memory", "push", "--dir", str(repo))
        # log: 位置引数 (名前)、--origin、--since、--json が渡る
        rc, out, err = self.mvm("memory", "log", "--json", "--origin", "local", "--since", "1d", "--dir", str(repo))
        entries = json.loads(out)
        self.assertEqual([(e["origin"], e["action"], e["name"]) for e in entries], [("local", "write", "user_role.md")] * 2)
        self.assertIn("s3://pmva-bucket/memory/github.com/owner/repo/", err)
        rc, out, _ = self.mvm("memory", "log", "user_role.md", "--dir", str(repo))
        self.assertEqual(len(out.strip().splitlines()), 2)
        self.assertRegex(out, r"(?m)^\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ  local +書いた +user_role\.md\r?$")
        rc, out, _ = self.mvm("memory", "log", "--origin", "vm-9", "--dir", str(repo))
        self.assertIn("履歴はありません", out)
        rc, _, err = self.mvm("memory", "log", "--since", "bogus", "--dir", str(repo), check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("--since", err)
        # revert: --dry-run は何も書き換えず、--to が渡る
        rc, out, _ = self.mvm("memory", "revert", "user_role.md", "--dry-run", "--dir", str(repo))
        self.assertIn("[dry-run] 戻す版", out)
        self.assertEqual((s3 / "user_role.md").read_bytes(), b"v2\n")
        rc, _, err = self.mvm("memory", "revert", "user_role.md", "--to", "20000101T000000Z", "--dir", str(repo), check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("以前の履歴がありません", err)
        self.mvm("memory", "revert", "user_role.md", "--dir", str(repo))
        self.assertEqual((s3 / "user_role.md").read_bytes(), b"v1\n")
        self.assertEqual((mem / "user_role.md").read_bytes(), b"v1\n")
        # 手元の未同期の変更で止まり、--force で戻せる
        (mem / "user_role.md").write_text("unsynced\n", encoding="utf-8")
        rc, _, err = self.mvm("memory", "revert", "user_role.md", "--dir", str(repo), check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("--force", err)
        self.mvm("memory", "revert", "user_role.md", "--force", "--dir", str(repo))
        self.assertEqual((mem / "user_role.md").read_bytes(), b"v2\n")
        # rm: --dry-run は何も消さない。本番は印を書いて S3 と手元から消す
        self.mvm("memory", "rm", "user_role.md", "--dry-run", "--dir", str(repo))
        self.assertTrue((mem / "user_role.md").exists() and (s3 / "user_role.md").exists())
        self.assertFalse((s3 / ".deleted.json").exists())
        rc, out, err = self.mvm("memory", "rm", "user_role.md", "--dir", str(repo))
        self.assertFalse((mem / "user_role.md").exists())
        self.assertFalse((s3 / "user_role.md").exists())
        self.assertNotIn("user_role.md", (s3 / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertNotIn("user_role.md", (mem / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertIn("user_role.md", json.loads((s3 / ".deleted.json").read_text(encoding="utf-8"))["files"])
        self.assertNotIn("install-hooks", err, "rm では同期の案内を出さない")
        rc, out, _ = self.mvm("memory", "log", "--json", "--dir", str(repo))
        self.assertEqual([e["action"] for e in json.loads(out)].count("delete"), 1)
        rc, _, err = self.mvm("memory", "rm", "user_role.md", "--dir", str(repo), check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("そのメモリはありません", err)
        rc, _, err = self.mvm("memory", "rm", "--dir", str(repo), check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn(".md", err)

    def test_memory_hooks_install_and_uninstall(self):
        repo, mem = self.memory_repo()
        settings_path = repo / ".claude" / "settings.local.json"
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        # 「memory hook」を含む利用者自身のフックも残す
        mine = {"hooks": [{"type": "command", "command": "echo mine; echo memory hook done"}]}
        settings["hooks"] = {"PostToolUse": [mine]}
        settings_path.write_text(json.dumps(settings), encoding="utf-8")
        for _ in range(2):          # 入れ直しても重複しない
            rc, _, err = self.mvm("memory", "install-hooks", "--dir", str(repo))
        # Windows の CI では一時フォルダが短い名前 (RUNNER~1) になり、git の返す名前と違うので末尾で比べる
        self.assertIn(str(Path("repo") / ".claude" / "settings.local.json"), err)
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(data["autoMemoryDirectory"], str(mem), "ほかの設定は残す")
        hooks = data["hooks"]
        self.assertEqual(hooks["PostToolUse"][0], mine, "利用者のフックは残す")
        commands = {ev: [h["command"] for e in hooks[ev] for h in e["hooks"] if "mvm.py" in h["command"]]
                    for ev in ("SessionStart", "UserPromptSubmit", "PostToolUse")}
        self.assertEqual({ev: len(c) for ev, c in commands.items()},
                         {"SessionStart": 1, "UserPromptSubmit": 1, "PostToolUse": 1})
        self.assertIn(f'"{MVM.as_posix()}" memory hook session-start', commands["SessionStart"][0])
        self.assertIn(f'"{Path(sys.executable).as_posix()}"', commands["SessionStart"][0])
        self.assertIn("memory hook prompt --min-interval 60", commands["UserPromptSubmit"][0])
        (ours,) = [e for e in hooks["PostToolUse"] if e is not hooks["PostToolUse"][0]]
        self.assertEqual(ours["matcher"], "Write|Edit")
        self.assertTrue(ours["hooks"][0]["async"], "送信は会話を待たせない")
        self.mvm("memory", "uninstall-hooks", "--dir", str(repo))
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        self.assertEqual(data["hooks"], {"PostToolUse": [mine]})
        self.assertEqual(data["autoMemoryDirectory"], str(mem))
        settings_path.unlink()          # 設定が無いときに外しても、ファイルを作らない
        self.mvm("memory", "uninstall-hooks", "--dir", str(repo))
        self.assertFalse(settings_path.exists())
        settings_path.write_text("{broken", encoding="utf-8")
        rc, _, err = self.mvm("memory", "install-hooks", "--dir", str(repo), check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("JSON", err)
        self.assertEqual(settings_path.read_text(encoding="utf-8"), "{broken", "壊れた設定は書き換えない")

    def test_memory_hook_called_by_claude_code(self):
        repo, mem = self.memory_repo()
        s3 = self.stub_dir / "s3" / "pmva-bucket" / "memory" / "github.com" / "owner" / "repo"
        (mem / "user_role.md").write_text("tester\n", encoding="utf-8")
        event = {"hook_event_name": "PostToolUse", "cwd": str(repo), "tool_name": "Write",
                 "tool_input": {"file_path": str(mem / "user_role.md")}}
        rc, out, _ = self.mvm("memory", "hook", "post-tool", input=json.dumps(event).encode("utf-8"))
        self.assertEqual((rc, out), (0, ""))
        self.assertEqual((s3 / "user_role.md").read_bytes(), b"tester\n")
        calls = [json.loads(l) for l in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        puts = [c for c in calls if c[:2] == ["s3api", "put-object"]]
        (put,) = [c for c in puts if "/.history/" not in c[c.index("--key") + 1]]
        self.assertEqual(put[put.index("--if-none-match") + 1], "*")
        (history,) = [c for c in puts if "/.history/" in c[c.index("--key") + 1]]
        self.assertRegex(history[history.index("--key") + 1], r"/\.history/user_role\.md/\d{8}T\d{6}Z-local-[0-9a-f]{8}\.md$")
        self.assertEqual(history[history.index("--metadata") + 1], "pmva-origin=local")
        # 他のマシンが書いたメモリが、会話の開始時に届く
        (s3 / "project_vm.md").write_text("VM で見つけた手順\n", encoding="utf-8")
        start = {"hook_event_name": "SessionStart", "cwd": str(repo), "source": "startup"}
        rc, out, _ = self.mvm("memory", "hook", "session-start", input=json.dumps(start).encode("utf-8"))
        self.assertEqual(rc, 0)
        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        self.assertIn("VM で見つけた手順", context)
        self.assertEqual((mem / "project_vm.md").read_text(encoding="utf-8"), "VM で見つけた手順\n")
        # 失敗しても 0 で終わり、標準出力 (会話に入る) には何も書かない
        rc, out, _ = self.mvm("memory", "hook", "prompt", input=json.dumps(start).encode("utf-8"),
                              env={**self.env, "PMVA_STUB_ERROR": "Could not connect to the endpoint URL"})
        self.assertEqual((rc, out), (0, ""))
        rc, out, _ = self.mvm("memory", "hook", "prompt", input=b"{}", env={**self.env, "PMVA_CONFIG": "/no/such/config.env"})
        self.assertEqual((rc, out), (0, ""))
        rc, out, _ = self.mvm("memory", "hook", input=b"{}")
        self.assertEqual((rc, out), (0, ""))
        # メモリ以外のファイルの書き込みでは AWS を呼ばない
        before = (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8")
        other = {"cwd": str(repo), "tool_input": {"file_path": str(repo / "main.py")}}
        self.assertEqual(self.mvm("memory", "hook", "post-tool", input=json.dumps(other).encode("utf-8"))[0], 0)
        self.assertEqual((self.stub_dir / "calls.jsonl").read_text(encoding="utf-8"), before)

    def test_memory_errors_say_what_to_do(self):
        repo, _ = self.memory_repo()
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", "/srv/local/repo.git"], check=True, env=self.env)
        rc, _, err = self.mvm("memory", "status", "--dir", str(repo), check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("origin", err)
        rc, _, err = self.mvm("memory", "status", "--dir", self.tmp.name, check=False)   # git リポジトリではない
        self.assertNotEqual(rc, 0)
        subprocess.run(["git", "-C", str(repo), "remote", "set-url", "origin", "https://github.com/Owner/Repo.git"],
                       check=True, env=self.env)
        rc, _, err = self.mvm("memory", "push", "--dir", str(repo), check=False,
                              env={**self.env, "PMVA_STUB_ERROR": "An error occurred (AccessDenied) when calling the ListObjectsV2 operation",
                                   "PMVA_STUB_ERROR_CMDS": "s3api list-objects-v2"})
        self.assertNotEqual(rc, 0)
        self.assertIn("mvm infra deploy", err)

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

    def test_renew_picks_up_ssh_key_created_after_up(self):
        # mvm ssh は「SSH_PUBLIC_KEY_FILE を設定して mvm renew」と案内する。鍵なしで up した後に鍵を作ったら、
        # renew で鍵付きに起動し直せること
        key = self.home / ".ssh" / "id_ed25519.pub"
        key.unlink()
        self.mvm("up", "k1")
        self.assertNotIn("sshPublicKey", self.payloads()[-1])
        key.write_text("ssh-ed25519 AAAANEW new@pc\n", encoding="utf-8")
        self.mvm("stop", "k1", "--no-save")
        self.mvm("renew", "k1")
        self.assertEqual(self.payloads()[-1]["sshPublicKey"], "ssh-ed25519 AAAANEW new@pc")
        session = Path(self.env["PMVA_STATE_DIR"]) / "sessions" / "k1.json"
        self.assertEqual(json.loads(session.read_text(encoding="utf-8"))["payload"]["sshPublicKey"],
                         "ssh-ed25519 AAAANEW new@pc", "mvm ssh が見るセッションにも鍵が入る")
        # 鍵ファイルが見つからなくなっても、前回の鍵はそのまま使う
        key.unlink()
        self.mvm("stop", "k1", "--no-save")
        self.mvm("renew", "k1")
        self.assertEqual(self.payloads()[-1]["sshPublicKey"], "ssh-ed25519 AAAANEW new@pc")

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

    def test_secret_set_removes_whitespace_inside_tokens(self):
        # 端末の折り返しごとコピーすると、トークンの途中に空白や改行が入る。トークンに空白は含まれないので取り除く
        _, _, err = self.mvm("secret", "set", "claude-oauth-token", input=b"sk-ant-oat01-ab\ncd ef\r\n")
        self.assertEqual((self.stub_dir / "secret-pmva_claude-oauth-token").read_text(encoding="utf-8"),
                         "sk-ant-oat01-abcdef")
        self.assertIn("空白や改行を 2 個取り除きました", err)
        self.assertNotIn("abcdef", err, "値はログに出さない")
        self.mvm("secret", "set", "github-token", input=b"ghp_ab cd")
        self.assertEqual((self.stub_dir / "secret-pmva_github-token").read_text(encoding="utf-8"), "ghp_abcd")
        # JSON や URL の種類は中身を変えない
        gw = '{"baseUrl": "https://gw.example.com", "authToken": "t"}'
        self.mvm("secret", "set", "anthropic-gateway", input=gw.encode())
        self.assertEqual((self.stub_dir / "secret-pmva_anthropic-gateway").read_text(encoding="utf-8"), gw)

    def test_secret_set_warns_when_oauth_token_looks_wrong(self):
        _, _, err = self.mvm("secret", "set", "claude-oauth-token", input=b"sk-ant-api03-x")
        self.assertIn("sk-ant-oat01-", err)
        self.assertIn("claude setup-token", err)
        _, _, err = self.mvm("secret", "set", "claude-oauth-token", input=b"sk-ant-oat01-x")
        self.assertNotIn("claude setup-token", err)

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

    def status(self, name="a1", idle=5, **agents):
        return {"name": name, "agent": "both" if len(agents) > 1 else "claude", "bootstrap": "ready",
                "idleSeconds": idle, "windows": [],
                "agents": {a: (v if isinstance(v, dict) else {"state": v}) for a, v in agents.items()}}

    def test_wait_until_agent_finishes(self):
        api = StatusApi()
        try:
            self.mvm("up", "a1")
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            api.statuses = [self.status(claude="starting"), self.status(claude="working"), self.status(claude="done")]
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", "--json", env=env)
            r = json.loads(out)
            self.assertEqual(rc, 0)
            self.assertTrue(r["finished"])
            self.assertEqual(r["sessions"][0]["state"], "done")
            self.assertEqual(api.status_calls, 3, "終わるまで状態を確かめ続ける")
            # 許可待ちは、その内容と一緒に返す
            api.statuses = [self.status(claude={"state": "waiting", "message": "Claude needs your permission to use Bash"})]
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", env=env)
            self.assertIn("a1               waiting  (claude: 許可・入力待ち)", out)
            self.assertIn("claude: Claude needs your permission to use Bash", out)
            # API のエラー (認証の失敗など) で止まったら、その内容と次にやることを返す
            message = "Failed to authenticate. API Error: 401 OAuth access token is invalid."
            api.statuses = [self.status(claude={"state": "error", "error": "authentication_failed", "message": message})]
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", env=env)
            self.assertEqual(rc, 0)
            self.assertIn("a1               error  (claude: エラーで止まった)", out)
            self.assertIn(f"claude: authentication_failed: {message}", out)
            self.assertIn("mvm secret set", out)
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", "--json", env=env)
            self.assertEqual(json.loads(out)["sessions"][0]["state"], "error")
            # 時間切れなら終了コード 2 で返る (司令塔のエージェントは呼び直す)
            api.statuses = [self.status(claude="working")]
            rc, _, err = self.mvm("wait", "a1", "--timeout", "0.3", "--interval", "0.05", env=env, check=False)
            self.assertEqual(rc, 2)
            self.assertIn("時間切れです。まだ終わっていないセッション: a1 (working)", err)
            # 作業中のはずなのに画面も CPU も長く動かなければ、止まっているとみなして返る
            api.statuses = [self.status(idle=400, claude="working")]
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", "--json", env=env)
            self.assertEqual(json.loads(out)["sessions"][0]["state"], "stalled")
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", env=env)
            self.assertIn("(画面と CPU が 400 秒動いていません)", out)
            # AWS から状態を取れないときは、終わったとはみなさずに待ち続ける
            rc, out, _ = self.mvm("wait", "a1", "--timeout", "0.3", "--interval", "0.05", "--json",
                                  env=dict(env, PMVA_STUB_GET_DENIED="1"), check=False)
            self.assertEqual((rc, json.loads(out)["sessions"][0]["state"]), (2, "unreachable"))
            rc, _, _ = self.mvm("wait", "a1", "--idle", "0", "--timeout", "0.2", "--interval", "0.05", env=env, check=False)
            self.assertEqual(rc, 2, "--idle 0 なら止まっているとはみなさない")
        finally:
            api.shutdown()

    def test_wait_multiple_sessions(self):
        api = StatusApi()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "a1", "--agent", "both", env=env)
            self.mvm("up", "a2", env=env)
            # --agent both は両方のエージェントが終わるまで待つ。終わり方が違えば、人の対応が要るほうを代表にする
            api.statuses = [self.status(claude="done", codex="working"), self.status(claude="done", codex="exited")]
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", "--json", env=env)
            self.assertEqual(json.loads(out)["sessions"][0]["state"], "exited")
            api.statuses = [self.status(claude="error", codex="exited")]
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", "--json", env=env)
            self.assertEqual(json.loads(out)["sessions"][0]["state"], "error", "エラーは終了より優先して知らせる")
            # 名前を省略すると動いている全セッション。サスペンド中の VM は起こさずに (API を呼ばずに) 終わり扱い
            (self.stub_dir / "microvm-0000.suspended").touch()
            api.statuses = [self.status(claude="working")]
            rc, out, _ = self.mvm("wait", "--any", "--interval", "0.05", "--json", env=env)
            r = {x["name"]: x for x in json.loads(out)["sessions"]}
            self.assertEqual(rc, 0)
            self.assertEqual((r["a1"]["state"], r["a1"]["finished"]), ("suspended", True))
            self.assertEqual((r["a2"]["state"], r["a2"]["finished"]), ("working", False))
            rc, _, _ = self.mvm("wait", "--timeout", "0.3", "--interval", "0.05", env=env, check=False)
            self.assertEqual(rc, 2, "--any がなければ全台を待つ")
            # 終了した VM も終わり扱い
            (self.stub_dir / "microvm-0001.term").touch()
            rc, out, _ = self.mvm("wait", "a2", "--interval", "0.05", "--json", env=env)
            self.assertEqual(json.loads(out)["sessions"][0]["state"], "terminated")
        finally:
            api.shutdown()

    def test_wait_suspended_tells_how_to_check(self):
        """サスペンド中の VM は起こさずに返すが、作業の途中で止まった可能性と、確かめ方を伝える。
        手動の mvm suspend や、キープアライブの無い最小構成では、作業中でもサスペンドするため。"""
        api = StatusApi()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "a1", env=env)
            (self.stub_dir / "microvm-0000.suspended").touch()
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", "--json", env=env)
            r = json.loads(out)["sessions"][0]
            self.assertEqual((rc, r["state"], r["finished"]), (0, "suspended", True))
            self.assertIn("mvm peek a1", r.get("hint", ""))
            self.assertEqual(api.status_calls, 0, "サスペンド中の VM は起こさない")
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", env=env)
            self.assertIn("作業の途中", out)
            self.assertIn("mvm peek a1", out)
        finally:
            api.shutdown()

    def test_wait_with_old_image_uses_idle_time(self):
        """エージェントの状態を返さない古いイメージでは、画面と CPU が止まっているかだけで判断する。"""
        api = StatusApi()
        try:
            self.mvm("up", "a1")
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            old = {"name": "a1", "agent": "claude", "bootstrap": "ready", "windows": []}
            api.statuses = [dict(old, idleSeconds=10), dict(old, idleSeconds=400)]
            rc, out, _ = self.mvm("wait", "a1", "--interval", "0.05", "--json", env=env)
            self.assertEqual(json.loads(out)["sessions"][0]["state"], "stalled")
            self.assertEqual(api.status_calls, 2)
            rc, _, err = self.mvm("result", "a1", env=env, check=False)
            self.assertNotEqual(rc, 0)
            self.assertIn("mvm image build", err)
        finally:
            api.shutdown()

    def test_result_and_json_output(self):
        api = StatusApi()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "a1", "--agent", "both", "--repo", "https://github.com/o/r.git", env=env)
            api.results = {"claude": {"agent": "claude", "state": "done", "text": "PR を作りました: #12"},
                           "codex": {"agent": "codex", "state": "working", "text": None}}
            rc, out, _ = self.mvm("result", "a1", "--agent", "claude", env=env)
            self.assertEqual(out.strip(), "PR を作りました: #12")
            rc, out, _ = self.mvm("result", "a1", env=env, check=False)
            self.assertEqual(rc, 1, "応答がまだないエージェントがあれば 1")
            self.assertIn("--- claude (done) ---\nPR を作りました: #12", out.replace("\r\n", "\n"))  # Windows は改行が CRLF
            self.assertIn("(まだ codex の応答がありません)", out)
            _, out, _ = self.mvm("result", "a1", "--json", "--agent", "claude", env=env)
            self.assertEqual(json.loads(out)["text"], "PR を作りました: #12")
            # API のエラーで止まったときは、エラーの内容と次にやることを出し、終了コード 1
            api.results["claude"] = {"agent": "claude", "state": "error", "error": "rate_limit",
                                     "text": "API Error: Rate limit reached"}
            rc, out, err = self.mvm("result", "a1", "--agent", "claude", env=env, check=False)
            self.assertEqual(rc, 1)
            self.assertIn("(エラーで止まりました: rate_limit) API Error: Rate limit reached", out)
            self.assertIn("mvm send", err)
            _, out, _ = self.mvm("result", "a1", "--json", "--agent", "claude", env=env, check=False)
            self.assertEqual((json.loads(out)["state"], json.loads(out)["error"]), ("error", "rate_limit"))
            # ls / peek も JSON で出せる (司令塔のエージェントが読み取りやすいように)
            _, out, _ = self.mvm("ls", "--json", env=env)
            rows = json.loads(out)
            self.assertEqual([(r["name"], r["state"], r["agent"], r["repo"], r["branch"]) for r in rows],
                             [("a1", "RUNNING", "both", "https://github.com/o/r.git", "pmva/a1")])
            api.statuses = [self.status(claude="done", codex="working")]
            _, out, _ = self.mvm("peek", "a1", "--json", env=env)
            self.assertEqual(json.loads(out)["agents"]["codex"]["state"], "working")
            _, out, _ = self.mvm("peek", "a1", env=env)
            self.assertIn("claude=done codex=working", out)
        finally:
            api.shutdown()

    def test_follow_prints_transcript_events(self):
        api = StatusApi()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "a1", "--agent", "both", env=env)
            api.events = {
                "claude": [{"agent": "claude", "file": "/c/1.jsonl", "offset": 100, "events": [
                    {"kind": "prompt", "text": "テストを直して"},
                    {"kind": "thinking", "text": "まず失敗しているテストを見る"},
                    {"kind": "tool", "name": "Bash", "text": "pytest -x"},
                    {"kind": "output", "text": "\n".join(f"line{i}" for i in range(1, 8))},
                    {"kind": "output", "text": "No such file", "error": True},
                    {"kind": "text", "text": "直しました。\n2 行目", "truncated": True}]}],
                "codex": [{"agent": "codex", "file": "/x/r.jsonl", "offset": 50, "events": [
                    {"kind": "tool", "name": "exec_command", "text": "npm test"}]}],
            }
            _, out, _ = self.mvm("follow", "a1", "--once", env=env)
            out = out.replace("\r\n", "\n")
            for line in ("[a1 claude] 指示: テストを直して", "[a1 claude] 思考: まず失敗しているテストを見る",
                         "[a1 claude] 実行: Bash: pytest -x", "[a1 claude] 結果: line1\n    line2",
                         "    … (残り 2 行)", "[a1 claude] エラー: No such file",
                         "[a1 claude] 応答: 直しました。\n    2 行目\n    … (省略。全文は --full)",
                         "[a1 codex]  実行: exec_command: npm test"):
                self.assertIn(line, out)
            self.assertNotIn("line6", out, "--full なしではツールの結果は先頭の数行だけ")
            q = api.event_queries[0]
            self.assertEqual((q.get("file", ""), q["tail"], q["max"]), ("", "20", "500"))
            # --json は 1 行に 1 件。--agent で片方だけ、--full で省略なし (max=0)
            api.events = {"codex": [{"agent": "codex", "file": "/x/r.jsonl", "offset": 50,
                                     "events": [{"kind": "text", "text": "完了"}]}]}
            _, out, _ = self.mvm("follow", "a1", "--once", "--json", "--agent", "codex", "--full", "--lines", "5", env=env)
            self.assertEqual([json.loads(l) for l in out.splitlines()],
                             [{"kind": "text", "text": "完了", "session": "a1", "agent": "codex"}])
            self.assertEqual((api.event_queries[-1]["max"], api.event_queries[-1]["tail"]), ("0", "5"))
        finally:
            api.shutdown()

    def test_follow_continues_from_offset_until_vm_ends(self):
        api = StatusApi()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "a1", env=env)
            api.events = {"claude": [
                {"agent": "claude", "file": None, "offset": 0, "events": []},
                {"agent": "claude", "file": "/c/1.jsonl", "offset": 100, "reset": True, "events": [{"kind": "text", "text": "一つ目"}]},
                {"agent": "claude", "file": "/c/1.jsonl", "offset": 180, "events": [{"kind": "text", "text": "二つ目"}]},
                {"agent": "claude", "file": "/c/2.jsonl", "offset": 40, "reset": True,
                 "events": [{"kind": "prompt", "text": "新しい指示"}]},
            ]}
            # 用意した応答が尽きると API が壊れた応答を返す。そのとき VM が終了していれば、表示をやめて終わる
            (self.stub_dir / "microvm-0000.term").touch()
            rc, out, err = self.mvm("follow", "a1", "--interval", "0.05", env=env)
            out = out.replace("\r\n", "\n")
            self.assertEqual(rc, 0)
            self.assertIn("[a1 claude] 応答: 一つ目\n[a1 claude] 応答: 二つ目\n[a1 claude] --- 新しい会話 ---\n"
                          "[a1 claude] 指示: 新しい指示", out)
            self.assertIn("a1 は終了しました", err)
            self.assertEqual([(q.get("file", ""), q["offset"]) for q in api.event_queries[:4]],
                             [("", "0"), ("-", "0"), ("/c/1.jsonl", "100"), ("/c/1.jsonl", "180")], "前回の続きから読む")
            self.assertEqual(out.count("新しい会話"), 1, "記録ができたときは「新しい会話」と表示しない")
        finally:
            api.shutdown()

    def test_top_and_peek_show_memory(self):
        api = StatusApi()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "a1", env=env)
            st = dict(self.status(claude="working"), oomKills=2, memory={
                "totalMiB": 4096, "availableMiB": 1024, "usedMiB": 3072,
                "top": [{"name": "chrome", "count": 9, "rssMiB": 1800}, {"name": "node", "count": 3, "rssMiB": 700}]})
            api.statuses = [st]
            _, out, _ = self.mvm("top", "a1", env=env)
            self.assertIn("[a1] 使用 3.0 GiB / 4.0 GiB (空き 1.0 GiB)", out)
            self.assertIn("!! メモリ不足でプロセスが 2 回強制終了されました", out)
            self.assertRegex(out, r"chrome\s+9 個\s+1800 MiB")
            _, out, _ = self.mvm("top", "--json", env=env)
            self.assertEqual(json.loads(out)[0]["memory"]["top"][1]["name"], "node")
            _, out, _ = self.mvm("peek", "a1", env=env)
            self.assertIn("mem=3.0/4.0GiB", out)
            self.assertIn("!! メモリ不足でプロセスが 2 回強制終了されました", out)
            api.statuses = [{"name": "a1", "agent": "claude", "bootstrap": "ready", "windows": []}]
            _, out, _ = self.mvm("top", "a1", env=env)
            self.assertIn("古いイメージ", out)
            _, out, _ = self.mvm("top", "a1", env=dict(env, PMVA_STUB_GET_ERROR=self.SSO_EXPIRED))
            self.assertIn("[a1] UNKNOWN", out)
            self.assertIn("aws sso login", out)
        finally:
            api.shutdown()

    def fake_ssh(self, output=""):
        """引数を記録し、output を標準エラーに出して終わる ssh の代わり。"""
        fake = Path(self.tmp.name) / "fakessh"
        fake.mkdir(exist_ok=True)
        record = Path(self.tmp.name) / "ssh-args.json"
        script = fake / "fake_ssh.py"
        script.write_text(f"import json, sys\nopen({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n"
                          f"sys.stderr.write({output!r})\n", encoding="utf-8")
        if IS_WINDOWS:
            (fake / "ssh.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            (fake / "ssh").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            (fake / "ssh").chmod(0o755)
        return dict(self.env, PATH=str(fake) + os.pathsep + self.env["PATH"]), record

    def test_ssh_no_reconnect_after_name(self):
        """ドキュメントの書き方 (mvm ssh <name> --no-reconnect) で、オプションをコマンドとして ssh に渡さない。"""
        env, record = self.fake_ssh()
        self.mvm("up", "s1", env=env)
        self.mvm("ssh", "s1", "--no-reconnect", env=env)
        args = json.loads(record.read_text(encoding="utf-8"))
        self.assertNotIn("--no-reconnect", args)
        self.assertEqual(args[-2:], ["agent@pmva-s1", "agent-attach"])
        self.mvm("ssh", "s1", "--no-reconnect", "--", "ls", "-la", env=env)
        args = json.loads(record.read_text(encoding="utf-8"))
        self.assertNotIn("--no-reconnect", args)
        self.assertEqual(args[-3:], ["agent@pmva-s1", "ls", "-la"])
        self.mvm("ssh", "s1", "--", "echo", "--no-reconnect", env=env)   # -- の後ろはそのままコマンド
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))[-2:], ["echo", "--no-reconnect"])

    def test_ssh_known_hosts_file_is_not_devnull(self):
        """Windows の OpenSSH は nul を普通のファイル名として扱い、カレントフォルダに nul を作ってしまう。
        ホスト鍵は .state の下のセッションごとのファイルに書き、VM を作り直したら消す。"""
        env, record = self.fake_ssh()
        self.mvm("up", "s1", env=env)
        self.mvm("ssh", "s1", "--", "true", env=env)
        args = json.loads(record.read_text(encoding="utf-8"))
        opts = [args[i + 1] for i, a in enumerate(args) if a == "-o"]
        known = [o.split("=", 1)[1].strip('"') for o in opts if o.startswith("UserKnownHostsFile=")]
        self.assertEqual(len(known), 1, opts)
        self.assertNotEqual(known[0], os.devnull)
        path = Path(known[0])
        self.assertTrue(path.is_absolute(), path)
        self.assertEqual(path.parent, (Path(self.env["PMVA_STATE_DIR"]) / "known_hosts").resolve())
        self.assertTrue(path.parent.is_dir(), "ssh はフォルダを作らないので、先に作っておく")
        # VM を作り直すとホスト鍵が変わるので、前の VM の鍵を消す (残すと警告が出て、ポート転送が止められる)
        path.write_text("pmva-s1 ssh-ed25519 AAAAOLD\n", encoding="utf-8")
        self.mvm("stop", "s1", "--no-save", env=env)
        self.mvm("up", "s1", env=env)
        self.assertFalse(path.exists())
        # VS Code 用の ~/.ssh/config も同じファイルを使う
        _, out, _ = self.mvm("ssh-config", env=env)
        self.assertNotIn(f"UserKnownHostsFile {os.devnull}\n", out)
        self.assertIn(path.as_posix(), out)

    def test_forward_ports(self):
        env, record = self.fake_ssh()
        self.mvm("up", "f1", env=env)
        _, _, err = self.mvm("forward", "f1", "3000", "18080:8080", env=env)
        args = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(args[-8:], ["-N", "-o", "ExitOnForwardFailure=yes", "-L", "127.0.0.1:3000:127.0.0.1:3000",
                                     "-L", "127.0.0.1:18080:127.0.0.1:8080", "agent@pmva-f1"][-8:])
        self.assertIn("http://localhost:18080/ → f1 のポート 8080", err)
        for bad in ("abc", "0", "70000", "1:2:3"):
            rc, _, err = self.mvm("forward", "f1", bad, env=env, check=False)
            self.assertNotEqual(rc, 0, bad)
            self.assertIn("ポートの指定が正しくありません", err)
        with socket.socket() as busy:
            busy.bind(("127.0.0.1", 0))
            busy.listen()
            port = busy.getsockname()[1]
            rc, _, err = self.mvm("forward", "f1", f"{port}:3000", env=env, check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn(f"手元のポート {port} は使われています", err)

    LOGIN_URL = "https://auth.openai.com/oauth/authorize?response_type=code&redirect_uri=http%3A%2F%2F127.0.0.1%3A1455%2Fauth%2Fcallback"

    def test_login_codex_forwards_callback_port_and_opens_browser(self):
        with socket.socket() as probe:
            if not IS_WINDOWS:
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", 1455))
            except OSError:
                self.skipTest("この PC では 1455 番が使われている")
        env, record = self.fake_ssh(output=f"Starting local login server on http://localhost:1455.\n\n{self.LOGIN_URL}\n")
        opened = Path(self.tmp.name) / "opened-url.txt"
        if not IS_WINDOWS:   # webbrowser は BROWSER のコマンドに URL を渡す
            browser = Path(self.tmp.name) / "browser.sh"
            browser.write_text(f'#!/bin/sh\nprintf %s "$1" > "{opened}"\n', encoding="utf-8")
            browser.chmod(0o755)
            env["BROWSER"] = f"{browser} %s"
        self.mvm("up", "l1", "--agent", "codex", "--codex-auth", "interactive", env=env)
        _, out, err = self.mvm("login", "l1", env=env)
        args = json.loads(record.read_text(encoding="utf-8"))
        self.assertIn("127.0.0.1:1455:127.0.0.1:1455", args)
        self.assertIn("ExitOnForwardFailure=yes", args)
        self.assertEqual(args[-2], "agent@pmva-l1")
        self.assertIn("codex login", args[-1])
        self.assertNotIn("--device-auth", args[-1])
        self.assertIn(self.LOGIN_URL, out, "Codex の出力をそのまま見せる")
        if not IS_WINDOWS:
            for _ in range(50):
                if opened.exists():
                    break
                time.sleep(0.1)
            self.assertEqual(opened.read_text(encoding="utf-8"), self.LOGIN_URL)
        self.assertIn("codex を起動し直して", err)
        # --no-browser では開かない。--device ではポートをつながない
        if not IS_WINDOWS:
            opened.unlink()
        self.mvm("login", "l1", "codex", "--device", "--no-browser", env=env)
        args = json.loads(record.read_text(encoding="utf-8"))
        self.assertIn("codex login --device-auth", args[-1])
        self.assertFalse([a for a in args if ":1455:" in a])
        self.assertFalse(opened.exists())

    def test_login_checks_port_and_agent(self):
        env, record = self.fake_ssh()
        self.mvm("up", "l2", "--agent", "both", env=env)
        rc, _, err = self.mvm("login", "l2", env=env, check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("claude か codex", err)
        self.mvm("login", "l2", "claude", "--console", env=env)
        args = json.loads(record.read_text(encoding="utf-8"))
        self.assertIn("-t", args)
        self.assertIn("claude auth login --console", args[-1])
        with socket.socket() as busy:
            if not IS_WINDOWS:
                busy.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                busy.bind(("127.0.0.1", 1455))
            except OSError:
                self.skipTest("この PC では 1455 番が使われている")
            busy.listen()
            rc, _, err = self.mvm("login", "l2", "codex", env=env, check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("1455", err)

    def test_codex_access_token_and_gateway(self):
        self.mvm("up", "t1", "--agent", "codex", "--codex-auth", "access-token")
        self.mvm("up", "g1", "--agent", "codex", "--codex-auth", "gateway")
        t1, g1 = self.payloads()
        self.assertEqual(t1["codexSecret"], "pmva/codex-access-token")
        self.assertEqual(g1["codexSecret"], "pmva/codex-gateway")
        for bad in ('{"apiKey": "k"}', '{"baseUrl": "ftp://x"}', '{"baseUrl": "https://x", "headers": ["a"]}',
                    '{"baseUrl": "https://x", "apiKeyHeader": "bad header"}', "[]"):
            rc, _, err = self.mvm("secret", "set", "codex-gateway", input=bad.encode(), check=False)
            self.assertNotEqual(rc, 0, bad)
            self.assertIn("baseUrl", err)
        self.mvm("secret", "set", "codex-gateway", input=json.dumps({
            "baseUrl": "https://gw.example.com/v1", "apiKey": "k", "apiKeyHeader": "api-key",
            "queryParams": {"api-version": "2025-04-01"}, "headers": {"X-Team": "a"}}).encode())
        rc, _, err = self.mvm("secret", "set", "codex-access-token", input=b"not-a-token", check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("at-", err)
        self.mvm("secret", "set", "codex-access-token", input=b"at-test")

    def test_custom_auth(self):
        self.mvm("up", "u1", "--agent", "both", "--claude-auth", "custom", "--codex-auth", "custom")
        u1 = self.payloads()[0]
        self.assertEqual((u1["claudeSecret"], u1["codexSecret"]), ("pmva/claude-custom-auth", "pmva/codex-custom-auth"))
        good = {"env": {"A_B": "1"}, "files": {"cert.pem": "x"}, "claudeSettings": {"apiKeyHelper": "sh {{files}}/k.sh"}}
        self.mvm("secret", "set", "claude-custom-auth", input=json.dumps(good).encode())
        self.mvm("secret", "set", "codex-custom-auth", input=json.dumps({"codexConfig": 'model_provider = "x"\n'}).encode())
        for kind, bad in (("claude-custom-auth", {"env": {"PATH": "/x"}}),
                          ("claude-custom-auth", {"env": {"bad-name": "x"}}),
                          ("claude-custom-auth", {"env": {"A": 1}}),
                          ("claude-custom-auth", {"files": {"../x": "y"}}),
                          ("claude-custom-auth", {"files": {".hidden": "y"}}),
                          ("claude-custom-auth", {"codexConfig": "x = 1"}),
                          ("codex-custom-auth", {"claudeSettings": {}}),
                          ("codex-custom-auth", {"codexConfig": ["x"]}),
                          ("codex-custom-auth", {"unknown": 1}),
                          ("codex-custom-auth", ["not", "object"])):
            rc, _, err = self.mvm("secret", "set", kind, input=json.dumps(bad).encode(), check=False)
            self.assertNotEqual(rc, 0, (kind, bad))
            self.assertIn("auth.md", err, (kind, bad))

    def test_setup_options_in_payload(self):
        self.mvm("up", "s1")
        self.mvm("up", "s2", "--no-setup")
        p1, p2 = self.payloads()
        self.assertEqual((p1["setupTimeoutSeconds"], "skipSetup" in p1), (1800, False))
        self.assertEqual(p2["skipSetup"], "true")

    def test_view_attaches_read_only(self):
        fake = Path(self.tmp.name) / "fakessh"
        fake.mkdir()
        record = Path(self.tmp.name) / "ssh-args.json"
        script = fake / "fake_ssh.py"
        script.write_text(f"import json, sys\nopen({str(record)!r}, 'w').write(json.dumps(sys.argv[1:]))\n", encoding="utf-8")
        if IS_WINDOWS:
            (fake / "ssh.cmd").write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="utf-8")
        else:
            (fake / "ssh").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8")
            (fake / "ssh").chmod(0o755)
        env = dict(self.env, PATH=str(fake) + os.pathsep + self.env["PATH"])
        self.mvm("up", "v1", "--agent", "codex", env=env)
        _, _, err = self.mvm("view", "v1", env=env)
        args = json.loads(record.read_text(encoding="utf-8"))
        self.assertEqual(args[-4:], ["agent@pmva-v1", "agent-attach", "--view", "codex"])
        self.assertIn("-t", args)
        self.assertIn("読み取り専用", err)
        self.mvm("view", "v1", "--window", "shell", env=env)
        self.assertEqual(json.loads(record.read_text(encoding="utf-8"))[-1], "shell")

    def make_local_agent_config(self):
        claude = self.home / ".claude"
        codex = Path(self.tmp.name) / "codex-home"
        files = {
            claude / "CLAUDE.md": b"# \xe6\x89\x8b\xe5\x85\x83\xe3\x81\xae\xe3\x83\xab\xe3\x83\xbc\xe3\x83\xab\r\n",
            claude / "skills" / "review" / "SKILL.md": b"---\nname: review\n---\n",
            claude / "skills" / "review" / "run.sh": b"#!/bin/bash\r\necho ok\r\n",
            claude / "agents" / "tester.md": b"tester",
            claude / "commands" / "ship.md": b"ship",
            claude / "settings.json": b'{"env": {"X": "1"}}',
            claude / ".credentials.json": b"{}",
            claude / "skills" / "mvm-orchestrator" / "SKILL.md": b"orchestrator",
            claude / "skills" / "leaky" / "SKILL.md": b"token: sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345",
            claude / "skills" / "big" / "data.bin": b"x" * (1024 * 1024 + 1),
            claude / "skills" / "local" / "SKILL.md": str(self.home).encode() + b"/notes.md",
            codex / "AGENTS.md": b"codex rules",
            codex / "prompts" / "fix.md": b"fix",
            codex / "skills" / ".system" / "imagegen" / "SKILL.md": b"system",
            codex / "config.toml": b'model = "x"',
        }
        for path, data in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return dict(self.env, CODEX_HOME=str(codex))

    def test_profile_push(self):
        env = self.make_local_agent_config()
        _, out, _ = self.mvm("profile", "show", env=env)
        out = out.replace("\r\n", "\n")
        self.assertIn("認証情報 (API キー、トークン、秘密鍵など) らしい文字列を含む", out)
        self.assertIn("大きすぎる", out)
        self.assertIn("手元のパスを含む", out)
        self.assertIn("8 ファイルを渡します (まだ送っていません)", out)
        self.assertFalse((self.stub_dir / "s3").exists(), "show では送らない")
        self.mvm("profile", "push", "--dry-run", env=env)
        self.assertFalse((self.stub_dir / "s3").exists(), "--dry-run では送らない")
        _, _, err = self.mvm("profile", "push", env=env)
        self.assertIn("PROFILE=default", err)
        obj = self.stub_dir / "s3" / "pmva-bucket" / "profiles" / "default.tar.gz"
        with tarfile.open(obj) as tar:
            members = {m.name: m for m in tar.getmembers()}
            self.assertEqual(sorted(members), [
                "claude/CLAUDE.md", "claude/agents/tester.md", "claude/commands/ship.md", "claude/skills/local/SKILL.md",
                "claude/skills/review/SKILL.md", "claude/skills/review/run.sh", "codex/AGENTS.md", "codex/prompts/fix.md"])
            run_sh = tar.extractfile("claude/skills/review/run.sh").read()
            self.assertEqual(run_sh, b"#!/bin/bash\necho ok\n", "Windows の改行は LF にする")
            self.assertEqual(members["claude/skills/review/run.sh"].mode, 0o755)
            self.assertEqual(members["claude/CLAUDE.md"].mode, 0o644)
        # 名前を付けて、Claude Code だけ
        self.mvm("profile", "push", "--name", "team", "--agent", "claude", env=env)
        with tarfile.open(self.stub_dir / "s3" / "pmva-bucket" / "profiles" / "team.tar.gz") as tar:
            self.assertTrue(all(n.startswith("claude/") for n in tar.getnames()))
        rc, _, err = self.mvm("profile", "push", "--name", "Bad_Name", env=env, check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("英小文字・数字・ハイフン", err)

    def test_up_uses_profile(self):
        env = dict(self.make_local_agent_config(), PMVA_STUB_QUOTA_GB="100")
        rc, _, err = self.mvm("up", "p1", "--profile", "default", env=env, check=False)
        self.assertNotEqual(rc, 0, "送っていない設定のまとまりは使えない")
        self.assertIn("mvm profile push --name default", err)
        self.mvm("profile", "push", env=env)
        self.mvm("up", "p1", "--profile", "default", env=env)
        cfg = Path(self.env["PMVA_CONFIG"])
        cfg.write_text(cfg.read_text(encoding="utf-8") + "PROFILE=default\n", encoding="utf-8")
        self.mvm("up", "p2", env=env)
        self.mvm("up", "p3", "--no-profile", env=env)
        p1, p2, p3 = self.payloads()
        self.assertEqual(p1["profile"], "s3://pmva-bucket/profiles/default.tar.gz")
        self.assertEqual(p2["profile"], "s3://pmva-bucket/profiles/default.tar.gz", "config.env の PROFILE")
        self.assertNotIn("profile", p3)

    def test_orchestrator_install_for_claude(self):
        env = dict(self.env, PMVA_CONFIG=str(Path(self.tmp.name) / "none.env"))  # config.env がなくても使える
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text(json.dumps({"model": "opus", "permissions": {"allow": ["Bash(git status)"]}}), encoding="utf-8")
        self.mvm("orchestrator", "install", "claude", env=env)
        self.mvm("orchestrator", "install", "claude", env=env)   # 2 回目でも重複しない
        skill = (self.home / ".claude" / "skills" / "mvm-orchestrator" / "SKILL.md").read_text(encoding="utf-8")
        self.assertTrue(skill.startswith("---\nname: mvm-orchestrator\n"))
        self.assertNotIn("{{MVM}}", skill)
        mvm = (f'"{Path(sys.executable).as_posix()}" "{MVM.as_posix()}"' if IS_WINDOWS
               else (ROOT / "bin" / "mvm").as_posix())
        self.assertIn(f"{mvm} wait <name>... --timeout 540 --json", skill)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual(data["model"], "opus", "ほかの設定は残す")
        self.assertEqual(data["permissions"]["allow"],
                         ["Bash(git status)"] + [f"Bash({mvm} {c}:*)" for c in ("ls", "peek", "wait", "result", "follow", "top", "doctor")])
        self.assertNotIn("hooks", data, "利用者全体の設定には、重い処理を止めるフックを入れない")
        # --dir: そのフォルダだけ。重い処理を止めるフックも入れる
        proj = Path(self.tmp.name) / "proj"
        proj.mkdir()
        self.mvm("orchestrator", "install", "claude", "--dir", str(proj), env=env)
        self.mvm("orchestrator", "install", "claude", "--dir", str(proj), env=env)
        local = json.loads((proj / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual(len(local["hooks"]["PreToolUse"]), 1)
        command = local["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn("orchestrator_guard.py", command)
        self.assertTrue((proj / ".claude" / "skills" / "mvm-orchestrator" / "SKILL.md").exists())
        # 取り除くと、入れたものだけが消える
        self.mvm("orchestrator", "uninstall", "claude", env=env)
        data = json.loads(settings.read_text(encoding="utf-8"))
        self.assertEqual((data["model"], data["permissions"]["allow"]), ("opus", ["Bash(git status)"]))
        self.assertFalse((self.home / ".claude" / "skills" / "mvm-orchestrator").exists())
        self.mvm("orchestrator", "uninstall", "claude", "--dir", str(proj), env=env)
        local = json.loads((proj / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
        self.assertEqual((local["hooks"]["PreToolUse"], local["permissions"]["allow"]), ([], []))
        # 壊れた設定ファイルは上書きしない
        settings.write_text("{broken", encoding="utf-8")
        rc, _, err = self.mvm("orchestrator", "install", "claude", env=env, check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("書き換えずに止めました", err)
        self.assertEqual(settings.read_text(encoding="utf-8"), "{broken")

    def test_orchestrator_install_for_codex(self):
        codex_home = Path(self.tmp.name) / "codex-home"
        env = dict(self.env, CODEX_HOME=str(codex_home))
        _, out, _ = self.mvm("orchestrator", "install", "codex", env=env)
        self.assertTrue((codex_home / "skills" / "mvm-orchestrator" / "SKILL.md").exists())
        self.assertIn("[sandbox_workspace_write]\nnetwork_access = true", out.replace("\r\n", "\n"))
        proj = Path(self.tmp.name) / "proj"
        proj.mkdir()
        (proj / "AGENTS.md").write_text("# 既存の決まり\n\n- テストを書く\n", encoding="utf-8")
        self.mvm("orchestrator", "install", "codex", "--dir", str(proj), env=env)
        self.mvm("orchestrator", "install", "codex", "--dir", str(proj), env=env)
        agents_md = (proj / "AGENTS.md").read_text(encoding="utf-8")
        self.assertTrue(agents_md.startswith("# 既存の決まり\n\n- テストを書く\n"))
        self.assertEqual(agents_md.count("## この PC は司令塔"), 1)
        self.assertTrue((proj / ".agents" / "skills" / "mvm-orchestrator" / "SKILL.md").exists())
        self.mvm("orchestrator", "uninstall", "codex", "--dir", str(proj), env=env)
        self.assertEqual((proj / "AGENTS.md").read_text(encoding="utf-8"), "# 既存の決まり\n\n- テストを書く\n")
        self.assertFalse((proj / ".agents" / "skills" / "mvm-orchestrator").exists())

    def test_shell_with_piped_stdin_waits_for_output(self):
        """printf '...' | mvm shell のように標準入力がすぐ閉じても、VM 側が閉じるまで出力を受け取って表示する。

        以前は標準入力が閉じた時点で接続を閉じていたので、コマンドは VM で動いても何も表示されなかった。
        """
        server = ShellWebSocketServer()
        try:
            self.mvm("up", "sh1")
            env = dict(self.env, PMVA_WS_BASE_URL=f"ws://127.0.0.1:{server.port}")
            started = time.time()
            rc, out, _ = self.mvm("shell", "sh1", input="echo one\nexit\n".encode(), env=env)
            self.assertEqual(rc, 0)
            self.assertIn("OUT:echo one", out)
            self.assertNotIn("session_init", out)
            self.assertLess(time.time() - started, 30)
            # exit を書かなくても、最後に Ctrl-D を送ってシェルを終わらせる。改行で終わっていなければ改行を足す
            rc, out, _ = self.mvm("shell", "sh1", input=b"echo two", env=env)
            self.assertEqual(rc, 0)
            self.assertIn("OUT:echo two", out)
            self.assertEqual(b"".join(server.inputs[-2:])[-2:], b"\n\x04")
            self.assertIn("/shell", server.paths[0])
        finally:
            server.close()

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

    def add_config(self, text):
        cfg = Path(self.env["PMVA_CONFIG"])
        cfg.write_text(cfg.read_text(encoding="utf-8") + text, encoding="utf-8")

    def test_doctor_compares_infra_with_local(self):
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("--   AWS の構成を手元と比べられません", out, "deploy 前 (GetTemplate の権限がない) は比べない")
        self.mvm("infra", "deploy")
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("ok   AWS の構成 (スタック pmva) は手元のテンプレートと設定のとおりです", out)
        self.add_config("ENABLE_ANTHROPIC_AWS=true\n")
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("!!   AWS の構成に、設定の変更が反映されていません: EnableAnthropicAws (false → true)", out)
        self.assertIn("mvm infra deploy", out)
        self.add_config("INFRA_TEMPLATE=minimal\n")
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("!!   AWS の構成が、手元のテンプレート (cloudformation-minimal.yaml) と違います", out)
        self.mvm("infra", "deploy")
        _, out, _ = self.mvm("doctor", env=dict(self.env, PMVA_STUB_STACK_STATUS="UPDATE_ROLLBACK_COMPLETE"), check=False)
        self.assertIn("!!   スタック pmva の状態が UPDATE_ROLLBACK_COMPLETE です", out)

    def test_doctor_checks_image_build(self):
        self.env["PMVA_STUB_QUOTA_GB"] = "100"
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("--   この PC からイメージをビルドした記録がありません", out)
        self.mvm("image", "build")
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("ok   イメージは、最後にビルドしたときから変わっていません (バージョン 2.0)", out)
        self.add_config("IMAGE_EXTRA_PACKAGES=git-lfs\n")
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("!!   イメージの元 (image/ と IMAGE_* などの設定) が、最後にビルドしたときから変わっています", out)
        self.assertIn("mvm image build", out)

    def deleted_image_versions(self):
        calls = [json.loads(l) for l in (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()]
        return [c[c.index("--image-version") + 1] for c in calls if c[:2] == ["lambda-microvms", "delete-microvm-image-version"]]

    def image_versions_env(self, latest="4.0", extra=()):
        versions = [{"imageVersion": v, "state": "SUCCESSFUL", "status": "ACTIVE"} for v in ("1.0", "2.0", "3.0", "4.0")]
        return dict(self.env, PMVA_STUB_IMAGE_VERSIONS=json.dumps(versions + list(extra)), PMVA_STUB_LATEST_ACTIVE=latest)

    def test_image_prune_keeps_latest_two_versions(self):
        env = self.image_versions_env(extra=[{"imageVersion": "5.0", "state": "FAILED", "status": "INACTIVE"}])
        _, out, err = self.mvm("image", "prune", "--yes", env=env)
        self.assertEqual(sorted(self.deleted_image_versions()), ["1.0", "2.0", "5.0"])
        self.assertIn("残す版: 4.0, 3.0", err)
        self.assertIn("1.0", out)
        # 削除中の版は、もう一度実行しても消しに行かない
        self.mvm("image", "prune", "--yes", env=env)
        self.assertEqual(len(self.deleted_image_versions()), 3)

    def assert_refused_without_answer(self, rc, err, hint):
        self.assertNotEqual(rc, 0)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("EOFError", err)
        self.assertIn("中止しました", err)
        self.assertIn(hint, err)

    def test_confirmations_stop_cleanly_without_an_answer(self):
        # 端末の無いところ (司令塔のエージェント、CI、パイプ) から打って確認の答えを読めないとき、
        # EOFError のトレースバックで落ちていた。何も消さずに、次にやることを示して止まる
        env = self.image_versions_env()
        rc, _, err = self.mvm("image", "prune", input=b"", env=env, check=False)
        self.assert_refused_without_answer(rc, err, "--yes")
        self.assertEqual(self.deleted_image_versions(), [])
        rc, _, err = self.mvm("image", "delete", input=b"", env=env, check=False)
        self.assert_refused_without_answer(rc, err, "--yes")
        rc, _, err = self.mvm("infra", "delete", input=b"", check=False)
        self.assert_refused_without_answer(rc, err, "端末")
        calls = (self.stub_dir / "calls.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("delete-microvm-image", calls)
        self.assertNotIn("delete-stack", calls)

    def test_image_prune_keep_option(self):
        self.mvm("image", "prune", "--keep", "1", "--yes", env=self.image_versions_env())
        self.assertEqual(sorted(self.deleted_image_versions()), ["1.0", "2.0", "3.0"])

    def test_image_prune_always_keeps_latest_active(self):
        # 最新の ACTIVE 版が一番新しい版でなくても残す (ビルドに成功した 4.0 より前の 2.0 を使っている)
        self.mvm("image", "prune", "--keep", "1", "--yes", env=self.image_versions_env(latest="2.0"))
        self.assertEqual(sorted(self.deleted_image_versions()), ["1.0", "3.0", "4.0"])

    def test_image_prune_does_not_delete_versions_being_built(self):
        env = self.image_versions_env(extra=[{"imageVersion": "5.0", "state": "IN_PROGRESS", "status": "INACTIVE"}])
        self.mvm("image", "prune", "--yes", env=env)
        self.assertEqual(sorted(self.deleted_image_versions()), ["1.0", "2.0"])

    def test_image_prune_skips_versions_used_by_running_vms(self):
        self.mvm("up", "a1")   # スタブの VM はバージョン 1.0 で起動する
        env = self.image_versions_env()
        rc, _, err = self.mvm("image", "prune", "--yes", env=env)
        self.assertEqual(rc, 0)
        self.assertEqual(self.deleted_image_versions(), ["2.0"])
        self.assertIn("1.0 は起動中の VM (a1) が使っているので残しました", err)
        self.assertIn("mvm renew a1", err)
        self.mvm("stop", "a1", "--no-save")
        self.mvm("image", "prune", "--yes", env=env)
        self.assertEqual(sorted(self.deleted_image_versions()), ["1.0", "2.0"])

    def test_image_prune_skips_versions_used_by_vms_from_other_pcs(self):
        other = [{"microvmId": "microvm-other", "imageVersion": "2.0", "state": "SUSPENDED"}]
        _, _, err = self.mvm("image", "prune", "--yes", env=dict(self.image_versions_env(), PMVA_STUB_MICROVMS=json.dumps(other)))
        self.assertEqual(self.deleted_image_versions(), ["1.0"])
        self.assertIn("2.0 は起動中の VM (microvm-other (この PC のセッションにない VM)) が使っているので残しました", err)
        self.assertIn("その VM を起動した PC で mvm renew か mvm stop", err)

    def test_image_prune_aborts_when_vm_image_version_is_unknown(self):
        other = [{"microvmId": "microvm-other", "state": "RUNNING"}]
        rc, _, err = self.mvm("image", "prune", "--yes", env=dict(self.image_versions_env(), PMVA_STUB_MICROVMS=json.dumps(other)),
                              check=False)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.deleted_image_versions(), [])
        self.assertIn("イメージのバージョンが分かりません", err)

    def test_image_prune_does_not_touch_versions_in_unknown_state(self):
        env = self.image_versions_env(extra=[{"imageVersion": "0.5", "state": "SOMETHING_NEW", "status": "INACTIVE"},
                                             {"imageVersion": "0.6", "state": "DELETE_FAILED", "status": "INACTIVE"}])
        self.mvm("image", "prune", "--yes", env=env)
        self.assertEqual(sorted(self.deleted_image_versions()), ["0.6", "1.0", "2.0"])

    def test_image_prune_aborts_when_vm_list_is_unavailable(self):
        env = dict(self.image_versions_env(), PMVA_STUB_LIST_MICROVMS_DENIED="1")
        rc, _, err = self.mvm("image", "prune", "--yes", env=env, check=False)
        self.assertNotEqual(rc, 0)
        self.assertEqual(self.deleted_image_versions(), [])
        self.assertIn("起動中の VM の一覧を取得できませんでした", err)

    def test_image_prune_reports_delete_failure(self):
        env = dict(self.image_versions_env(), PMVA_STUB_DELETE_FAIL="1.0")
        rc, _, err = self.mvm("image", "prune", "--yes", env=env, check=False)
        self.assertNotEqual(rc, 0)
        # 1 つ失敗しても残りは消しに行く
        self.assertEqual(sorted(self.deleted_image_versions()), ["1.0", "2.0"])
        self.assertEqual(json.loads((self.stub_dir / "deleted-versions.json").read_text(encoding="utf-8")), ["2.0"])
        self.assertIn("バージョン 1.0 を削除できませんでした", err)
        self.assertIn("mvm ls", err)

    def test_image_prune_asks_before_deleting(self):
        rc, _, err = self.mvm("image", "prune", input=b"n\n", env=self.image_versions_env(), check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("中止しました", err)
        self.assertEqual(self.deleted_image_versions(), [])
        self.mvm("image", "prune", input=b"y\n", env=self.image_versions_env())
        self.assertEqual(sorted(self.deleted_image_versions()), ["1.0", "2.0"])

    def test_image_prune_with_nothing_to_delete(self):
        _, _, err = self.mvm("image", "prune")   # スタブの既定はバージョン 1.0 だけ
        self.assertIn("削除する古いバージョンはありません", err)
        self.assertEqual(self.deleted_image_versions(), [])

    def test_image_prune_rejects_keep_below_one(self):
        rc, _, err = self.mvm("image", "prune", "--keep", "0", "--yes", check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("--keep は 1 以上", err)

    def test_doctor_checks_default_auth(self):
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("!!   既定の認証方式 (DEFAULT_CLAUDE_AUTH=oauth-token) のシークレット pmva/claude-oauth-token がありません。"
                      "mvm secret set claude-oauth-token で登録してください", out)
        self.assertIn("!!   USE_GITHUB_TOKEN=true のシークレット pmva/github-token がありません", out)
        self.mvm("secret", "set", "claude-oauth-token", input=b"sk-ant-oat01-x")
        self.mvm("secret", "set", "github-token", input=b"ghp_x")
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("ok   シークレット pmva/claude-oauth-token (DEFAULT_CLAUDE_AUTH=oauth-token)", out)
        self.assertIn("ok   シークレット pmva/github-token (USE_GITHUB_TOKEN=true)", out)
        self.add_config("DEFAULT_AGENT=both\nDEFAULT_CLAUDE_AUTH=anthropic-aws\nDEFAULT_CODEX_AUTH=bedrock\nENABLE_BEDROCK=false\n")
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("!!   DEFAULT_CLAUDE_AUTH=anthropic-aws には ANTHROPIC_AWS_WORKSPACE_ID が必要です", out)
        self.assertIn("!!   DEFAULT_CLAUDE_AUTH=anthropic-aws には ENABLE_ANTHROPIC_AWS=true", out)
        self.assertIn("!!   DEFAULT_CODEX_AUTH=bedrock には ENABLE_BEDROCK=true", out)
        self.add_config("DEFAULT_CLAUDE_AUTH=interactive\nDEFAULT_CODEX_AUTH=none\n")
        _, out, _ = self.mvm("doctor", check=False)
        self.assertIn("--   DEFAULT_CLAUDE_AUTH=interactive は起動後にログインします (mvm login)", out)

    def test_api_error_does_not_mark_session_terminated(self):
        self.mvm("up", "u1")
        _, out, _ = self.mvm("ls", env=dict(self.env, PMVA_STUB_GET_DENIED="1"))
        self.assertIn("UNKNOWN", out)
        _, out, _ = self.mvm("ls")
        self.assertIn("RUNNING", out, "一時的なエラーで終了扱いにしない")
        rc, _, err = self.mvm("infra", "delete", input=b"y\n", env=dict(self.env, PMVA_STUB_GET_DENIED="1"), check=False)
        self.assertNotEqual(rc, 0)
        # 状態を取れないときは「起動中」と決めつけず、取れない理由を出して止める
        self.assertIn("u1 の状態を AWS から取得できません", err)
        self.assertIn("権限", err)
        rc, _, err = self.mvm("infra", "delete", input=b"y\n", check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("stop-all", err)

    SSO_EXPIRED = ("Error when retrieving token from sso: Token has expired and refresh failed\n")

    ACCESS_DENIED = ("\nAn error occurred (AccessDeniedException) when calling the DescribeSecret operation: User: "
                     "arn:aws:iam::123456789012:user/x is not authorized to perform: secretsmanager:DescribeSecret\n")

    def test_failed_lookups_are_not_reported_as_missing(self):
        """認証切れなどで問い合わせに失敗したとき、「無い」(作り直せ) と案内しない。

        2026-11-22、SSO の期限切れのまま mvm image status を打つと「イメージはまだありません (mvm image build)」と出た。
        スタックなら「mvm infra deploy を実行してください」と出て、要らない作り直しを招く。
        """
        env = dict(self.env, PMVA_STUB_ERROR=self.SSO_EXPIRED)
        (Path(self.env["PMVA_STATE_DIR"]) / "stack-outputs.json").unlink(missing_ok=True)
        for args in (["image", "status"], ["up", "e1"], ["infra", "outputs"]):
            with self.subTest(args=args):
                rc, _, err = self.mvm(*args, env=env, check=False)
                self.assertNotEqual(rc, 0)
                self.assertIn("aws sso login", err)
                self.assertNotIn("まだありません", err)
                self.assertNotIn("見つかりません", err)
                self.assertNotIn("Traceback", err)
        _, out, _ = self.mvm("doctor", env=env, check=False)
        self.assertIn("aws sso login", out)

    def test_failed_lookups_in_doctor_and_profile_show_the_reason(self):
        # 権限が足りないだけのときも、シークレットや設定のまとまりが「無い」とは言わない
        env = dict(self.env, PMVA_STUB_ERROR=self.ACCESS_DENIED, PMVA_STUB_ERROR_CMDS="secretsmanager describe-secret")
        _, out, _ = self.mvm("doctor", env=env, check=False)
        self.assertIn("権限", out)
        self.assertNotIn("がありません。mvm secret set", out)
        env = dict(self.env, PMVA_STUB_ERROR=self.SSO_EXPIRED, PMVA_STUB_ERROR_CMDS="s3api head-object")
        rc, _, err = self.mvm("up", "p1", "--profile", "team", env=env, check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("aws sso login", err)
        self.assertNotIn("まだありません", err)
        # 本当に無いときは、これまでどおり作り方を案内する
        rc, _, err = self.mvm("up", "p2", "--profile", "team", check=False)
        self.assertNotEqual(rc, 0)
        self.assertIn("mvm profile push", err)

    def test_unknown_state_shows_reason_and_next_step(self):
        """状態を取れなかったとき、UNKNOWN だけでなく理由とやるべきことを出す。"""
        self.mvm("up", "u1")
        self.mvm("up", "u2")
        cases = [
            (self.SSO_EXPIRED, "認証", "aws sso login"),
            ("\nUnable to locate credentials. You can configure credentials by running \"aws configure\".\n",
             "認証", "AWS_PROFILE"),
            ("\nAn error occurred (ExpiredTokenException) when calling the GetMicrovm operation: "
             "The security token included in the request is expired\n", "認証", "aws sso login"),
            ("\nAn error occurred (AccessDeniedException) when calling the GetMicrovm operation: User: "
             "arn:aws:iam::123456789012:user/x is not authorized to perform: lambda:GetMicrovm\n", "権限", "mvm doctor"),
            ("\nCould not connect to the endpoint URL: \"https://lambda.ap-northeast-1.amazonaws.com/\"\n",
             "接続", "ネットワーク"),
            ("\nAn error occurred (InternalFailure) when calling the GetMicrovm operation: boom\n",
             "InternalFailure", "mvm doctor"),
        ]
        for error, reason, action in cases:
            with self.subTest(error=error.strip()[:40]):
                env = dict(self.env, PMVA_STUB_GET_ERROR=error)
                _, out, err = self.mvm("ls", env=env)
                self.assertIn("UNKNOWN", out)
                self.assertIn(reason, err)
                self.assertIn(action, err)
                self.assertEqual(err.count(reason), 1, f"同じ理由は 1 回だけ出す:\n{err}")
                self.assertIn("u1", err)
                self.assertIn("u2", err)
                _, out, _ = self.mvm("ls", "--json", env=env)
                rows = json.loads(out)
                self.assertEqual(rows[0]["state"], "UNKNOWN")
                self.assertIn(reason, rows[0]["stateError"])
                self.assertIn(action, rows[0]["stateError"])
        _, out, _ = self.mvm("ls", "--json")
        self.assertNotIn("stateError", json.loads(out)[0], "取れたときは付けない")

    def test_wait_reports_why_state_is_unknown(self):
        self.mvm("up", "u1")
        env = dict(self.env, PMVA_STUB_GET_ERROR=self.SSO_EXPIRED)
        rc, out, _ = self.mvm("wait", "u1", "--timeout", "0.3", "--interval", "0.05", "--json", env=env, check=False)
        r = json.loads(out)["sessions"][0]
        self.assertEqual((rc, r["state"]), (2, "unreachable"))
        self.assertIn("aws sso login", r["error"])
        rc, _, err = self.mvm("wait", "u1", "--timeout", "0.3", "--interval", "0.05", env=env, check=False)
        self.assertEqual(rc, 2)
        self.assertIn("aws sso login", err)

    def test_watch_shows_why_state_is_unknown(self):
        self.mvm("up", "u1")
        env = dict(self.env, PMVA_STUB_GET_ERROR=self.SSO_EXPIRED, TERM="dumb")
        p = subprocess.Popen([sys.executable, str(MVM), "watch", "1"], stdout=subprocess.PIPE,
                             stderr=subprocess.DEVNULL, env=env)
        try:
            seen = b""
            deadline = time.time() + 60
            while "自動サスペンド".encode() not in seen and time.time() < deadline:
                line = p.stdout.readline()
                if not line:
                    break
                seen += line
        finally:
            p.kill()
            p.communicate()
        text = seen.decode("utf-8", "replace")
        # 一覧ボードにしたので、表の行 (u1 ... UNKNOWN) と、注意の行 (!! u1: 理由とやること) で確かめる
        self.assertRegex(text, r"(?m)^u1 +UNKNOWN")
        self.assertIn("!! u1:", text)
        self.assertIn("aws sso login", text)
        self.assertNotIn("\x1b", text, "端末でなければ制御文字を出さない")

    def watch_api(self, **status):
        """mvm watch 用の StatusApi。status は /status の応答に足す項目。"""
        api = StatusApi()
        api.statuses = [dict({"bootstrap": "ready", "idleSeconds": 1, "oomKills": 0,
                              "agents": {"claude": {"state": "working", "since": int(time.time()) - 720}},
                              "memory": {"usedMiB": 1229, "totalMiB": 3891, "availableMiB": 2600, "top": []}}, **status)]
        api.events = {"claude": [{"file": "/t/1.jsonl", "offset": 10,
                                  "events": [{"kind": "tool", "name": "Bash", "text": "pytest -x"}]}]}
        return api

    def test_watch_once_json(self):
        api = self.watch_api()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "w1", env=env)
            _, out, _ = self.mvm("watch", "--json", env=env)
        finally:
            api.shutdown()
        rows = json.loads(out)
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual((r["name"], r["vm"], r["state"]), ("w1", "RUNNING", "working"))
        self.assertEqual(r["memory"], {"usedMiB": 1229, "totalMiB": 3891, "peakMiB": 1229})
        self.assertIn(r["sinceSeconds"], range(720, 740))
        self.assertEqual((r["oomKills"], r["notes"]), (0, []))
        self.assertEqual(r["lastEvent"], "実行: Bash: pytest -x")
        self.assertEqual(r["agents"]["claude"]["state"], "working")
        self.assertEqual(api.event_queries[0]["tail"], "1")

    def test_watch_once_prints_board_without_control_characters(self):
        api = self.watch_api()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "w1", env=env)
            _, out, _ = self.mvm("watch", "--once", env=env)
        finally:
            api.shutdown()
        out = out.replace("\r\n", "\n")
        self.assertNotIn("\x1b", out)
        self.assertIn("1 台 (作業中 1 / 要対応 0 / 完了 0)  10 秒ごと", out)
        self.assertRegex(out, r"(?m)^w1 +作業中 +12分 +1\.2/3\.8G 最大1\.2G +実行: Bash: pytest -x$")
        self.assertEqual(api.status_calls, 1, "--once は 1 回だけ取得して終わる")

    def test_watch_log_appends_csv_with_header_once(self):
        api = self.watch_api(oomKills=2)
        log = Path(self.tmp.name) / "out" / "mem.csv"
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "w1", env=env)
            self.mvm("watch", "--once", "--log", str(log), env=env)
            self.mvm("watch", "--json", "--log", str(log), env=env)
        finally:
            api.shutdown()
        lines = log.read_text(encoding="utf-8").splitlines()
        self.assertEqual(lines[0], "time,name,vm,state,usedMiB,totalMiB,peakMiB,oomKills")
        self.assertEqual(len(lines), 3, "見出しは 1 回だけで、実行のたびに 1 台 1 行を足す")
        for line in lines[1:]:
            cols = line.split(",")
            self.assertEqual(cols[1:], ["w1", "RUNNING", "working", "1229", "3891", "1229", "2"])
            self.assertRegex(cols[0], r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d$")

    def test_watch_log_unwritable_stops_with_next_step(self):
        rc, _, err = self.mvm("watch", "--once", "--log", self.tmp.name, check=False)   # フォルダは CSV にできない
        self.assertNotEqual(rc, 0)
        self.assertIn("--log", err)
        self.assertIn("別のパス", err)

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
            self.assertIn("権限", err, "状態を取れなかった理由も出す")
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

    def test_stop_no_save_tells_vm_not_to_save_on_terminate(self):
        """--no-save でも VM の terminate フックが保存し、latest.tar.gz を上書きしていた。終了の前に VM に伝える。"""
        api = StatusApi()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "n1", env=env)
            self.mvm("stop", "n1", "--no-save", env=env)
            self.assertIn(("/terminate-save", {"save": False}), api.received)
            self.assertNotIn("/snapshot", [p for p, _ in api.received])
            self.assertTrue((self.stub_dir / "microvm-0000.term").exists())
        finally:
            api.shutdown()
        # VM の API に届かなくても終了はする。終了時に保存されうることを伝える
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            closed = s.getsockname()[1]
        env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{closed}")
        self.mvm("up", "n3", env=env)
        _, _, err = self.mvm("stop", "n3", "--no-save", env=env)
        self.assertTrue((self.stub_dir / "microvm-0001.term").exists())
        self.assertIn("終了時に保存されることがあります", err)

    def test_stop_saves_once(self):
        """保存の約 3 秒後に terminate フックがもう一度保存していた。保存できたら terminate フックの保存は止める。"""
        api = StatusApi()
        try:
            env = dict(self.env, PMVA_API_BASE_URL=f"http://127.0.0.1:{api.server_address[1]}")
            self.mvm("up", "s1", env=env)
            self.mvm("stop", "s1", env=env)
            self.assertEqual([p for p, _ in api.received], ["/snapshot", "/terminate-save"])
            self.assertEqual(api.received[1], ("/terminate-save", {"save": False}))
            self.assertTrue((self.stub_dir / "microvm-0000.term").exists())
            # 保存に失敗したときは、terminate フックでもう一度試みる (止めない)
            api.received.clear()
            api.snapshot_ok = False
            self.mvm("up", "s2", env=env)
            self.mvm("stop", "s2", env=env)
            self.assertEqual([p for p, _ in api.received], ["/snapshot"])
            self.assertTrue((self.stub_dir / "microvm-0001.term").exists())
            # renew も同じ
            api.received.clear()
            api.snapshot_ok = True
            self.mvm("up", "s3", env=env)
            self.mvm("renew", "s3", env=env)
            self.assertEqual([p for p, _ in api.received], ["/snapshot", "/terminate-save"])
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

    def cost_row(self, *args):
        _, out, _ = self.mvm("cost", *args)
        row = next(x for x in out.splitlines() if x.startswith(("tokyo", "custom")))
        return out, row.split()

    def test_cost_uses_tokyo_billed_rates_and_tax(self):
        # 2026-11 の東京の実請求: vCPU 秒 0.0000322421、GB 秒 0.0000042688 → 4GB (2 vCPU) で 1 台 1 時間 0.2936
        out, row = self.cost_row()
        self.assertEqual((row[0], row[1]), ("tokyo", "0.2936"))
        before_tax, with_tax = float(row[6]), float(row[7])
        self.assertAlmostEqual(with_tax, before_tax * 1.10, delta=0.01)  # 日本の消費税 10% を既定で含める
        self.assertIn("消費税", out)
        _, row = self.cost_row("--tax-rate", "0")
        self.assertEqual(row[6], row[7])

    def test_cost_snapshot_read_and_write_rates_differ(self):
        # 1 回のサスペンドで 6GB (4GB + ディスク 2GB) を書いて読む: 2 台 x (0.00466 + 0.00186) x 6 = 0.0782
        _, row = self.cost_row("--suspend-cycles-per-day", "1")
        self.assertEqual(row[3], "0.08")
        _, row = self.cost_row("--suspend-cycles-per-day", "1", "--snapshot-read-gb", "0.01", "--snapshot-write-gb", "0.03")
        self.assertEqual((row[0], row[3]), ("custom", "0.48"))
        _, row = self.cost_row("--suspend-cycles-per-day", "1", "--snapshot-rw-gb", "0.02")  # 互換: 読み書きとも
        self.assertEqual((row[0], row[3]), ("custom", "0.48"))

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
