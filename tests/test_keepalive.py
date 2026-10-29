"""infra/cloudformation.yaml に埋め込んだ keepalive Lambda のテスト。

テンプレートからコードを取り出し、boto3 / botocore と HTTP 通信を差し替えて動かす。
"""

import io
import json
import sys
import textwrap
import time
import types
import unittest
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def lambda_source(name="keepalive"):
    """テンプレートから FunctionName が <prefix>-<name> の Lambda のインラインコードを取り出す。"""
    lines = (ROOT / "infra" / "cloudformation.yaml").read_text(encoding="utf-8").splitlines()
    i = next(n for n, l in enumerate(lines) if l.strip() == f'FunctionName: !Sub "${{Prefix}}-{name}"')
    i = next(n for n in range(i, len(lines)) if lines[n].strip() == "ZipFile: |") + 1
    body = []
    while i < len(lines) and (not lines[i].strip() or lines[i].startswith(" " * 10)):
        body.append(lines[i])
        i += 1
    return textwrap.dedent("\n".join(body).rstrip()) + "\n"


class FakeS3:
    def __init__(self, objects):
        self.objects = objects

    def get_paginator(self, name):
        objs = self.objects

        class P:
            def paginate(self, Bucket, Prefix):
                return [{"Contents": [{"Key": k} for k in objs if k.startswith(Prefix)]}]
        return P()

    def get_object(self, Bucket, Key):
        return {"Body": io.BytesIO(json.dumps(self.objects[Key]).encode())}


class KeepaliveTest(unittest.TestCase):
    def load(self, objects, vms, name="keepalive", urlopen=None):
        """Lambda のコードを差し替えたモジュールで読み込む。vms: microvmId -> state"""
        self.requests = []
        fake_s3 = FakeS3(objects)
        boto3 = types.ModuleType("boto3")
        boto3.client = lambda name: fake_s3
        botocore = types.ModuleType("botocore")
        session = types.ModuleType("botocore.session")

        class Creds:
            def get_frozen_credentials(self):
                return object()

        session.Session = lambda: types.SimpleNamespace(get_credentials=lambda: Creds())
        auth = types.ModuleType("botocore.auth")

        class SigV4Auth:
            def __init__(self, creds, service, region):
                assert (service, region) == ("lambda", "ap-northeast-1")

            def add_auth(self, req):
                req.headers["Authorization"] = "AWS4-HMAC-SHA256 test"

        auth.SigV4Auth = SigV4Auth
        awsrequest = types.ModuleType("botocore.awsrequest")

        class AWSRequest:
            def __init__(self, method, url, data, headers):
                self.method, self.url, self.data, self.headers = method, url, data, dict(headers)

        awsrequest.AWSRequest = AWSRequest
        botocore.session, botocore.auth, botocore.awsrequest = session, auth, awsrequest
        mods = {"boto3": boto3, "botocore": botocore, "botocore.session": session,
                "botocore.auth": auth, "botocore.awsrequest": awsrequest}
        self.saved_modules = {k: sys.modules.get(k) for k in mods}
        sys.modules.update(mods)

        def fake_urlopen(req, timeout=None):
            url = req.full_url
            self.requests.append((req.get_method(), url, dict(req.header_items())))
            if url.endswith("/auth-token"):
                body = {"authToken": {"X-aws-proxy-auth": "TOK"}}
            elif "/2025-09-09/microvms/" in url:
                vid = url.rsplit("/", 1)[1]
                body = {"state": vms[vid], "endpoint": f"https://{vid}.lambda-microvm.example.on.aws"}
            else:
                body = {"ok": True}
            return io.BytesIO(json.dumps(body).encode())

        self.saved_urlopen = urllib.request.urlopen
        urllib.request.urlopen = urlopen or fake_urlopen
        import os
        os.environ.update({"AWS_REGION": "ap-northeast-1", "BUCKET": "b", "ACTIVE_SECONDS": "900",
                           "IMAGE_ARN": "arn:aws:lambda:ap-northeast-1:123:microvm-image:pmva-agent"})
        mod = types.ModuleType(name)
        exec(compile(lambda_source(name), name, "exec"), mod.__dict__)
        return mod

    def tearDown(self):
        if not hasattr(self, "saved_urlopen"):
            return
        urllib.request.urlopen = self.saved_urlopen
        for k, v in self.saved_modules.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def test_pings_only_busy_running_vms(self):
        now = time.time()
        objects = {
            "status/busy.json": {"name": "busy", "microvmId": "m-busy", "lastActivity": now - 60, "updatedAt": now - 30},
            "status/idle.json": {"name": "idle", "microvmId": "m-idle", "lastActivity": now - 3600, "updatedAt": now - 30},
            "status/stale.json": {"name": "stale", "microvmId": "m-stale", "lastActivity": now - 60, "updatedAt": now - 3600},
            "status/susp.json": {"name": "susp", "microvmId": "m-susp", "lastActivity": now - 60, "updatedAt": now - 30},
        }
        mod = self.load(objects, {"m-busy": "RUNNING", "m-idle": "RUNNING", "m-stale": "RUNNING", "m-susp": "SUSPENDED"})
        result = mod.handler({}, None)
        self.assertEqual(result, {"pinged": ["busy"]})
        urls = [u for _, u, _ in self.requests]
        self.assertIn("https://lambda.ap-northeast-1.amazonaws.com/2025-09-09/microvms/m-busy/auth-token", urls)
        health = [r for r in self.requests if r[1].endswith("/healthz")]
        self.assertEqual(len(health), 1)
        self.assertEqual(health[0][1], "https://m-busy.lambda-microvm.example.on.aws/healthz")
        headers = {k.lower(): v for k, v in health[0][2].items()}
        self.assertEqual(headers["x-aws-proxy-auth"], "TOK")
        self.assertEqual(headers["x-aws-proxy-port"], "8080")
        # 一時停止中の VM には API で状態を見るだけで、エンドポイントには触れない (起こさない)
        self.assertNotIn("https://m-susp.lambda-microvm.example.on.aws/healthz", urls)

    def test_inline_code_fits_cloudformation_limit(self):
        for name in ("keepalive", "nightly-stop"):
            self.assertLess(len(lambda_source(name)), 4096, name)

    def test_nightly_stop_saves_then_terminates_active_vms(self):
        calls = []
        pages = {
            None: {"items": [{"microvmId": "m-run", "state": "RUNNING"}, {"microvmId": "m-term", "state": "TERMINATED"}],
                   "nextToken": "p2"},
            "p2": {"items": [{"microvmId": "m-susp", "state": "SUSPENDED"}, {"microvmId": "m-bad", "state": "RUNNING"}]},
        }

        def urlopen(req, timeout=None):
            url, method = req.full_url, req.get_method()
            calls.append((method, url))
            parsed = urllib.parse.urlparse(url)
            if parsed.path == "/2025-09-09/microvms" and method == "GET":
                q = urllib.parse.parse_qs(parsed.query)
                self.assertEqual(q["imageIdentifier"], ["arn:aws:lambda:ap-northeast-1:123:microvm-image:pmva-agent"])
                body = pages[q.get("nextToken", [None])[0]]
            elif url.endswith("/auth-token"):
                body = {"authToken": {"X-aws-proxy-auth": "TOK"}}
            elif method == "GET":
                vid = url.rsplit("/", 1)[1]
                body = {"endpoint": f"https://{vid}.lambda-microvm.example.on.aws"}
            elif url.endswith("/snapshot"):
                if "m-bad" in url:
                    raise OSError("snapshot failed")
                body = {"ok": True}
            else:
                body = {}
            return io.BytesIO(json.dumps(body).encode())

        mod = self.load({}, {}, name="nightly-stop", urlopen=urlopen)
        result = mod.handler({}, None)
        self.assertEqual(result, {"stopped": ["m-run", "m-susp", "m-bad"]})
        # 保存してから終了する。保存に失敗しても終了はする。終了済みの VM には触らない
        run = [c for c in calls if "m-run" in c[1]]
        self.assertEqual(run[-2], ("POST", "https://m-run.lambda-microvm.example.on.aws/snapshot"))
        self.assertEqual(run[-1], ("DELETE", "https://lambda.ap-northeast-1.amazonaws.com/2025-09-09/microvms/m-run"))
        self.assertIn(("DELETE", "https://lambda.ap-northeast-1.amazonaws.com/2025-09-09/microvms/m-bad"), calls)
        self.assertFalse([c for c in calls if "m-term" in c[1]])


if __name__ == "__main__":
    unittest.main()
