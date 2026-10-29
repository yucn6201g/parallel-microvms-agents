# 使い方ガイド (場面別)

1 台目を動かした後に、実際の作業でどう使うかを場面ごとにまとめたページです。初めての人は先に [quickstart.md](quickstart.md) を済ませてください。コマンドの細かい仕様は [commands.md](commands.md) にあります。

## 1 日の流れ

```bash
# 朝: 前回の続きから始める (保存したワークスペースを復元)
mvm up api-fix --repo https://github.com/<owner>/<repo>.git --restore latest

# 日中: 様子を見て、必要なら指示を足す
mvm watch                       # 全台の画面を 60 秒ごとに表示 (Ctrl-C で終了)
mvm send api-fix "テストが通ったら PR を作って"

# 夕方: 保存してから全部止める
mvm stop-all
```

`--restore latest` は、そのセッション名で最後に保存したワークスペースを戻します。作業ブランチ (`pmva/<name>`) も同じものを使い続けます。
止め忘れが心配なら、毎晩決まった時刻に全台を保存して止める設定にしておきます (`NIGHTLY_STOP_TIME`、[setup.md](setup.md#毎晩の自動停止-止め忘れ防止))。

## 1 つのリポジトリで、別々のタスクを並列に進める

タスクを 1 行に 1 つずつ書いたファイルを用意します。

```text
ログイン画面のバリデーションを追加し、テストも書いて。終わったら PR を作って
README の古いセットアップ手順を現在の手順に合わせて直して PR を作って
API のレスポンスが遅いエンドポイントを調べて、原因と改善案を docs/perf.md にまとめて
```

```bash
mvm fleet 3 --prefix task --repo https://github.com/<owner>/<repo>.git --prompts-file tasks.txt
```

- `task-01` 〜 `task-03` が起動し、それぞれ `pmva/task-01` 〜 `pmva/task-03` のブランチで作業します。ブランチが分かれているので、お互いの変更はぶつかりません
- 進み具合は `mvm watch`、個別の確認は `mvm peek task-02`
- 指示どおりに PR を作れるよう、GitHub トークンは Contents と Pull requests を Read and write にしておきます ([auth.md](auth.md#github))

指示のコツ:

- **終わりの状態を書く**: 「〜して」だけでなく「テストを通して commit し、push して PR を作って」まで書くと、放っておいても成果が GitHub に残ります
- **1 台に 1 つのタスク**: 関係のない作業を 1 台に詰めると、ブランチと履歴が混ざります
- **長い指示は後から送る**: 起動時の指示は 4KB までです。長い仕様は `mvm send` で送るか、リポジトリにファイルとして置いて「docs/spec.md を読んで実装して」と書きます

## Claude Code と Codex で同じ課題を比べる

```bash
mvm up cmp-claude --agent claude --repo <URL> --prompt "<同じ指示>"
mvm up cmp-codex  --agent codex  --repo <URL> --prompt "<同じ指示>"
```

それぞれ `pmva/cmp-claude` と `pmva/cmp-codex` のブランチに結果が残るので、GitHub 上で差分を比べられます。1 台で両方を動かすこともできます (`--agent both`。tmux のウィンドウが `claude` と `codex` に分かれます)。

## 手元の PC を閉じて、エージェントに任せる

`mvm ssh` から抜けても (Ctrl-b → d)、PC を閉じても、エージェントは AWS 上で作業を続けます。

- 作業中の VM は、AWS 側のキープアライブが起こし続けるので一時停止しません。作業が終わると、しばらくして一時停止します ([setup.md](setup.md#pc-を閉じても作業を続ける-キープアライブ))
- 終わったことを知りたいときは、Slack などへの通知を使います (`mvm up ... --notify`、[setup.md](setup.md#完了入力待ちの通知))
- 戻ってきたら `mvm ls` → `mvm peek <name>` で結果を確認します

## 8 時間を超えて続ける

MicroVM の寿命は最大 8 時間です。その前に作り直します。

```bash
mvm renew <name>      # 保存 → 終了 → 新しい VM で復元。ブランチと設定は引き継ぐ
```

`renew` は、保存に失敗したときは終了せずに止まるので、作業を失いません。エージェントの会話の履歴 (`~/.claude`、`~/.codex`) も復元されるので、Claude Code なら `claude --continue` で前の会話の続きから再開できます。

## エージェントの画面に入って一緒に作業する

```bash
mvm ssh <name>         # tmux にアタッチ。ウィンドウの切り替えは Ctrl-b → n / p、抜けるのは Ctrl-b → d
```

- `shell` ウィンドウは自由に使える作業用のシェルです。テストを手で走らせたり、ログを見たりできます
- エージェントを終了しても、ウィンドウはシェルとして残ります。もう一度 `claude` (または `codex`) で起動できます
- 日本語が `_` に化けるときは、ターミナルの文字コードが UTF-8 になっているかを確認します

## VS Code で MicroVM の中を開く

```bash
mvm ssh-config --install     # ~/.ssh/config に Host pmva-<name> を書く
```

VS Code の Remote-SSH で `pmva-<name>` に接続し、`/home/agent/work/<リポジトリ名>` を開きます。VS Code Server は MicroVM 側で動くので、手元の負荷は小さく済みます。セッションを作り直したら `--install` をもう一度実行します。

## 中のファイルを手元に持ってくる

```bash
mvm ssh-config --install
scp pmva-<name>:work/<リポジトリ名>/report.md .
```

基本は、エージェントに commit と push をさせて GitHub 経由で受け取るのが確実です。

## 費用を抑える

| やること | 効果 |
|---|---|
| 使い終わったら `mvm stop` / `mvm stop-all` | 止めれば計算料金はかからない |
| 毎晩の自動停止 (`NIGHTLY_STOP_TIME`) | 止め忘れを防ぐ |
| 軽い作業は `MEMORY_MIB=2048` | 1 台あたりの単価が約半分 ([cost.md](cost.md)) |
| `IDLE_MAX_SECONDS` を短くする | 使っていない VM が早く一時停止する |
| 予算アラート (`BUDGET_MONTHLY_USD` / `BUDGET_EMAIL`) | 想定外の費用に早く気付く |
| `mvm cost --actual --days 7` | 実際にかかった金額を確認する |

LLM の利用料 (サブスクリプションや API) は別です。10 台が同じ Claude のサブスクリプションを使うと、利用上限を全台で分け合います。

## 複数の認証情報を使い分ける

個人用とチーム用など、別の認証情報を別名で登録して、セッションごとに選べます。

```bash
mvm secret set claude-oauth-token --name pmva/claude-oauth-token-team
mvm up t1 --claude-secret pmva/claude-oauth-token-team
```

名前は決まった接頭辞で始める必要があります ([auth.md](auth.md#複数アカウントの使い分け))。
