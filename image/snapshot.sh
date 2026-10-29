#!/usr/bin/env bash
# ワークスペース (~/work) とエージェントの会話履歴 (~/.claude, ~/.codex) を S3 に保存する。
#   s3://<bucket>/workspaces/<name>/latest.tar.gz           ... 復元に使う最新版
#   s3://<bucket>/workspace-history/<name>/<ts>.tar.gz      ... 履歴 (ライフサイクルで自動削除)
# 認証情報ファイルは保存対象から除外する。
set -euo pipefail
umask 077

REASON="${1:-manual}"
SESSION="$HOME/.pmva/session.json"
[ -f "$SESSION" ] || { echo "no session; skip"; exit 0; }

BUCKET="$(jq -r '.snapshotBucket // empty' "$SESSION")"
NAME="$(jq -r '.name // empty' "$SESSION")"
REGION="$(jq -r '.region // empty' "$SESSION")"
[ -n "$BUCKET" ] && [ -n "$NAME" ] || { echo "snapshot disabled; skip"; exit 0; }
export AWS_REGION="$REGION" AWS_DEFAULT_REGION="$REGION"

paths=()
for p in work .claude .codex; do
  [ -e "$HOME/$p" ] && paths+=("$p")
done
[ "${#paths[@]}" -gt 0 ] || { echo "nothing to save"; exit 0; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
ts="$(date -u +%Y%m%dT%H%M%SZ)"

# tar は「読み取り中にファイルが変化した」場合に 1 を返すが、作業中の保存なので許容する
rc=0
tar -czf "$tmp/ws.tar.gz" -C "$HOME" \
  --exclude='.claude/.credentials.json' \
  --exclude='.codex/auth.json' \
  --exclude='node_modules' \
  --exclude='.venv' \
  --exclude='.cache' \
  "${paths[@]}" || rc=$?
[ "$rc" -le 1 ] || { echo "tar failed: $rc"; exit "$rc"; }

aws s3 cp --only-show-errors "$tmp/ws.tar.gz" "s3://$BUCKET/workspace-history/$NAME/$ts.tar.gz"
aws s3 cp --only-show-errors "$tmp/ws.tar.gz" "s3://$BUCKET/workspaces/$NAME/latest.tar.gz"

echo "$ts $REASON $(du -h "$tmp/ws.tar.gz" | cut -f1)" > "$HOME/.pmva/last_snapshot"
echo "saved s3://$BUCKET/workspaces/$NAME/latest.tar.gz ($ts, $REASON)"
