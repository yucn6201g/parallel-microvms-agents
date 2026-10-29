#!/bin/sh
# macOS / Linux 用のセットアップ: 前提ツールの確認、config.env の作成、mvm エイリアスの登録。
# 何度実行しても同じ結果になる (エイリアスの行は書き換える)。
#
#   scripts/install.sh            # ログインシェルに合わせて ~/.zshrc か ~/.bashrc に登録
#   scripts/install.sh --rc FILE  # 登録先を指定
#   scripts/install.sh --no-alias # エイリアスは登録しない
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
RC=""
ALIAS=true
while [ $# -gt 0 ]; do
  case "$1" in
    --rc) RC=${2:?--rc にはファイル名が必要です}; shift 2 ;;
    --no-alias) ALIAS=false; shift ;;
    -h|--help) sed -n '2,8p' "$0"; exit 0 ;;
    *) echo "不明な引数: $1" >&2; exit 2 ;;
  esac
done

missing=0
ok() { echo "[OK] $1"; }
ng() { echo "[NG] $1"; missing=$((missing + 1)); }

if command -v python3 >/dev/null 2>&1 && python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))'; then
  ok "$(python3 --version 2>&1)"
else
  ng "Python 3.9 以上が見つかりません (macOS: brew install python / https://www.python.org/downloads/)"
fi
if command -v aws >/dev/null 2>&1; then
  v=$(aws --version 2>&1) || v="error:$(echo "$v" | tail -1)"
  case "$v" in
    error:*) ng "aws --version が失敗しました: ${v#error:}" ;;
    *) ok "$v (lambda-microvms に対応しているかは mvm doctor で確認します)" ;;
  esac
else
  ng "AWS CLI v2 が見つかりません https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html"
fi
if command -v ssh >/dev/null 2>&1; then
  ok "$(ssh -V 2>&1)"
else
  ng "OpenSSH クライアント (ssh) が見つかりません"
fi

CONFIG=${PMVA_CONFIG:-$ROOT/config.env}  # mvm と同じく PMVA_CONFIG で置き場所を変えられる
if [ -f "$CONFIG" ]; then
  ok "config.env はすでにあります (上書きしません): $CONFIG"
else
  cp "$ROOT/config.example.env" "$CONFIG"
  ok "config.env を作成しました。AWS_REGION などを確認してください: $CONFIG"
fi
chmod +x "$ROOT/bin/mvm"

if [ "$ALIAS" = true ]; then
  if [ -z "$RC" ]; then
    case "${SHELL:-}" in
      */zsh) RC="$HOME/.zshrc" ;;
      *) RC="$HOME/.bashrc" ;;
    esac
  fi
  line="alias mvm='$ROOT/bin/mvm' # pmva"
  touch "$RC"
  tmp="$RC.pmva-tmp"
  grep -v ' # pmva$' "$RC" > "$tmp" || true
  echo "$line" >> "$tmp"
  cat "$tmp" > "$RC"
  rm -f "$tmp"
  ok "エイリアスを登録しました: $RC (新しいターミナルを開くか source $RC で有効になります)"
fi

echo
if [ "$missing" -gt 0 ]; then
  echo "足りないツールが $missing 個あります。入れてから mvm doctor で確認してください"
else
  echo "次は: mvm doctor → mvm infra deploy (docs/setup.md)"
fi
