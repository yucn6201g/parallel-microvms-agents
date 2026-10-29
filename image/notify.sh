#!/usr/bin/env bash
# エージェントの完了・入力待ちを Webhook (Slack / Discord / Teams のワークフローなど) に通知する。
#   Claude Code: settings.json の hooks から呼ばれ、イベントの JSON を標準入力で受け取る
#     notify.sh claude-stop          応答が終わった (Stop)
#     notify.sh claude-notification  許可待ち・入力待ち (Notification)
#   Codex: config.toml の notify から呼ばれ、イベントの JSON を最後の引数で受け取る
#     notify.sh codex '<json>'
# 通知に失敗してもエージェントの動作を止めないよう、常に 0 で終わる。
set -u

kind="${1:-}"
url_file="$HOME/.pmva/secrets/notify_webhook"
[ -s "$url_file" ] || exit 0
url="$(cat "$url_file")"
name="$(jq -r '.name // "pmva"' "$HOME/.pmva/session.json" 2>/dev/null || echo pmva)"

case "$kind" in
  claude-stop)
    event="$(cat)"
    msg="Claude Code の応答が終わりました"
    ;;
  claude-notification)
    event="$(cat)"
    msg="Claude Code: $(jq -r '.message // "入力を待っています"' <<<"$event" 2>/dev/null)"
    ;;
  codex)
    event="${2:-}"
    [ "$(jq -r '.type // empty' <<<"$event" 2>/dev/null)" = "agent-turn-complete" ] || exit 0
    # 文字単位で切り詰める (head -c だと日本語の途中で切れて文字化けする)
    last="$(jq -r '(."last-assistant-message" // "")[0:300]' <<<"$event" 2>/dev/null)"
    msg="Codex の応答が終わりました${last:+: $last}"
    ;;
  *)
    exit 0
    ;;
esac

text="[$name] $msg"
# Discord は content、それ以外 (Slack など) は text を読む
case "$url" in
  *discord.com/*|*discordapp.com/*) body="$(jq -nc --arg t "$text" '{content: $t}')" ;;
  *) body="$(jq -nc --arg t "$text" '{text: $t}')" ;;
esac
curl -fsS --max-time 10 -H 'Content-Type: application/json' -d "$body" "$url" >/dev/null 2>&1 || true
exit 0
