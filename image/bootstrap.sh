#!/usr/bin/env bash
# /run フック後に 1 回だけ実行されるセッション初期化。
# ~/.pmva/session.json (RunMicrovm の runHookPayload) を読み、
#   1. 認証情報を Secrets Manager から取得してエージェントを設定
#   2. ワークスペースを復元 / リポジトリを clone
#   3. SSH over WebSocket ブリッジを起動 (公開鍵が渡された場合)
#   4. tmux セッション "main" でエージェントを起動
# 認証情報は実行ロール (IMDSv2) 経由で取得する。イメージには一切焼き込まない。
# -E: 関数の中で失敗したときも ERR トラップで state を failed にする
set -Eeuo pipefail
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
# 1 行のトークン。端末の折り返しごと登録された値 (途中に空白や改行) でも使えるように、空白を取り除く
token_secret() { local v; v="$(secret "$1")" || return; printf '%s' "${v//[[:space:]]/}"; }
addenv() { printf 'export %s=%q\n' "$1" "$2" >> "$ENV_FILE"; }
# AWS のリージョン名 (設定ファイルに書くので、形を確かめてから使う)
valid_region() { [[ "$1" =~ ^[a-z]{2}(-[a-z]+)+-[0-9]+$ ]]; }
BEDROCK_REGION="$(j .bedrockRegion)"; BEDROCK_REGION="${BEDROCK_REGION:-$AWS_REGION}"

# --claude-auth custom / --codex-auth custom: シークレットに書いた環境変数 (env)、ファイル (files)、
# エージェントの設定 (claudeSettings / codexConfig) をそのまま入れる。値の {{files}} はファイルを置いた場所に置き換える。
# ファイルは ~/.pmva/secrets/<agent>-custom/ (スナップショットに含めない) に権限 600 で置く
CLAUDE_CUSTOM_SETTINGS=""
custom_auth() {
  local agent="$1" spec dir name value
  spec="$(secret "$2")"
  dir="$SECRETS_DIR/$agent-custom"
  rm -rf "$dir"; mkdir -p "$dir"
  while IFS= read -r name; do
    if [[ "$name" =~ ^[A-Za-z0-9_][A-Za-z0-9._-]*$ ]]; then
      jq -j --arg n "$name" '.files[$n] | tostring' <<<"$spec" > "$dir/$name"
    else
      warn "custom auth: skipped file $name"
    fi
  done < <(jq -r '.files // {} | keys[]' <<<"$spec")
  while IFS= read -r name; do
    if [[ "$name" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] && ! [[ "$name" =~ ^(PATH|HOME|USER|SHELL|LANG|TERM|TMUX.*|LD_.*|PMVA_.*)$ ]]; then
      value="$(jq -j --arg n "$name" --arg d "$dir" '.env[$n] | tostring | gsub("\\{\\{files\\}\\}"; $d)' <<<"$spec")"
      addenv "$name" "$value"
    else
      warn "custom auth: skipped env $name"
    fi
  done < <(jq -r '.env // {} | keys[]' <<<"$spec")
  if [ "$agent" = claude ]; then
    CLAUDE_CUSTOM_SETTINGS="$(jq -c --arg d "$dir" '.claudeSettings // empty
      | walk(if type == "string" then gsub("\\{\\{files\\}\\}"; $d) else . end)' <<<"$spec")"
  else
    CODEX_PROVIDER="$(jq -r --arg d "$dir" '.codexConfig // empty | gsub("\\{\\{files\\}\\}"; $d)' <<<"$spec")"
  fi
}

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
    token_secret "$CLAUDE_SECRET" > "$SECRETS_DIR/anthropic_api_key"
    printf '#!/bin/sh\ncat %q\n' "$SECRETS_DIR/anthropic_api_key" > "$PMVA/anthropic-key-helper"
    chmod 700 "$PMVA/anthropic-key-helper"
    settings="$HOME/.claude/settings.json"
    [ -s "$settings" ] || echo '{}' > "$settings"
    jq --arg h "$PMVA/anthropic-key-helper" '.apiKeyHelper = $h' "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
    ;;
  oauth-token)
    # ローカル PC で `claude setup-token` して得た長期トークン (Pro/Max サブスクリプション)
    # addenv の引数の中で secret が失敗しても set -e では止まらないので、いったん変数に受ける
    token="$(token_secret "$CLAUDE_SECRET")"
    addenv CLAUDE_CODE_OAUTH_TOKEN "$token"
    unset token
    ;;
  bedrock)
    # 実行ロールの認証情報 (IMDSv2) で Amazon Bedrock を呼ぶ
    addenv CLAUDE_CODE_USE_BEDROCK 1
    addenv AWS_REGION "$BEDROCK_REGION"
    ;;
  bedrock-api-key)
    # Amazon Bedrock の API キー (別の AWS アカウントの Bedrock を使うときなど)。実行ロールより優先される
    key="$(token_secret "$CLAUDE_SECRET")"
    addenv CLAUDE_CODE_USE_BEDROCK 1
    addenv AWS_BEARER_TOKEN_BEDROCK "$key"
    addenv AWS_REGION "$BEDROCK_REGION"
    unset key
    ;;
  mantle)
    # Bedrock の Mantle (Anthropic の API と同じ形の入口)。実行ロールで署名する
    addenv CLAUDE_CODE_USE_MANTLE 1
    addenv AWS_REGION "$BEDROCK_REGION"
    ;;
  anthropic-aws|anthropic-aws-api-key)
    # Claude Platform on AWS: 実行ロールで署名するか、ワークスペースの API キーを使う
    addenv CLAUDE_CODE_USE_ANTHROPIC_AWS 1
    workspace="$(j .anthropicAwsWorkspaceId)"
    [ -n "$workspace" ] || warn "anthropicAwsWorkspaceId is empty; Claude Platform on AWS needs a workspace ID"
    addenv ANTHROPIC_AWS_WORKSPACE_ID "$workspace"
    anthropic_aws_region="$(j .anthropicAwsRegion)"
    addenv AWS_REGION "${anthropic_aws_region:-$AWS_REGION}"
    if [ "$CLAUDE_AUTH" = anthropic-aws-api-key ]; then
      key="$(token_secret "$CLAUDE_SECRET")"
      addenv ANTHROPIC_AWS_API_KEY "$key"
      unset key
    fi
    ;;
  gateway)
    # LLM ゲートウェイ / プロキシ経由: シークレットは {"baseUrl": "...", "authToken": "..."}
    gw="$(secret "$CLAUDE_SECRET")"
    addenv ANTHROPIC_BASE_URL "$(jq -r .baseUrl <<<"$gw")"
    addenv ANTHROPIC_AUTH_TOKEN "$(jq -r .authToken <<<"$gw")"
    ;;
  custom) custom_auth claude "$CLAUDE_SECRET" ;;
  interactive|none) ;;  # interactive: 接続後に claude を起動して /login する
  *) warn "unknown claudeAuth: $CLAUDE_AUTH" ;;
esac
CLAUDE_MODEL="$(j .claudeModel)"
[ -z "$CLAUDE_MODEL" ] || addenv ANTHROPIC_MODEL "$CLAUDE_MODEL"

# ---------------------------------------------------------------- Codex
CODEX_AUTH="$(j .codexAuth)"; CODEX_AUTH="${CODEX_AUTH:-none}"
CODEX_SECRET="$(j .codexSecret)"
# ~/.codex/config.toml に書く接続先の設定: プロバイダーの ID と、model_providers.<ID>.* の行 (トップレベルのキー)
CODEX_PROVIDER_ID=""; CODEX_PROVIDER=""
log "codex auth: $CODEX_AUTH"
case "$CODEX_AUTH" in
  api-key)
    key="$(token_secret "$CODEX_SECRET")"
    addenv OPENAI_API_KEY "$key"
    printf '%s' "$key" | codex login --with-api-key >/dev/null 2>&1 || warn "codex login --with-api-key failed"
    unset key
    ;;
  chatgpt)
    # ローカル PC で ChatGPT ログイン済みの ~/.codex/auth.json をそのまま配置
    secret "$CODEX_SECRET" > "$HOME/.codex/auth.json"
    ;;
  access-token)
    # ChatGPT のアカウントのアクセストークン (at- で始まる個人用のトークン、またはエージェント用の JWT)。
    # ログインの保存に失敗しても、環境変数から読める
    token="$(token_secret "$CODEX_SECRET")"
    addenv CODEX_ACCESS_TOKEN "$token"
    printf '%s' "$token" | codex login --with-access-token >/dev/null 2>&1 || warn "codex login --with-access-token failed"
    unset token
    ;;
  gateway)
    # OpenAI の Responses API と同じ形の接続先 (LiteLLM、Azure OpenAI、社内ゲートウェイなど)。
    # シークレットは {"baseUrl", "apiKey"?, "apiKeyHeader"?, "queryParams"?, "headers"?}
    gw="$(secret "$CODEX_SECRET")"
    if [ -z "$(jq -r '.baseUrl // empty' <<<"$gw")" ]; then
      warn "codex gateway secret has no baseUrl"
    else
      key_file="$SECRETS_DIR/codex_gateway_key"
      if [ -n "$(jq -r '.apiKey // empty' <<<"$gw")" ]; then
        if [ -n "$(jq -r '.apiKeyHeader // empty' <<<"$gw")" ]; then
          # 決まったヘッダー (Azure OpenAI の api-key など) で送るときは、環境変数から渡す
          gw_key="$(jq -r .apiKey <<<"$gw")"
          addenv PMVA_CODEX_GATEWAY_KEY "$gw_key"
          unset gw_key
        else
          # Authorization: Bearer で送るときは、環境変数に出さずファイルから読ませる
          jq -j .apiKey <<<"$gw" > "$key_file"
        fi
      fi
      CODEX_PROVIDER_ID=pmva-gateway
      # 値は JSON の文字列 (TOML の文字列としても正しい) にして書く
      CODEX_PROVIDER="$(jq -r --arg key "$key_file" '
        def table: to_entries | map("\(.key | @json) = \(.value | tostring | @json)") | join(", ") | "{ \(.) }";
        "model_providers.pmva-gateway.name = \"pmva-gateway\"",
        "model_providers.pmva-gateway.base_url = \(.baseUrl | @json)",
        "model_providers.pmva-gateway.wire_api = \"responses\"",
        (if (.apiKey // "") != "" and (.apiKeyHeader // "") == "" then
           "model_providers.pmva-gateway.auth = { command = \"cat\", args = [\($key | @json)] }" else empty end),
        (if (.apiKey // "") != "" and (.apiKeyHeader // "") != "" then
           "model_providers.pmva-gateway.env_http_headers = { \(.apiKeyHeader | @json) = \"PMVA_CODEX_GATEWAY_KEY\" }" else empty end),
        (if (.queryParams // {}) != {} then "model_providers.pmva-gateway.query_params = \(.queryParams | table)" else empty end),
        (if (.headers // {}) != {} then "model_providers.pmva-gateway.http_headers = \(.headers | table)" else empty end)
      ' <<<"$gw")"
    fi
    unset gw
    ;;
  bedrock|bedrock-api-key)
    # Amazon Bedrock (Mantle) の OpenAI のモデル。接続先の設定は ~/.codex/config.toml に書く (フックの節で、復元の後に)
    if ! valid_region "$BEDROCK_REGION"; then
      warn "invalid Bedrock region for Codex: $BEDROCK_REGION"
    elif [ "$CODEX_AUTH" = bedrock ]; then
      # 実行ロールで署名する
      CODEX_PROVIDER_ID=amazon-bedrock
      CODEX_PROVIDER="model_providers.amazon-bedrock.aws.region = \"$BEDROCK_REGION\""
    else
      # API キーは環境変数に出さず、ファイルから読ませる (Claude Code の Bedrock のキーと混ざらないように)。
      # キーを使うときは aws (リージョン) を書けないので、接続先の URL でリージョンを決める
      token_secret "$CODEX_SECRET" > "$SECRETS_DIR/codex_bedrock_api_key"
      CODEX_PROVIDER_ID=amazon-bedrock
      CODEX_PROVIDER="model_providers.amazon-bedrock.base_url = \"https://bedrock-mantle.$BEDROCK_REGION.api.aws/openai/v1\"
model_providers.amazon-bedrock.auth = { command = \"cat\", args = [$(jq -rn --arg p "$SECRETS_DIR/codex_bedrock_api_key" '$p | @json')] }"
    fi
    if [ "$CLAUDE_AUTH" = bedrock-api-key ] && [ "$CODEX_AUTH" = bedrock ]; then
      warn "Codex also uses the Claude Bedrock API key (AWS_BEARER_TOKEN_BEDROCK); use --codex-auth bedrock-api-key or --claude-auth bedrock"
    fi
    ;;
  custom) custom_auth codex "$CODEX_SECRET" ;;   # 接続先の設定 (codexConfig) は CODEX_PROVIDER に入る
  interactive|none) ;;  # interactive: 接続後に手元から mvm login (ブラウザ) か mvm login --device
  *) warn "unknown codexAuth: $CODEX_AUTH" ;;
esac

# ---------------------------------------------------------------- 状態の記録と、完了・入力待ちの通知
# エージェントのフックから notify.sh を呼び、作業中・完了・入力待ちを ~/.pmva/agents/ に記録する (mvm wait / mvm result が使う)。
# 通知の Webhook が設定されていれば、完了と入力待ちを通知もする
NOTIFY_SECRET="$(j .notifySecret)"
if [ -n "$NOTIFY_SECRET" ]; then
  log "notify webhook: $NOTIFY_SECRET"
  secret "$NOTIFY_SECRET" > "$SECRETS_DIR/notify_webhook"
fi
# フックの設定は、スナップショットの復元 (~/.claude と ~/.codex を上書きする) の後で入れる (ワークスペースの節)

# ---------------------------------------------------------------- Git / GitHub
GITHUB_SECRET="$(j .githubSecret)"
if [ -n "$GITHUB_SECRET" ]; then
  log "github token: $GITHUB_SECRET"
  token="$(token_secret "$GITHUB_SECRET")"
  printf 'https://x-access-token:%s@github.com\n' "$token" > "$HOME/.git-credentials"
  git config --global credential.helper store
  addenv GH_TOKEN "$token"
fi
[ -z "$(j .gitUserName)" ] || git config --global user.name "$(j .gitUserName)"
[ -z "$(j .gitUserEmail)" ] || git config --global user.email "$(j .gitUserEmail)"
# コミットの作成者が設定に無ければ、GitHub トークンの持ち主 (ログイン名と noreply のアドレス) にする。
# VM の中では git が作成者を推測できず、名前かアドレスが欠けているとコミットが失敗するため。
# 個人のアドレスを VM に送らずに済み、GitHub の「アドレスを公開する push を拒否する」設定にも引っかからない
github_user() {  # トークンの持ち主を "<id> <login>" で出す。読めなければ何も出さない
  local json
  json="$(GH_TOKEN="$1" timeout 15 gh api user 2>/dev/null)" || return 0
  jq -r 'select((.id | type) == "number" and (.login | type) == "string") | "\(.id) \(.login)"' <<< "$json" 2>/dev/null || true
}
if [ -n "$GITHUB_SECRET" ] && ! { git config --global user.name && git config --global user.email; } > /dev/null; then
  gh_user="$(github_user "$token")"
  if [ -n "$gh_user" ]; then
    read -r gh_id gh_login <<< "$gh_user"
    git config --global user.name > /dev/null || git config --global user.name "$gh_login"
    git config --global user.email > /dev/null || git config --global user.email "${gh_id}+${gh_login}@users.noreply.github.com"
    log "git user: $(git config --global user.name) <$(git config --global user.email)>"
  else
    warn "git user.name / user.email are not set (could not read the GitHub token owner). Set GIT_USER_NAME and GIT_USER_EMAIL in config.env, or commits in the VM will fail"
  fi
fi
unset token
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

# ---------------------------------------------------------------- 手元の Claude Code / Codex の設定 (mvm profile push)
# ルール (CLAUDE.md / AGENTS.md)、skill、サブエージェント、コマンド。復元の後に置くので、送った設定が優先される。
# skill などは 1 つずつ丸ごと置き換える (手元で消したファイルが残らないように)。VM の中で作った別の名前のものは残す
PROFILE_URI="$(j .profile)"
if [ -n "$PROFILE_URI" ]; then
  log "apply profile $PROFILE_URI"
  profile_tmp="$(mktemp -d)"
  if aws s3 cp --only-show-errors "$PROFILE_URI" "$profile_tmp/profile.tar.gz" \
      && tar -xzf "$profile_tmp/profile.tar.gz" -C "$profile_tmp" --no-same-owner; then
    for agent in claude codex; do
      [ -d "$profile_tmp/$agent" ] || continue
      dest="$HOME/.$agent"
      mkdir -p "$dest"
      for entry in "$profile_tmp/$agent"/*; do
        [ -e "$entry" ] || continue
        item="$(basename "$entry")"
        if [ -d "$entry" ]; then
          mkdir -p "$dest/$item"
          for child in "$entry"/*; do
            [ -e "$child" ] || continue
            rm -rf "${dest:?}/$item/$(basename "$child")"
            cp -R "$child" "$dest/$item/"
          done
        else
          cp "$entry" "$dest/$item"
        fi
      done
    done
    log "profile applied"
  else
    warn "profile could not be applied: $PROFILE_URI"
  fi
  rm -rf "$profile_tmp"
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

# エージェントのフック (状態の記録と通知)。Claude Code は、利用者が自分で書いたフックを残し、pmva のフックだけを入れ直す
settings="$HOME/.claude/settings.json"
[ -s "$settings" ] || echo '{}' > "$settings"
# --claude-auth custom の claudeSettings を先に足す (pmva のフックは、その後で必ず入れ直す)
if [ -n "$CLAUDE_CUSTOM_SETTINGS" ]; then
  jq --argjson s "$CLAUDE_CUSTOM_SETTINGS" '. * $s' "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
fi
jq 'def pmva(ev; arg):
      .hooks[ev] = ([(.hooks[ev] // [])[] | select([.hooks[]?.command? // "" | startswith("/opt/pmva/notify.sh")] | any | not)]
                    + [{"hooks": [{"type": "command", "command": ("/opt/pmva/notify.sh " + arg)}]}]);
    pmva("UserPromptSubmit"; "claude-prompt") | pmva("PostToolUse"; "claude-tool")
    | pmva("Stop"; "claude-stop") | pmva("StopFailure"; "claude-stop-failure")
    | pmva("Notification"; "claude-notification")' \
  "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
# Claude Code の自動メモリを手元と共有する (mvm up --memory)。メモリのフォルダを ~/.claude/pmva-memory にして、
# S3 から取得する。同期の状態ファイルは、スナップショット (~/.claude を含む) でメモリと一緒に戻るよう ~/.claude に置く
# (メモリのフォルダの中に置くと Claude Code が読んでしまう)。保存は snapshot.sh が行う
MEMORY="$(j .memory)"
if [ -n "$MEMORY" ]; then
  log "shared memory: $MEMORY"
  memory_dir="$HOME/.claude/pmva-memory"
  mkdir -p "$memory_dir"
  jq --arg d '~/.claude/pmva-memory' '.autoMemoryDirectory = $d' "$settings" > "$settings.tmp" && mv "$settings.tmp" "$settings"
  python3 /opt/pmva/memsync.py pull --dir "$memory_dir" --uri "$MEMORY" --state "$HOME/.claude/pmva-memory.state.json" --origin "$NAME" || warn "memory pull failed: $MEMORY"
fi
# Codex: notify はトップレベルのキーなので、テーブル ([projects...]) より前に置く
codex_cfg="$HOME/.codex/config.toml"
if ! grep -q '^notify *=' "$codex_cfg" 2>/dev/null; then
  { echo 'notify = ["/opt/pmva/notify.sh", "codex"]'; cat "$codex_cfg" 2>/dev/null || true; } > "$codex_cfg.tmp"
  mv "$codex_cfg.tmp" "$codex_cfg"
elif ! grep -q '^notify *=.*/opt/pmva/notify.sh' "$codex_cfg"; then
  warn "codex notify is already set in config.toml; mvm wait cannot detect when Codex finishes"
fi
# Codex の接続先 (--codex-auth bedrock など)。前回の起動で書いたもの (スナップショットから戻る) を消してから書き直す
sed -i '/^# pmva:provider begin/,/^# pmva:provider end/d' "$codex_cfg"
# 同じキーを 2 回書くと Codex が config.toml を読めずに起動しなくなるので、重なるときは書かずに警告する
codex_conflict=""
if [ -n "$CODEX_PROVIDER_ID" ]; then
  grep -qE "^(model_provider *=|\\[model_providers\\.$CODEX_PROVIDER_ID)" "$codex_cfg" && codex_conflict=model_provider
elif [ -n "$CODEX_PROVIDER" ]; then
  # custom (codexConfig): 書くトップレベルのキーが、利用者の設定にすでにあるか
  while IFS= read -r key; do
    if grep -qE "^${key//./\\.}[[:space:]]*=" "$codex_cfg"; then codex_conflict="$key"; break; fi
  done < <(printf '%s\n' "$CODEX_PROVIDER" | sed -nE 's/^([A-Za-z0-9_.-]+)[[:space:]]*=.*/\1/p')
fi
if [ -n "$CODEX_PROVIDER" ]; then
  if [ -n "$codex_conflict" ]; then
    warn "codex config.toml already sets $codex_conflict; --codex-auth $CODEX_AUTH was not applied"
  else
    { echo "# pmva:provider begin (mvm up --codex-auth $CODEX_AUTH が起動のたびに書き直す)"
      [ -z "$CODEX_PROVIDER_ID" ] || echo "model_provider = \"$CODEX_PROVIDER_ID\""
      echo "$CODEX_PROVIDER"
      echo "# pmva:provider end"
      cat "$codex_cfg"; } > "$codex_cfg.tmp"
    mv "$codex_cfg.tmp" "$codex_cfg"
  fi
fi

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

# ---------------------------------------------------------------- リポジトリごとの準備 (.pmva/setup.sh)
# 依存関係のインストール (npm ci、uv sync)、ブラウザの用意 (npx playwright install chromium) など。
# スナップショットには node_modules / .venv / ~/.cache を含めないので、起動のたびに (復元したときも) 実行する。
# 失敗してもエージェントは起動する (エージェントがログを見て直せるように)。ログは ~/.pmva/setup.log
SETUP_SCRIPT="$WORKDIR/.pmva/setup.sh"
if [ -f "$SETUP_SCRIPT" ] && [ "$(j .skipSetup)" != "true" ]; then
  setup_timeout="$(j .setupTimeoutSeconds)"
  setup_timeout="${setup_timeout:-1800}"
  log "run $SETUP_SCRIPT (timeout ${setup_timeout}s, log: $PMVA/setup.log)"
  mark setup
  # shellcheck disable=SC1090
  if (cd "$WORKDIR" && . "$ENV_FILE" && export PATH="$HOME/.local/bin:$PATH" \
        && umask 022 && timeout "$setup_timeout" bash "$SETUP_SCRIPT") > "$PMVA/setup.log" 2>&1 < /dev/null; then
    log "setup done"
  else
    warn "setup.sh failed (exit $?). See ~/.pmva/setup.log"
  fi
  mark running
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
  # 最初の状態 (mvm wait 用)。最初の指示があれば作業中、なければ指示待ち。以降はエージェントのフックが書き換える
  local initial=ready
  [ -z "$PROMPT_TEXT" ] || initial=working
  mkdir -p "$PMVA/agents"
  jq -nc --arg s "$initial" '{state: $s, at: now}' > "$PMVA/agents/$window.json"
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
