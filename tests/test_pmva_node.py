"""image/pmva-node (リポジトリが求める版の Node.js を入れる) のテスト。nodejs.org の代わりにローカルのサーバーを使う。"""

import hashlib
import http.server
import io
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

PMVA_NODE = Path(__file__).resolve().parent.parent / "image" / "pmva-node"
ARCH = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
# index.json は新しい順 (nodejs.org と同じ)
INDEX = [
    {"version": "v22.3.0", "lts": False}, {"version": "v20.11.1", "lts": "Iron"}, {"version": "v20.11.0", "lts": "Iron"},
    {"version": "v20.2.0", "lts": False}, {"version": "v18.20.0", "lts": "Hydrogen"},
]


def node_tarball(version):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        script = f"#!/bin/sh\necho v{version}\n".encode()
        info = tarfile.TarInfo(f"node-v{version}-linux-{ARCH}/bin/node")
        info.size, info.mode = len(script), 0o755
        tar.addfile(info, io.BytesIO(script))
    return buf.getvalue()


class Mirror(http.server.ThreadingHTTPServer):
    def __init__(self):
        self.files = {"/index.json": json.dumps(INDEX).encode()}
        for entry in INDEX:
            v = entry["version"][1:]
            name = f"node-v{v}-linux-{ARCH}.tar.xz"
            data = node_tarball(v)
            self.files[f"/v{v}/{name}"] = data
            self.files[f"/v{v}/SHASUMS256.txt"] = f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()
        srv = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                data = srv.files.get(self.path)
                self.send_response(200 if data is not None else 404)
                self.end_headers()
                self.wfile.write(data or b"")

        super().__init__(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server_address[1]}"
        threading.Thread(target=self.serve_forever, daemon=True).start()


@unittest.skipUnless(sys.platform.startswith("linux") and ARCH and shutil.which("jq") and shutil.which("sha256sum"),
                     "Linux と jq / sha256sum が必要 (MicroVM の中で使うスクリプト)")
class PmvaNodeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        self.repo = Path(self.tmp.name) / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        self.mirror = Mirror()

    def tearDown(self):
        self.mirror.shutdown()
        self.tmp.cleanup()

    def run_node(self, *args):
        env = dict(os.environ, HOME=str(self.home), PMVA_NODE_MIRROR=self.mirror.url, NO_PROXY="127.0.0.1", no_proxy="127.0.0.1")
        return subprocess.run(["bash", str(PMVA_NODE), *args], cwd=self.repo, env=env, capture_output=True, text=True)

    def node_version(self):
        return subprocess.run([str(self.home / ".local" / "bin" / "node")], capture_output=True, text=True).stdout.strip()

    def test_versions(self):
        for want, got in (("20", "v20.11.1"), ("20.2", "v20.2.0"), ("v18.20.0", "v18.20.0"), ("lts/*", "v20.11.1"),
                          ("lts/hydrogen", "v18.20.0")):
            p = self.run_node("install", want)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertEqual(self.node_version(), got, want)
        # 2 は 20 系や 22 系に当たらない
        p = self.run_node("install", "2")
        self.assertNotEqual(p.returncode, 0)
        self.assertIn("Node.js の版 2 が見つかりません", p.stderr)

    def test_reads_version_files_and_reset(self):
        self.assertNotEqual(self.run_node("install").returncode, 0, "版の指定もファイルもなければエラー")
        (self.repo / ".nvmrc").write_text("lts/iron\n", encoding="utf-8")
        self.assertEqual(self.run_node("install").returncode, 0)
        self.assertEqual(self.node_version(), "v20.11.1")
        (self.repo / ".node-version").write_text("18\n", encoding="utf-8")
        self.assertEqual(self.run_node("install").returncode, 0, ".node-version を優先する")
        self.assertEqual(self.node_version(), "v18.20.0")
        self.assertEqual(self.run_node("reset").returncode, 0)
        self.assertFalse((self.home / ".local" / "bin" / "node").exists())

    def test_checksum_mismatch_is_rejected(self):
        name = f"node-v22.3.0-linux-{ARCH}.tar.xz"
        self.mirror.files[f"/v22.3.0/{name}"] += b"tampered"
        p = self.run_node("install", "22")
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.home / ".local" / "bin" / "node").exists(), "確かめられないものは使わない")


if __name__ == "__main__":
    unittest.main()
