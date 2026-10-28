#!/usr/bin/env bash
# /run フック後に 1 回だけ実行されるセッション初期化。
# ~/.pmva/session.json (RunMicrovm の runHookPayload) を読み、
#   1. 認証情報を Secrets Manager から取得してエージェントを設定
#   2. ワークスペースを復元 / リポジトリを clone
#   3. SSH over WebSocket ブリッジを起動 (公開鍵が渡された場合)
#   4. tmux セッション "main" でエージェントを起動
# 認証情報は実行ロール (IMDSv2) 経由で取得する。イメージには一切焼き込まない。
set -euo pipefail
umask 077

PMVA="$HOME/.pmva"
SESSION="$PMVA/session.json"
STATE="$PMVA/state"
ENV_FILE="$PMVA/env.sh"
SECRETS_DIR="$PMVA/secrets"

mark() { echo "$*" > "$STATE"; }
log() { echo "[bootstrap] $(date -u +%H:%M:%S) $*"; }
warn() { log "WARN: $*"; WARNINGS="${WARNINGS:+$WARNINGS; }$*"; }
trap 'mark "failed: line $LINENO"' ERR
WARNINGS=""

mark running
j() { jq -r "$1 // empty" "$SESSION"; }

NAME="$(j .name)"
AGENT="$(j .agent)"; AGENT="${AGENT:-claude}"
REGION="$(j .region)"
export AWS_REGION="${REGION:-${AWS_REGION:-}}"
export AWS_DEFAULT_REGION="$AWS_REGION"

secret() { aws secretsmanager get-secret-value --secret-id "$1" --query SecretString --output text; }
addenv() { printf 'export %s=%q\n' "$1" "$2" >> "$ENV_FILE"; }

mkdir -p "$PMVA" "$SECRETS_DIR" "$HOME/.claude" "$HOME/.codex" "$HOME/work"
: > "$ENV_FILE"
addenv PMVA_SESSION "$NAME"

# ---------------------------------------------------------------- Claude Code
CLAUDE_AUTH="$(j .claudeAuth)"; CLAUDE_AUTH="${CLAUDE_AUTH:-none}"
CLAUDE_SECRET="$(j .claudeSecret)"
log "claude auth: $CLAUDE_AUTH"
case "$CLAUDE_AUTH" in
  api-key)
    # apiKeyHelper を使うと起動時の「この API キーを使いますか？」確認が出ない
    secret "$CLAUDE_SECRET" > "$SECRETS_DIR/anthropic_api_key"
    printf '#!/bin/sh\ncat %q\n' "$SECRETS_DIR/anthropic_api_key" > "$PMVA/anthropic-key-helper"
    chmod 700 "$PMVA/anthropic-key-helper"
    settings="$HOME/.claude/settings.json"
    [ -s "$settings" ] || echo '{}' > "$settings"
    jq --arg h "$PMVA/anthropic-key-helper" '.apiKeyHelper = $h' "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
    ;;
  oauth-token)
    # ローカル PC で `claude setup-token` して得た長期トークン (Pro/Max サブスクリプション)
    addenv CLAUDE_CODE_OAUTH_TOKEN "$(secret "$CLAUDE_SECRET")"
    ;;
  bedrock)
    # 実行ロールの認証情報 (IMDSv2) で Amazon Bedrock を呼ぶ
    addenv CLAUDE_CODE_USE_BEDROCK 1
    bedrock_region="$(j .bedrockRegion)"
    addenv AWS_REGION "${bedrock_region:-$AWS_REGION}"
    ;;
  gateway)
    # LLM ゲートウェイ / プロキシ経由: シークレットは {"baseUrl": "...", "authToken": "..."}
    gw="$(secret "$CLAUDE_SECRET")"
    addenv ANTHROPIC_BASE_URL "$(jq -r .baseUrl <<<"$gw")"
    addenv ANTHROPIC_AUTH_TOKEN "$(jq -r .authToken <<<"$gw")"
    ;;
  interactive|none) ;;  # interactive: 接続後に claude を起動して /login する
  *) warn "unknown claudeAuth: $CLAUDE_AUTH" ;;
esac
CLAUDE_MODEL="$(j .claudeModel)"
[ -z "$CLAUDE_MODEL" ] || addenv ANTHROPIC_MODEL "$CLAUDE_MODEL"

# ---------------------------------------------------------------- Codex
CODEX_AUTH="$(j .codexAuth)"; CODEX_AUTH="${CODEX_AUTH:-none}"
CODEX_SECRET="$(j .codexSecret)"
log "codex auth: $CODEX_AUTH"
case "$CODEX_AUTH" in
  api-key)
    key="$(secret "$CODEX_SECRET")"
    addenv OPENAI_API_KEY "$key"
    printf '%s' "$key" | codex login --with-api-key >/dev/null 2>&1 || warn "codex login --with-api-key failed"
    unset key
    ;;
  chatgpt)
    # ローカル PC で ChatGPT ログイン済みの ~/.codex/auth.json をそのまま配置
    secret "$CODEX_SECRET" > "$HOME/.codex/auth.json"
    ;;
  interactive|none) ;;  # interactive: 接続後に `codex login --device-auth`
  *) warn "unknown codexAuth: $CODEX_AUTH" ;;
esac

# ---------------------------------------------------------------- Git / GitHub
GITHUB_SECRET="$(j .githubSecret)"
if [ -n "$GITHUB_SECRET" ]; then
  log "github token: $GITHUB_SECRET"
  token="$(secret "$GITHUB_SECRET")"
  printf 'https://x-access-token:%s@github.com\n' "$token" > "$HOME/.git-credentials"
  git config --global credential.helper store
  addenv GH_TOKEN "$token"
  unset token
fi
[ -z "$(j .gitUserName)" ] || git config --global user.name "$(j .gitUserName)"
[ -z "$(j .gitUserEmail)" ] || git config --global user.email "$(j .gitUserEmail)"
git config --global init.defaultBranch main
git config --global push.autoSetupRemote true

# ---------------------------------------------------------------- ワークスペース
RESTORE="$(j .restore)"
if [ -n "$RESTORE" ]; then
  log "restore workspace from $RESTORE"
  if aws s3 cp "$RESTORE" - | tar -xzf - -C "$HOME"; then
    log "restored"
  else
    warn "restore failed: $RESTORE"
  fi
fi

REPO="$(j .repo)"
WORKDIR="$HOME/work"
if [ -n "$REPO" ]; then
  dir="$HOME/work/$(basename "$REPO" .git)"
  BRANCH="$(j .branch)"
  BASE="$(j .baseBranch)"
  if [ ! -d "$dir/.git" ]; then
    log "clone $REPO"
    git clone "$REPO" "$dir"
  else
    git -C "$dir" fetch --all --prune || warn "git fetch failed"
  fi
  if [ -n "$BRANCH" ]; then
    if git -C "$dir" rev-parse --verify --quiet "$BRANCH" >/dev/null; then
      git -C "$dir" checkout "$BRANCH"
    elif git -C "$dir" rev-parse --verify --quiet "origin/$BRANCH" >/dev/null; then
      git -C "$dir" checkout -b "$BRANCH" --track "origin/$BRANCH"
    else
      # 作業ブランチの上流を base にしない (誤って base に push しないように)
      git -C "$dir" checkout --no-track -b "$BRANCH" ${BASE:+"origin/$BASE"}
    fi
  fi
  WORKDIR="$dir"
fi
addenv PMVA_WORKDIR "$WORKDIR"

# 無人で起動できるよう、初回オンボーディングと作業ディレクトリの「信頼しますか？」確認を済ませておく
claude_json="$HOME/.claude.json"
[ -s "$claude_json" ] || echo '{}' > "$claude_json"
jq --arg d "$WORKDIR" '.hasCompletedOnboarding = true | .projects[$d].hasTrustDialogAccepted = true' \
  "$claude_json" > "$claude_json.tmp" && mv "$claude_json.tmp" "$claude_json"
grep -qF "[projects.\"$WORKDIR\"]" "$HOME/.codex/config.toml" 2>/dev/null \
  || printf '\n[projects."%s"]\ntrust_level = "trusted"\n' "$WORKDIR" >> "$HOME/.codex/config.toml"

# ---------------------------------------------------------------- SSH over WebSocket
SSH_KEY="$(j .sshPublicKey)"
if [ -n "$SSH_KEY" ]; then
  log "start sshd bridge"
  ssh_dir="$PMVA/ssh"
  mkdir -p "$ssh_dir" "$HOME/.ssh"
  # ホスト鍵はスナップショットで共有されないよう VM ごとにここで生成する
  [ -f "$ssh_dir/host_ed25519" ] || ssh-keygen -q -t ed25519 -N '' -f "$ssh_dir/host_ed25519"
  printf '%s\n' "$SSH_KEY" > "$HOME/.ssh/authorized_keys"
  chmod 700 "$HOME/.ssh"; chmod 600 "$HOME/.ssh/authorized_keys"
  cat > "$ssh_dir/sshd_config" <<EOF
Port 2222
ListenAddress 127.0.0.1
HostKey $ssh_dir/host_ed25519
PidFile $ssh_dir/sshd.pid
AuthorizedKeysFile $HOME/.ssh/authorized_keys
PasswordAuthentication no
KbdInteractiveAuthentication no
PubkeyAuthentication yes
UsePAM no
StrictModes no
AllowTcpForwarding yes
PrintMotd no
Subsystem sftp internal-sftp
EOF
  # 非 root の sshd は自分自身 (agent) としてのログインのみ受け付ける
  /usr/sbin/sshd -f "$ssh_dir/sshd_config" -E "$ssh_dir/sshd.log" || warn "sshd failed to start"
  nohup setsid websocat --binary -E ws-l:0.0.0.0:7681 tcp:127.0.0.1:2222 \
    > "$ssh_dir/websocat.log" 2>&1 < /dev/null &
fi

# ---------------------------------------------------------------- tmux / エージェント起動
PROMPT_TEXT="$(j .prompt)"
CLAUDE_ARGS="$(j .claudeArgs)"
CODEX_ARGS="$(j .codexArgs)"

tmux has-session -t main 2>/dev/null || tmux new-session -d -s main -n shell -c "$WORKDIR"

start_agent() {
  local window="$1" cmd="$2"
  tmux list-windows -t main -F '#{window_name}' | grep -qx "$window" && return 0
  tmux new-window -d -t main -n "$window" -c "$WORKDIR"
  # シェル経由で起動するので、エージェントを終了してもウィンドウはシェルとして残る
  tmux send-keys -t "main:$window" "$cmd" Enter
}

claude_cmd="claude${CLAUDE_ARGS:+ $CLAUDE_ARGS}"
codex_cmd="codex${CODEX_ARGS:+ $CODEX_ARGS}"
if [ -n "$PROMPT_TEXT" ]; then
  claude_cmd="$claude_cmd $(printf '%q' "$PROMPT_TEXT")"
  codex_cmd="$codex_cmd $(printf '%q' "$PROMPT_TEXT")"
fi

case "$AGENT" in
  claude) start_agent claude "$claude_cmd" ;;
  codex)  start_agent codex "$codex_cmd" ;;
  both)   start_agent claude "$claude_cmd"; start_agent codex "$codex_cmd" ;;
  none)   ;;
  *) warn "unknown agent: $AGENT" ;;
esac

if [ -n "$WARNINGS" ]; then
  mark "ready (warnings: $WARNINGS)"
else
  mark ready
fi
log "done"
