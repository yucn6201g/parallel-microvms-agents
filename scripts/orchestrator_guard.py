#!/usr/bin/env python3
"""司令塔の PC で、重い処理 (依存関係のインストール、ビルド、テスト、ブラウザなど) を止める Claude Code のフック。

`mvm orchestrator install claude --dir <フォルダ>` が、そのフォルダの .claude/settings.local.json の
PreToolUse (Bash) に登録する。Claude Code はツールを実行する前に、その内容 (JSON) を標準入力で渡してくる。
重い処理なら終了コード 2 で止め、標準エラーの文章を Claude に返す (「MicroVM に任せて」と伝える)。

本当にこの PC で動かす必要があるときは、ユーザーの了承を得たうえで、コマンドの先頭に PMVA_ALLOW_LOCAL=1 を付ける。
Python 標準ライブラリだけで書いている (Windows / macOS / Linux で動く)。
"""

import json
import re
import sys

# コマンドの区切り (&&、||、;、|、改行) で分けた各部分の先頭が、これに当たれば止める
HEAVY = [
    r"(npm|pnpm|yarn|bun)( |$)(?!(-v|--version|view|info|ls|list|outdated|config|whoami)\b)",
    r"npx( |$)", r"bunx( |$)",
    r"(python3?|py)( -[0-9.]+)? -m (pytest|pip install|tox|nox|playwright|http\.server|unittest)",
    r"(pytest|tox|nox)( |$)", r"pip3? install", r"uv (sync|run|pip install|add)", r"poetry (install|run|add)",
    r"(playwright|cypress|lighthouse|puppeteer)( |$)", r"(google-chrome|chromium|chromium-browser|chrome)( |$)",
    r"(docker|podman)( compose)? (up|run|build|compose up)", r"docker-compose (up|run|build)",
    r"(\./)?(gradlew?|mvnw?)( |$)", r"cargo (build|test|run|bench|check)", r"go (build|test|run)( |$)",
    r"(make|cmake|ninja|bazel)( |$)", r"dotnet (build|test|run|restore)", r"swift (build|test)", r"xcodebuild( |$)",
    r"(tsc|webpack|vite|next|nuxt|jest|vitest|mocha|karma|turbo|nx)( |$)",
]
# 先頭の環境変数の設定 (FOO=1 cmd)、cd、time、sudo などは飛ばして本体を見る
PREFIX = re.compile(r"^\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*=\S*|time|sudo|nice|exec|env|command)\s+)*")
HEAVY_RE = [re.compile(p) for p in HEAVY]


def split_commands(command):
    """&&、||、;、|、改行でコマンドを分ける。引用符の中は分けない (mvm send a1 "直して; npm test" は 1 つ)。"""
    parts, buf, quote, i = [], [], None, 0
    while i < len(command):
        c = command[i]
        if quote:
            if c == "\\" and quote == '"' and i + 1 < len(command):
                buf.append(command[i:i + 2])
                i += 2
                continue
            if c == quote:
                quote = None
            buf.append(c)
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif c in ";|&\n":
            parts.append("".join(buf))
            buf = []
            if command[i:i + 2] in ("&&", "||"):
                i += 1
        else:
            buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def heavy_command(command):
    """重い処理に当たる部分を返す。なければ None。"""
    if re.search(r"(^|\s)PMVA_ALLOW_LOCAL=1(\s|$)", command):
        return None
    for part in split_commands(command):
        body = PREFIX.sub("", part).strip()
        if body.startswith("cd ") or not body:
            continue
        for pattern in HEAVY_RE:
            if pattern.match(body):
                return body
    return None


def main():
    # 入出力は UTF-8 のバイト列で扱う (Windows では標準入出力の既定が cp1252 / cp932 などになり、日本語が化けるため)
    try:
        event = json.loads(sys.stdin.buffer.read().decode("utf-8", "replace") or "null")
    except ValueError:
        return 0
    if not isinstance(event, dict) or event.get("tool_name") != "Bash":
        return 0
    command = (event.get("tool_input") or {}).get("command") or ""
    hit = heavy_command(command)
    if not hit:
        return 0
    message = (
        f"この PC は司令塔なので、重い処理はここで動かしません: {hit[:200]}\n"
        "MicroVM のエージェントに任せてください (mvm-orchestrator の skill を参照)。"
        "例: mvm send <name> \"<やってほしいこと>\" → mvm wait <name> --timeout 540 → mvm result <name>\n"
        "どうしてもこの PC で動かす必要があるときは、理由をユーザーに説明して了承を得てから、"
        "コマンドの先頭に PMVA_ALLOW_LOCAL=1 を付けて実行してください。\n")
    sys.stderr.buffer.write(message.encode("utf-8"))
    sys.stderr.flush()
    return 2


if __name__ == "__main__":
    sys.exit(main())
