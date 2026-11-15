#!/usr/bin/env bash
# エージェントの状態 (作業中・完了・入力待ち) を記録し、設定があれば Webhook (Slack / Discord / Teams のワークフローなど) に通知する。
#   Claude Code: settings.json の hooks から呼ばれ、イベントの JSON を標準入力で受け取る
#     notify.sh claude-prompt        指示を受け取った (UserPromptSubmit)     → 作業中
#     notify.sh claude-tool          ツールを実行した (PostToolUse)          → 作業中
#     notify.sh claude-stop          応答が終わった (Stop)                   → 完了
#     notify.sh claude-notification  許可待ち・入力待ち (Notification)       → 入力待ち
#   Codex: config.toml の notify から呼ばれ、イベントの JSON を最後の引数で受け取る
#     notify.sh codex '<json>'       応答が終わった (agent-turn-complete)    → 完了
#   supervisor.py から呼ばれる
#     notify.sh oom <回数>            メモリ不足でプロセスが強制終了された (通知だけ)
# 状態は ~/.pmva/agents/<claude|codex>.json に書き、supervisor.py の /status と /result が読む (mvm wait / mvm result)。
# 失敗してもエージェントの動作を止めないよう、常に 0 で終わる。
set -u

kind="${1:-}"
state_dir="$HOME/.pmva/agents"

# record <agent> <state> [<追加する項目の JSON>]
record() {
  mkdir -p "$state_dir" 2>/dev/null || return 0
  local f="$state_dir/$1.json"
  jq -nc --arg s "$2" --argjson x "${3:-null}" '{state: $s, at: now} + ($x // {})' > "$f.tmp.$$" 2>/dev/null \
    && mv "$f.tmp.$$" "$f" || rm -f "$f.tmp.$$"
}

current() {
  jq -r '.state // empty' "$state_dir/$1.json" 2>/dev/null
}

case "$kind" in
  claude-prompt|claude-tool)
    cat > /dev/null
    record claude working
    exit 0
    ;;
  claude-stop)
    event="$(cat)"
    # 最後の応答の文章 (last_assistant_message) は、渡されない版のために会話記録の場所 (transcript_path) も残す
    record claude "done" "$(jq -c '{transcript: (.transcript_path // ""), message: (.last_assistant_message // "")}' <<<"$event" 2>/dev/null)"
    msg="Claude Code の応答が終わりました"
    ;;
  claude-notification)
    event="$(cat)"
    message="$(jq -r '.message // "入力を待っています"' <<<"$event" 2>/dev/null)"
    # 応答が終わってしばらく入力がないときの通知 (idle_prompt) では、「完了」を「入力待ち」で上書きしない
    if [ "$(jq -r '.notification_type // empty' <<<"$event" 2>/dev/null)" != idle_prompt ] || [ "$(current claude)" != "done" ]; then
      record claude waiting "$(jq -nc --arg m "$message" '{message: $m}')"
    fi
    msg="Claude Code: $message"
    ;;
  codex)
    event="${2:-}"
    [ "$(jq -r '.type // empty' <<<"$event" 2>/dev/null)" = "agent-turn-complete" ] || exit 0
    record codex "done" "$(jq -c '{message: (."last-assistant-message" // "")}' <<<"$event" 2>/dev/null)"
    # 文字単位で切り詰める (head -c だと日本語の途中で切れて文字化けする)
    last="$(jq -r '(."last-assistant-message" // "")[0:300]' <<<"$event" 2>/dev/null)"
    msg="Codex の応答が終わりました${last:+: $last}"
    ;;
  oom)
    msg="メモリ不足でプロセスが強制終了されました (このセッションで ${2:-?} 回目)。MEMORY_MIB を増やすか、同時に動かすテストやブラウザを減らしてください"
    ;;
  *)
    exit 0
    ;;
esac

url_file="$HOME/.pmva/secrets/notify_webhook"
[ -s "$url_file" ] || exit 0
url="$(cat "$url_file")"
name="$(jq -r '.name // "pmva"' "$HOME/.pmva/session.json" 2>/dev/null || echo pmva)"

text="[$name] $msg"
# Discord は content、それ以外 (Slack など) は text を読む
case "$url" in
  *discord.com/*|*discordapp.com/*) body="$(jq -nc --arg t "$text" '{content: $t}')" ;;
  *) body="$(jq -nc --arg t "$text" '{text: $t}')" ;;
esac
curl -fsS --max-time 10 -H 'Content-Type: application/json' -d "$body" "$url" >/dev/null 2>&1 || true
exit 0
