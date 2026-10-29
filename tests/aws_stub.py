"""テスト用の AWS CLI スタブ。mvm が呼ぶコマンドだけに JSON で答える。

状態は環境変数 PMVA_STUB_DIR のディレクトリに置く:
  calls.jsonl          呼び出された引数
  <microvmId>.payload  run-microvm の --run-hook-payload
  <microvmId>.term     terminate 済みの印
  secret-<name>        create-secret / put-secret-value で受け取った値
"""

import json
import os
import sys
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
if cmd == "lambda get-function":  # PMVA_STUB_LAMBDA=exists / denied
    mode = os.environ.get("PMVA_STUB_LAMBDA", "")
    if mode == "exists":
        out({"Configuration": {"FunctionName": opt("--function-name")}})
    fail("AccessDeniedException" if mode == "denied" else "ResourceNotFoundException")
if cmd == "cloudformation describe-stacks":
    out({"Stacks": [{"Outputs": [{"OutputKey": k, "OutputValue": v} for k, v in {
        "ArtifactBucketName": "pmva-bucket",
        "BuildRoleArn": "arn:aws:iam::123456789012:role/build",
        "ExecutionRoleArn": "arn:aws:iam::123456789012:role/exec",
        "ExecutionRoleClaudeArn": "arn:aws:iam::123456789012:role/exec-claude",
        "ExecutionRoleCodexArn": "arn:aws:iam::123456789012:role/exec-codex",
        "ExecutionRoleNoneArn": "arn:aws:iam::123456789012:role/exec-none",
        "LogGroupName": "/aws/lambda-microvms/pmva",
    }.items()]}]})
if cmd == "service-quotas get-service-quota":
    out({"Quota": {"QuotaCode": opt("--quota-code"), "Value": float(os.environ.get("PMVA_STUB_QUOTA_GB", "8"))}})
if cmd == "lambda-microvms list-managed-microvm-images":
    out({"items": [{"imageArn": "arn:aws:lambda:ap-northeast-1:aws:microvm-image:al2023-1"}]})
if cmd == "lambda-microvms list-microvm-images":
    out({"items": [{"name": "pmva-agent", "imageArn": "arn:img", "state": "CREATED"}]})
if cmd == "lambda-microvms get-microvm-image":
    out({"latestActiveImageVersion": "1.0"})
if cmd == "lambda-microvms run-microvm":
    vid = "microvm-%04d" % len(list(D.glob("microvm-*.payload")))
    payload = opt("--run-hook-payload")
    if payload.startswith("file://"):
        payload = Path(payload[len("file://"):]).read_text(encoding="utf-8")
    (D / f"{vid}.payload").write_text(payload, encoding="utf-8")
    (D / f"{vid}.args").write_text(json.dumps(a), encoding="utf-8")
    out({"microvmId": vid, "state": "PENDING", "endpoint": f"https://{vid}.lambda-microvm.example.on.aws",
         "imageVersion": "1.0", "startedAt": 1793201909.5})
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
    if (D / f"{vid}.term").exists():
        out({"state": "TERMINATED"})
    out({"state": "SUSPENDED" if (D / f"{vid}.suspended").exists() else "RUNNING"})
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
if cmd in ("s3 rm", "cloudformation deploy"):
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
