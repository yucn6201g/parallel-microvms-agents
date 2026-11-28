"""テスト用の AWS CLI スタブ。mvm が呼ぶコマンドだけに JSON で答える。

状態は環境変数 PMVA_STUB_DIR のディレクトリに置く:
  calls.jsonl          呼び出された引数
  <microvmId>.payload  run-microvm の --run-hook-payload
  <microvmId>.term     terminate 済みの印
  secret-<name>        create-secret / put-secret-value で受け取った値
  s3/<bucket>/<key>    s3 cp / s3api put-object で S3 に置いたファイル (s3api delete-object で消える。s3 cp <S3> - は標準出力に読む)
  deployed.json        cloudformation deploy で受け取ったテンプレートとパラメータ (describe-stacks / get-template が返す)
"""

import json
import os
import sys
import time
from pathlib import Path

D = Path(os.environ["PMVA_STUB_DIR"])
D.mkdir(parents=True, exist_ok=True)
a = sys.argv[1:]
with open(D / "calls.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps(a, ensure_ascii=False) + "\n")


def opt(k):
    return a[a.index(k) + 1] if k in a else None


def out(obj):
    print(json.dumps(obj))
    sys.exit(0)


def fail(msg, code=254):
    sys.stderr.write(msg)
    sys.exit(code)


cmd = " ".join(a[:2])
if a[:1] == ["--version"]:
    print("aws-cli/stub")
    sys.exit(0)
# PMVA_STUB_ERROR: AWS CLI のエラー文 (認証切れ、権限なし、通信エラーなど)。PMVA_STUB_ERROR_CMDS
# (「s3api head-object,secretsmanager describe-secret」の形) が空なら全部の呼び出しを、あればそれだけを失敗させる
if os.environ.get("PMVA_STUB_ERROR"):
    only = [c for c in os.environ.get("PMVA_STUB_ERROR_CMDS", "").split(",") if c]
    if not only or cmd in only:
        fail(os.environ["PMVA_STUB_ERROR"])
if cmd == "sts get-caller-identity":
    out({"Account": "123456789012", "Arn": os.environ.get("PMVA_STUB_ARN", "arn:aws:iam::123456789012:user/test")})
if a[:1] == ["iam"]:  # PMVA_STUB_IAM=admin で管理者、denied で権限なし
    mode = os.environ.get("PMVA_STUB_IAM", "")
    if mode == "denied":
        fail("An error occurred (AccessDenied) when calling the " + a[1] + " operation")
    admin = [{"PolicyName": "AdministratorAccess", "PolicyArn": "arn:aws:iam::aws:policy/AdministratorAccess"}]
    if a[1] in ("list-attached-user-policies", "list-attached-role-policies"):
        out({"AttachedPolicies": admin if mode == "admin" else []})
    if a[1] == "list-groups-for-user":
        out({"Groups": []})
    if a[1] == "list-access-keys":
        out({"AccessKeyMetadata": [{"AccessKeyId": "AKIAOLDKEY", "Status": "Active",
                                    "CreateDate": os.environ.get("PMVA_STUB_KEY_CREATED", "2099-01-01T00:00:00+00:00")}]})
    # mvm infra operator。作った IAM ユーザーは iam-user-<名前> の印で覚える
    user_mark = D / f"iam-user-{opt('--user-name')}"
    if a[1] == "get-user":
        if user_mark.exists():
            out({"User": {"UserName": opt("--user-name")}})
        fail(f"An error occurred (NoSuchEntity) when calling the GetUser operation: "
             f"The user with name {opt('--user-name')} cannot be found.")
    if a[1] == "create-user":
        user_mark.write_text("", encoding="utf-8")
        out({"User": {"UserName": opt("--user-name")}})
    if a[1] == "attach-user-policy":
        sys.exit(0)
    if a[1] == "create-access-key":  # PMVA_STUB_KEY_LIMIT=1 でキーの上限 (1 ユーザー 2 個) に達している
        if os.environ.get("PMVA_STUB_KEY_LIMIT"):
            fail("An error occurred (LimitExceeded) when calling the CreateAccessKey operation: "
                 "Cannot exceed quota for AccessKeysPerUser: 2")
        out({"AccessKey": {"UserName": opt("--user-name"), "AccessKeyId": "AKIASTUBNEWKEY",
                           "SecretAccessKey": "stub/secret+KEY", "Status": "Active"}})
if cmd == "lambda get-function":  # PMVA_STUB_LAMBDA=exists / denied
    mode = os.environ.get("PMVA_STUB_LAMBDA", "")
    if mode == "exists":
        out({"Configuration": {"FunctionName": opt("--function-name")}})
    fail("AccessDeniedException" if mode == "denied" else "ResourceNotFoundException")
deployed_file = D / "deployed.json"
deployed = json.loads(deployed_file.read_text(encoding="utf-8")) if deployed_file.exists() else None
if cmd == "cloudformation get-template":  # deploy する前は権限がない場合と同じく失敗する
    if not deployed:
        fail("An error occurred (AccessDenied) when calling the GetTemplate operation")
    out({"TemplateBody": deployed["template"], "StagesAvailable": ["Original", "Processed"]})
if cmd == "cloudformation describe-stacks":
    if os.environ.get("PMVA_STUB_NO_STACK") and not deployed:   # deploy するまでスタックが無い
        fail("An error occurred (ValidationError) when calling the DescribeStacks operation: "
             f"Stack with id {opt('--stack-name')} does not exist")
    if os.environ.get("PMVA_STUB_STACK_FAILED"):   # 作成に失敗して出力の無いスタック (ROLLBACK_COMPLETE など)
        out({"Stacks": [{"StackStatus": os.environ["PMVA_STUB_STACK_FAILED"], "Outputs": []}]})
    extra = {}
    if deployed:
        extra = {"StackStatus": os.environ.get("PMVA_STUB_STACK_STATUS", "UPDATE_COMPLETE"),
                 "Parameters": [{"ParameterKey": k, "ParameterValue": v} for k, v in deployed["parameters"].items()]}
    out({"Stacks": [{**extra, "Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in {
        "ArtifactBucketName": "pmva-bucket",
        "BuildRoleArn": "arn:aws:iam::123456789012:role/build",
        "ExecutionRoleArn": "arn:aws:iam::123456789012:role/exec",
        "ExecutionRoleClaudeArn": "arn:aws:iam::123456789012:role/exec-claude",
        "ExecutionRoleCodexArn": "arn:aws:iam::123456789012:role/exec-codex",
        "ExecutionRoleNoneArn": "arn:aws:iam::123456789012:role/exec-none",
        "LogGroupName": "/aws/lambda-microvms/pmva",
        "OperatorPolicyArn": "arn:aws:iam::123456789012:policy/pmva-operator",
    }.items()]}]})
if cmd == "service-quotas get-service-quota":
    out({"Quota": {"QuotaCode": opt("--quota-code"), "Value": float(os.environ.get("PMVA_STUB_QUOTA_GB", "8"))}})
if os.environ.get("PMVA_STUB_DOCKER_IMAGE"):   # 手元の Docker を MicroVM の代わりにする (tests/local_microvms.py)
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import local_microvms
    if cmd in local_microvms.HANDLED:
        try:
            out(local_microvms.handle(cmd, opt, D))
        except RuntimeError as e:
            fail(str(e))
if cmd == "lambda-microvms list-managed-microvm-images":
    out({"items": [{"imageArn": "arn:aws:lambda:ap-northeast-1:aws:microvm-image:al2023-1"}]})
if cmd == "lambda-microvms list-microvm-images":   # PMVA_STUB_NO_IMAGE=1 で create-microvm-image するまでイメージが無い
    if os.environ.get("PMVA_STUB_NO_IMAGE") and not (D / "image-created").exists():
        out({"items": []})
    out({"items": [{"name": "pmva-agent", "imageArn": "arn:img", "state": "CREATED"}]})
if cmd in ("lambda-microvms create-microvm-image", "lambda-microvms update-microvm-image"):
    (D / "image-created").touch()
    out({"imageArn": "arn:img", "imageVersion": "2.0"})
if cmd == "lambda-microvms get-microvm-image-version":
    out({"state": "SUCCESSFUL"})
if cmd == "lambda-microvms get-microvm-image":
    out({"latestActiveImageVersion": os.environ.get("PMVA_STUB_LATEST_ACTIVE", "1.0")})
# PMVA_STUB_IMAGE_VERSIONS: list-microvm-image-versions が返す版 (JSON の配列)。
# delete-microvm-image-version で消した版は、本物と同じく DELETING になる
deleted_versions_file = D / "deleted-versions.json"
deleted_versions = json.loads(deleted_versions_file.read_text(encoding="utf-8")) if deleted_versions_file.exists() else []
if cmd == "lambda-microvms list-microvm-image-versions":
    versions = json.loads(os.environ.get("PMVA_STUB_IMAGE_VERSIONS") or json.dumps(
        [{"imageVersion": "1.0", "state": "SUCCESSFUL", "status": "ACTIVE"}]))
    for v in versions:
        v.setdefault("createdAt", "2026-11-0%sT00:00:00+00:00" % v["imageVersion"][0])
        if v["imageVersion"] in deleted_versions:
            v["state"] = "DELETING"
    out({"items": versions})
if cmd == "lambda-microvms delete-microvm-image-version":  # PMVA_STUB_DELETE_FAIL=2.0,3.0 でその版の削除が失敗する
    ver = opt("--image-version")
    if ver in os.environ.get("PMVA_STUB_DELETE_FAIL", "").split(","):
        fail("An error occurred (ConflictException) when calling the DeleteMicrovmImageVersion operation: "
             "The image version is in use by one or more MicroVMs")
    deleted_versions_file.write_text(json.dumps(deleted_versions + [ver]), encoding="utf-8")
    out({"imageIdentifier": opt("--image-identifier"), "imageVersion": ver, "state": "DELETING"})
if cmd == "lambda-microvms list-microvms":  # run-microvm で起動した VM (版は 1.0)。terminate した VM も TERMINATED で返す
    if os.environ.get("PMVA_STUB_LIST_MICROVMS_DENIED"):
        fail("An error occurred (AccessDeniedException) when calling the ListMicrovms operation")
    vms = [{"microvmId": p.stem, "imageArn": "arn:img", "imageVersion": "1.0", "startedAt": 1793201909.5,
            "state": "TERMINATED" if (D / f"{p.stem}.term").exists()
            else "SUSPENDED" if (D / f"{p.stem}.suspended").exists() else "RUNNING"}
           for p in sorted(D.glob("microvm-*.payload"))]
    # PMVA_STUB_MICROVMS: この PC のセッションにない VM を足す (JSON の配列)
    out({"items": vms + json.loads(os.environ.get("PMVA_STUB_MICROVMS") or "[]")})
if cmd == "lambda-microvms run-microvm":
    vid = "microvm-%04d" % len(list(D.glob("microvm-*.payload")))
    payload = opt("--run-hook-payload")
    if payload.startswith("file://"):
        payload = Path(payload[len("file://"):]).read_text(encoding="utf-8")
    (D / f"{vid}.payload").write_text(payload, encoding="utf-8")
    (D / f"{vid}.args").write_text(json.dumps(a), encoding="utf-8")
    out({"microvmId": vid, "state": "PENDING", "endpoint": f"https://{vid}.lambda-microvm.example.on.aws",
         "imageVersion": "1.0", "startedAt": time.time()})
if cmd == "lambda-microvms get-microvm":
    vid = opt("--microvm-identifier")
    if not (D / f"{vid}.payload").exists():
        fail("ResourceNotFoundException")
    countdown = D / f"{vid}.terminating"  # 残り回数だけ TERMINATING を返す
    if countdown.exists() and int(countdown.read_text()) > 0:
        countdown.write_text(str(int(countdown.read_text()) - 1))
        out({"state": "TERMINATING"})
    if os.environ.get("PMVA_STUB_GET_DENIED"):
        fail("AccessDeniedException")
    if os.environ.get("PMVA_STUB_GET_ERROR"):   # AWS CLI のエラー文をそのまま返す (認証切れ、通信エラーなど)
        fail(os.environ["PMVA_STUB_GET_ERROR"])
    if (D / f"{vid}.term").exists():
        out({"state": "TERMINATED"})
    out({"state": "SUSPENDED" if (D / f"{vid}.suspended").exists() else "RUNNING"})
if cmd == "lambda-microvms suspend-microvm":
    (D / f"{opt('--microvm-identifier')}.suspended").touch()
    out({})
if cmd == "lambda-microvms resume-microvm":
    (D / f"{opt('--microvm-identifier')}.suspended").unlink(missing_ok=True)
    out({})
if cmd == "lambda-microvms terminate-microvm":
    (D / f"{opt('--microvm-identifier')}.term").touch()
    out({})
if cmd in ("lambda-microvms create-microvm-auth-token", "lambda-microvms create-microvm-shell-auth-token"):
    out({"authToken": {"X-aws-proxy-auth": "TOKEN-" + opt("--microvm-identifier")}})
if cmd == "secretsmanager describe-secret":
    if (D / ("secret-" + opt("--secret-id").replace("/", "_"))).exists():
        out({"Name": opt("--secret-id")})
    fail("ResourceNotFoundException")
if cmd in ("secretsmanager create-secret", "secretsmanager put-secret-value"):
    name = opt("--name") or opt("--secret-id")
    uri = opt("--secret-string")
    assert uri.startswith("file://"), uri
    value = Path(uri[len("file://"):]).read_text(encoding="utf-8")
    (D / ("secret-" + name.replace("/", "_"))).write_text(value, encoding="utf-8")
    out({"Name": name})
# PMVA_STUB_UNDELETABLE=A,B: そのリソースを --retain-resources で残すまでスタックの削除が失敗する
UNDELETABLE = [x for x in os.environ.get("PMVA_STUB_UNDELETABLE", "").split(",") if x]
retained_file = D / "retained.json"
retained = json.loads(retained_file.read_text()) if retained_file.exists() else []
if cmd == "cloudformation delete-stack":
    retained_file.write_text(json.dumps(a[a.index("--retain-resources") + 1:] if "--retain-resources" in a else []))
    sys.exit(0)
if cmd == "cloudformation wait":
    sys.exit(0 if all(u in retained for u in UNDELETABLE) else 255)
if cmd == "cloudformation describe-stack-resources":  # 依存関係の順に 1 つずつ失敗する
    pending = [u for u in UNDELETABLE if u not in retained]
    out({"StackResources": [{"LogicalResourceId": u, "PhysicalResourceId": "pmva-keepalive" if "Function" in u else "perm-1",
                             "ResourceType": "AWS::Lambda::Function" if "Function" in u else "AWS::Lambda::Permission",
                             "ResourceStatus": "DELETE_FAILED"} for u in pending[:1]]})
# s3 cp (手元 → S3) は s3/<バケット>/<キー> に置き、s3api head-object はそこを見る
paths = [x for i, x in enumerate(a[2:], start=2) if not x.startswith("--") and a[i - 1] != "--metadata"]
if cmd == "s3 cp" and len(paths) == 2 and paths[1].startswith("s3://"):
    dest = D / "s3" / paths[1][len("s3://"):]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(Path(paths[0]).read_bytes())
    if opt("--metadata"):   # --metadata pmva-origin=x は <キー>.metadata に書く (オブジェクトとは別のファイル)
        Path(str(dest) + ".metadata").write_text(opt("--metadata"), encoding="utf-8")
    sys.exit(0)
if cmd == "s3 cp" and len(paths) == 2 and paths[0].startswith("s3://"):   # S3 → 手元
    src = D / "s3" / paths[0][len("s3://"):]
    if not src.is_file():
        fail("An error occurred (404) when calling the HeadObject operation: Key \"%s\" does not exist" % paths[0])
    if paths[1] == "-":   # 標準出力へ (mvm result が VM 終了後に最後の応答を読む)
        sys.stdout.buffer.write(src.read_bytes())
        sys.exit(0)
    Path(paths[1]).write_bytes(src.read_bytes())
    sys.exit(0)
if cmd in ("s3api put-object", "s3api get-object"):
    # put-object は s3 cp と同じ場所に置き、--if-match / --if-none-match を本物の S3 と同じく確かめる。
    # get-object は最後の引数 (保存先) に書く。ETag は中身の MD5
    import hashlib
    obj = D / "s3" / opt("--bucket") / opt("--key")
    if cmd == "s3api get-object":
        if not obj.is_file():
            fail("An error occurred (NoSuchKey) when calling the GetObject operation: The specified key does not exist.")
        Path(a[-1]).write_bytes(obj.read_bytes())
        out({"ETag": '"%s"' % hashlib.md5(obj.read_bytes()).hexdigest(), "ContentLength": obj.stat().st_size})
    precondition = "An error occurred (PreconditionFailed) when calling the PutObject operation: At least one of the pre-conditions you specified did not hold"
    if opt("--if-none-match") == "*" and obj.is_file():
        fail(precondition)
    if opt("--if-match") and (not obj.is_file() or hashlib.md5(obj.read_bytes()).hexdigest() != opt("--if-match").strip('"')):
        fail(precondition)
    obj.parent.mkdir(parents=True, exist_ok=True)
    obj.write_bytes(Path(opt("--body")).read_bytes())
    if opt("--metadata"):
        Path(str(obj) + ".metadata").write_text(opt("--metadata"), encoding="utf-8")
    out({"ETag": '"%s"' % hashlib.md5(obj.read_bytes()).hexdigest()})
if cmd == "s3api list-objects-v2":   # s3/<バケット>/ の下のうち、--prefix で始まるキー。ETag は中身の MD5 (PMVA_STUB_ETAG_<名前> で上書き)
    import hashlib
    root = D / "s3" / opt("--bucket")
    prefix = opt("--prefix") or ""
    contents = []
    for f in sorted(root.rglob("*")) if root.exists() else []:
        key = f.relative_to(root).as_posix()
        if f.is_file() and key.startswith(prefix) and not key.endswith(".metadata"):
            if opt("--delimiter") and opt("--delimiter") in key[len(prefix):]:
                continue    # 本物の S3 と同じく、delimiter を含むキーは Contents に入れない (CommonPrefixes になる)
            etag = os.environ.get("PMVA_STUB_ETAG_" + key.rsplit("/", 1)[-1].replace(".", "_").replace("-", "_"))                 or hashlib.md5(f.read_bytes()).hexdigest()
            contents.append({"Key": key, "ETag": '"%s"' % etag, "Size": f.stat().st_size})
    out({"Contents": contents} if contents else {})
if cmd == "s3api delete-object":   # mvm memory rm。S3 のオブジェクトと --metadata の記録を消す (無くても成功する。本物と同じ)
    obj = D / "s3" / opt("--bucket") / opt("--key")
    obj.unlink(missing_ok=True)
    Path(str(obj) + ".metadata").unlink(missing_ok=True)
    out({})
if cmd == "s3api head-object":
    obj = D / "s3" / opt("--bucket") / opt("--key")
    if obj.exists():
        out({"ContentLength": obj.stat().st_size})
    fail("An error occurred (404) when calling the HeadObject operation: Not Found")
if cmd == "cloudformation deploy":
    params = a[a.index("--parameter-overrides") + 1:]
    params = params[:next((i for i, x in enumerate(params) if x.startswith("--")), len(params))]
    # 本物の CloudFormation は、テンプレートの ASCII 以外の文字 (日本語のコメントや Description) を
    # 1 文字ずつ ? に置き換えて保存する (2026-11-20 に実機で確認)
    template = Path(opt("--template-file")).read_text(encoding="utf-8")
    deployed_file.write_text(json.dumps({
        "template": "".join(c if ord(c) < 128 else "?" for c in template),
        "parameters": dict(x.split("=", 1) for x in params)}), encoding="utf-8")
    sys.exit(0)
if cmd == "s3 rm":
    sys.exit(0)
if cmd == "ce get-cost-and-usage":  # 2 ページに分けて返す
    def group(service, amount):
        return {"Keys": [service], "Metrics": {"UnblendedCost": {"Amount": amount, "Unit": "USD"}}}
    if opt("--next-page-token") == "p2":
        out({"ResultsByTime": [{"TimePeriod": {"Start": "2026-10-28"}, "Groups": [group("AWS Lambda", "1.25")]}]})
    out({"NextPageToken": "p2", "ResultsByTime": [
        {"TimePeriod": {"Start": "2026-10-27"}, "Groups": [group("AWS Lambda", "2.5"), group("Amazon S3", "0.001")]}]})
if cmd == "logs describe-log-groups":
    out({"logGroups": []})
fail("unhandled: " + cmd, 1)
