# 使い方

1 台目を動かした後に、実際の作業でどう使うかを場面ごとにまとめたページです。初めての人は先に [quickstart.md](quickstart.md) を済ませてください。オプションの細かい仕様は [commands.md](commands.md) にあります。

## よく使うコマンド

| やりたいこと | コマンド |
|---|---|
| 今の状態と次に打つコマンドを知る | `mvm` (引数なし。番号を選ぶとそのまま実行できる。全部のコマンドは `mvm -h`) |
| 起動する / 前回の続きから始める | リポジトリのフォルダで `mvm up <name>` / 2 回目からはどこでも `mvm up <name>` |
| ブラウザで様子を見て操作する | `mvm ui` (一覧、画面、最後の応答、起動、送信、再開、renew、停止をまとめて) |
| 一覧と状態 | `mvm ls` (`--all` で終了済みも) |
| 画面の末尾を見る | `mvm peek <name>` (全台の状態の一覧は `mvm watch`) |
| 応答が終わるまで待つ / 最後の応答を読む | `mvm wait`、`mvm result <name>` |
| 指示を送る | `mvm send <name> "指示"` |
| 画面に入って操作する | `mvm ssh <name>` (抜けるのは Ctrl-b → d) |
| 8 時間が近い | `mvm renew <name>` |
| 一時停止 / 再開 | `mvm suspend <name>` / `mvm resume <name>` (通信が来れば自動でも再開) |
| 保存してから止める | `mvm stop <name>`、全台なら `mvm stop-all` |
| 費用を確かめる | `mvm cost --actual --days 7` |

### 似たコマンドの使い分け

**画面や状況を見る**

| やりたいこと | コマンド | 違い |
|---|---|---|
| 今の画面を 1 回だけ見る | `mvm peek <name>` | 画面の末尾と状態を表示して終わる。接続しない。**サスペンド中だと VM が起きて課金が再開する** |
| 画面を流れるまま見続ける (操作はしない) | `mvm view <name>` | 読み取り専用で tmux の画面を表示する。キーを押してもエージェントに届かない (Ctrl-b → d で抜ける) |
| 推論の過程 (指示・思考・実行・結果・応答) を追う | `mvm follow [<name>...]` | 画面ではなく会話の記録を読む。画面からはみ出した過去の分も見られる。複数台をまとめて流せる |
| 全台の状態を 1 画面で見渡す (端末) | `mvm watch` | 1 台 1 行の一覧を定期的に出す。画面の中身は出さない。サスペンド中の VM は起こさない |
| 全台の状態を見て、指示や停止もする (ブラウザ) | `mvm ui` | `watch` と同じ一覧に、画面・最後の応答の表示、指示の送信、再開、renew、停止、新しい起動が付く |
| 最後の応答 (報告) だけ読む | `mvm result <name>` | 画面ではなく応答そのものなので、長くても切れない。サスペンド中だと VM が起きる |
| 画面に入って一緒に操作する | `mvm ssh <name>` | 入力がエージェントに届く。見るだけなら `view` |

**止める・延ばす**

| やりたいこと | コマンド | 違い |
|---|---|---|
| 今のワークスペースを S3 に保存するだけ | `mvm save <name>` | VM は動き続け、課金も続く |
| しばらく使わないので一時停止する | `mvm suspend <name>` | 保存してから一時停止する。計算料金は止まり、画面やプロセスはそのまま残る。通信が来れば自動で再開し (`mvm resume` でも再開)、寿命 (8 時間) には数える。サスペンドのまま 6 時間で終了する |
| 今日の作業を終える | `mvm stop <name>` / `mvm stop-all` | 保存してから終了する。次は `mvm up <name>` で、保存したワークスペースを戻して続きから始める。プロセスや画面は残らない |
| 8 時間の寿命が近い / 新しいイメージに移りたい | `mvm renew <name>` | 保存 → 終了 → 同じ設定で起動し直して復元する。作業ブランチは引き継ぐ。保存に失敗したときは終了せずに止まる |

各コマンドの全オプションは [commands.md](commands.md) にあります。

## 1 日の流れ

```bash
# 初日: リポジトリのフォルダで起動する (今のフォルダの origin を clone する)
cd ~/src/<repo>
mvm up api-fix

# 次の日の朝: 名前だけで前回の続きから始める (repo とブランチを引き継ぎ、保存したワークスペースを復元)
mvm up api-fix

# 日中: 様子を見て、必要なら指示を足す
mvm ui                          # ブラウザで全台の一覧を見て、行をクリックして画面・応答を見たり指示を送ったりする
# (端末だけで済ませるなら)
mvm watch                       # 全台を 1 台 1 行の一覧で 10 秒ごとに表示 (Ctrl-C で終了。画面は mvm peek / view)
mvm send api-fix "テストが通ったら PR を作って"

# 夕方: 保存してから全部止める
mvm stop-all
```

同じ名前で前に起動していれば、`mvm up <name>` だけで前回の repo と作業ブランチ (`pmva/<name>`) を引き継ぎ、そのセッション名で最後に保存したワークスペースを戻します。同じ名前でまっさらから始めたいときは `--fresh` を付けます。`--repo` を明示したときは引き継がないので、続きから始めるなら `--restore latest` も付けます ([commands.md](commands.md#up))。
止め忘れが心配なら、毎晩決まった時刻に全台を保存して止める設定にしておきます ([setup.md](setup.md#毎晩の自動停止))。

## 別々のタスクを並列に進める

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
- 台数は `mvm doctor` の「同時に何台まで」以内にします。収まらなければ 1 台も起動せずに止まります
- エージェントに PR を作らせるなら、GitHub トークンは Contents と Pull requests を Read and write にしておきます ([auth.md](auth.md#github))

指示のコツ:

- **終わりの状態を書く**: 「〜して」だけでなく「テストを通して commit し、push して PR を作って」まで書くと、放っておいても成果が GitHub に残ります
- **1 台に 1 つのタスク**: 関係のない作業を 1 台に詰めると、ブランチと履歴が混ざります
- **長い指示は後から送る**: 起動時の指示は 4KB までです。長い仕様は `mvm send` で送るか、リポジトリにファイルとして置いて「docs/spec.md を読んで実装して」と書きます
- **無人で進めるなら許可の確認を省く**: VM は隔離された環境なので、`--claude-args "--dangerously-skip-permissions"` / `--codex-args "--dangerously-bypass-approvals-and-sandbox"` で起動すると、許可待ちで止まりません (`config.env` の `DEFAULT_CLAUDE_ARGS` / `DEFAULT_CODEX_ARGS`)

## 終わるまで待って、結果を読む

```bash
mvm wait                          # 全台の応答が終わるか、許可・入力待ちになるまで待つ
mvm result task-02                # task-02 の最後の応答 (報告) を読む
mvm send task-02 "レビューの指摘を直して"
mvm wait task-02 --timeout 600    # 10 分で打ち切る (終了コード 2)
```

`mvm wait` は、終わった台から 1 行ずつ表示します。

- `waiting` (許可・入力待ち): `mvm peek` で画面を見て、`mvm send` で答えるか、`mvm ssh` で入って操作します
- `error` (認証の失敗や利用上限など): 表示されたエラーと次にやることに従います
- `suspended` (一時停止中): ふつうは作業が終わった状態です。`mvm peek <name>` で再開して確かめ、作業中なら `mvm wait <name>` で待ち直します

`--json` を付けると `wait` / `result` / `ls` / `peek` の結果を JSON で受け取れるので、スクリプトや手元のエージェントから扱えます。

## 手元のエージェントに任せる (司令塔)

`mvm` のコマンドを自分で打つ代わりに、手元の Claude Code / Codex に「MicroVM で並列に直して」と頼むこともできます。`mvm orchestrator install claude` (または `codex`) で skill を入れると、エージェントがタスクを分けて起動し、終わるのを待ち、結果を確かめて報告します。詳しくは [orchestrator.md](orchestrator.md) にあります。

## 推論の過程や画面を見る

```bash
mvm follow                     # 動いている全台の推論の過程を、流れるように表示 (Ctrl-C で終了)
mvm follow task-02 --full      # 1 台だけ、省略せずに
mvm view task-02               # エージェントの画面をそのまま見る (読み取り専用。Ctrl-b → d で抜ける)
```

- `follow` は会話の記録を読むので、画面からはみ出した過去の分も追えます
- `view` ではキーを押してもエージェントに届かないので、見ているだけのときはこちらが安全です。指示を足すなら `mvm send`、一緒に操作するなら `mvm ssh` を使います

## 手元の PC を閉じて任せる

`mvm ssh` から抜けても、PC を閉じても、エージェントは AWS 上で作業を続けます。

- 作業中の VM は、AWS 側のキープアライブが起こし続けるので一時停止しません。作業が終わると、しばらくして一時停止します ([setup.md](setup.md#キープアライブ))
- 終わったことを知りたいときは、[Slack などへの通知](#完了入力待ちを通知する)を使います
- 戻ってきたら `mvm ls` → `mvm result <name>` で結果を確認します

## 完了・入力待ちを通知する

エージェントの応答が終わったときや、許可・入力を待っているときに、Slack などの Webhook に 1 行の通知を送れます。

1. Slack なら「Incoming Webhooks」で URL を発行する (Discord の Webhook、Teams のワークフローの URL でも可)
2. `mvm secret set notify-webhook` で URL を登録する
3. `config.env` で `NOTIFY_WEBHOOK=true` にするか、`mvm up <name> --notify` で起動する

| エージェント | 通知されるとき |
|---|---|
| Claude Code | 応答の完了、API のエラーで止まった、許可・入力待ち |
| Codex | 応答の完了 |

VM のメモリ不足でプロセスが強制終了されたときも通知します。通知に失敗してもエージェントは止まりません。

## 8 時間を超えて続ける

MicroVM の寿命は最大 8 時間 (`MAX_DURATION_SECONDS`) です。その前に作り直します。残りは `mvm ls` / `mvm watch` / `mvm ui` / 引数なしの `mvm` に出て、1 時間を切ると `!!` で知らせます。サスペンド中も寿命に数え、サスペンドのまま 6 時間 (`IDLE_SUSPENDED_SECONDS`) たつと終了します。

```bash
mvm renew <name>      # 保存 → 終了 → 新しい VM で復元。ブランチと設定は引き継ぐ
```

保存に失敗したときは終了せずに止まるので、作業を失いません。会話の履歴 (`~/.claude`、`~/.codex`) も復元され、エージェントは前の会話の続きから起動します (Claude Code は `claude --continue`、Codex は `codex resume --last`)。`mvm up <name> --restore latest` でも同じです。

## 画面に入って一緒に作業する

```bash
mvm ssh <name>         # ウィンドウの切り替えは Ctrl-b → n / p、抜けるのは Ctrl-b → d
```

- `shell` ウィンドウは自由に使える作業用のシェルです
- エージェントを終了しても、ウィンドウはシェルとして残ります。もう一度 `claude` (または `codex`) で起動できます
- 接続が切れても自動で再接続します

## VS Code で開く・ファイルを手元に持ってくる

```bash
mvm ssh-config --install     # ~/.ssh/config に Host pmva-<name> を書く
```

VS Code の Remote-SSH で `pmva-<name>` に接続し、`/home/agent/work/<リポジトリ名>` を開きます。初めて接続するときは接続先の OS を聞かれるので、Linux を選びます。VS Code Server は VM の側で動くので、手元の負荷は小さく済みます。
セッションを増やしたり作り直したりしたら、`--install` をもう一度実行します (pmva のブロックだけが置き換わります)。初回の接続が `Connecting with SSH timed out` で失敗したら、もう一度開き直してください。Windows でも VS Code の `remote.SSH.path` は既定のままで使えます (Windows 標準の OpenSSH と Git for Windows の ssh のどちらでも接続を確かめています)。

同じ設定で `scp` も使えます。

```bash
scp pmva-<name>:work/<リポジトリ名>/report.md .
```

成果物は、エージェントに commit と push をさせて GitHub 経由で受け取るのが確実です。

## リポジトリごとの準備 (.pmva/setup.sh)

リポジトリに `.pmva/setup.sh` を置くと、VM の起動時 (clone の後、エージェントを起動する前) に実行します。依存関係のインストールやブラウザの用意を書いておくと、エージェントがすぐにテストを動かせます。

```bash
# .pmva/setup.sh の例 (bash で実行。作業ディレクトリはリポジトリの直下)
set -e
pmva-node install                 # .node-version / .nvmrc の版の Node.js (なければイメージの Node.js 22 のまま)
npm ci
npx playwright install chromium   # ブラウザを使うテストがあるとき
uv sync                           # Python: pyproject.toml の依存関係と、指定の版の Python
```

- **起動のたびに実行する**: 保存したワークスペースには `node_modules`、`.venv`、`~/.cache` を含めないので、`--restore` で戻したときも実行します。何度実行しても同じ結果になるように書きます
- **時間の上限**: `SETUP_TIMEOUT_SECONDS` (既定 1800 秒)。準備の間、`mvm peek` には `bootstrap=setup` と出て、`mvm wait` は待ち続けます
- **失敗しても、エージェントは起動します**: `mvm peek` に `ready (warnings: setup.sh failed ...)` と出ます。ログは VM の `~/.pmva/setup.log` です
- **実行しないとき**: `mvm up <name> --no-setup`
- **root にはなれません**: OS のパッケージが要るときは、`config.env` の `IMAGE_EXTRA_PACKAGES` に書いて `mvm image build` で入れます (例: `postgresql16-server valkey sqlite`)。DB はエージェントのユーザーのまま起動します (例: `valkey-server --daemonize yes`)
- 認証情報と同じ環境で動くので、信頼できるリポジトリのスクリプトだけを実行してください

イメージに入っている開発ツール:

| ツール | 内容 |
|---|---|
| Node.js 22 | 別の版は `pmva-node install <版>` |
| uv | Python の版とパッケージ (`uv python install 3.12`、`uv sync`、`uv run pytest`)。OS の Python は 3.9 |
| Chromium の実行に必要なライブラリと日本語フォント | Playwright / Puppeteer を headless で動かせる。ブラウザ本体は `setup.sh` で入れる (`~/.cache` に VM ごとに 1 回)。`IMAGE_BROWSER_DEPS=false` で外すとイメージが約 450MB 小さくなる |
| gcc / make、git、gh、jq、tmux | |

Docker は入っておらず、GPU もありません。CPU は ARM64 だけです。

## ブラウザを使うテストや開発サーバー

- **準備**: `.pmva/setup.sh` に `npx playwright install chromium` を書きます。VM には画面がないので headless で動かします (Playwright の既定)
- **開発サーバーを手元のブラウザで見る**: `mvm forward <name> 3000` で、VM の 3000 番を手元の `http://localhost:3000/` につなぎます
- **テストの結果を見る**: トレースやスクリーンショットは、エージェントに commit させるか `scp` で持ってきます

## VM のメモリを確かめる

```bash
mvm top                        # 各 VM のメモリの使用量と、多く使っているプロセス
```

メモリ不足でプロセスが強制終了されると、`mvm top` / `peek` に `!!` で表示されます。エージェントに「テストの並列数を減らして」と指示するか、1 台のメモリを増やします。
メモリはイメージを作るときに決まるので、`config.env` の `MEMORY_MIB` を変えて `mvm image build` をやり直し、`mvm renew <name>` で移ります。大きくすると、同じクォータで同時に動かせる台数は減ります。

| 作業 | `MEMORY_MIB` の目安 |
|---|---|
| コードの読み書き、軽いテスト (Python / Node.js) | 2048 |
| 依存関係の多いビルド、テストの並列実行 | 4096 (既定) |
| ブラウザを使う E2E テスト、開発サーバーを動かしたままのテスト | 4096〜8192 |
| 大きなビルド (Rust、C++、JVM)、DB を一緒に動かす | 8192 |

## Claude Code と Codex で同じ課題を比べる

```bash
mvm up cmp-claude --agent claude --repo <URL> --prompt "<同じ指示>"
mvm up cmp-codex  --agent codex  --repo <URL> --prompt "<同じ指示>"
```

`pmva/cmp-claude` と `pmva/cmp-codex` のブランチに結果が残るので、GitHub 上で差分を比べられます。1 台で両方を動かすこともできます (`--agent both`。tmux のウィンドウが `claude` と `codex` に分かれます)。

## Remote Control (スマホや claude.ai/code から操作する)

VM の Claude Code を、claude.ai/code やスマホの Claude アプリから操作できます (Claude Code の Remote Control)。

要件:

- claude.ai のサブスクリプション (Pro / Max など) のフル権限のログイン。Team / Enterprise は、管理者が Remote Control を有効にしている必要があります
- `--claude-auth oauth-token` (`claude setup-token` のトークンは推論専用)、API キー、Bedrock、gateway、custom では使えません
- Codex では使えません (`--agent claude` か `both`)

手順:

```bash
mvm up <name> --remote-control --repo <URL>
mvm login <name>
```

1. `--remote-control` を付けると、`--claude-auth interactive` で起動します (他の `--claude-auth` を指定するとエラーになります)。`claude` のウィンドウは「`mvm login <name>` を実行してください」と表示して待ちます
2. `mvm login <name>` で表示される URL を手元のブラウザで開き、表示されたコードを貼り付けます。ログインすると、待っていた Claude Code が `claude --remote-control pmva-<name>` で自動で起動します
3. claude.ai/code か、スマホの Claude アプリのセッション一覧で `pmva-<name>` を開きます

注意:

- VM が作業を終えれば、これまでどおりサスペンドされます。Remote Control の接続は VM から外向きなので、アイドルの判定を妨げません (実機で確認)
- ログインは保存されません。`mvm renew` や `--restore` で起動した VM では、`mvm login <name>` でログインし直します
- この VM では、テレメトリなどを `DISABLE_TELEMETRY`、`DISABLE_ERROR_REPORTING`、`DISABLE_BUG_COMMAND` の個別の変数で止めています。非必須の通信をまとめて止める `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` (と `CLAUDE_CODE_DISABLE_FEATURE_FLAGS`、`DISABLE_GROWTHBOOK`) は、Remote Control が使えなくなるので外しています。ほかの VM では、これまでどおり入っています
- サスペンドから再開したあとに、Remote Control がつなぎ直すかは未確認です。つながらないときは、`mvm shell <name>` で `claude` のウィンドウに入り、`/remote-control` を実行してください

## 手元の設定を VM でも使う

リポジトリに入っている設定 (`CLAUDE.md`、`AGENTS.md`、`.claude/skills/` など) は、clone すればそのまま使われます。手元の自分用の設定 (`~/.claude/CLAUDE.md`、skill、サブエージェントなど) は、次のように送ります。

```bash
mvm profile show            # 送るものを確かめる
mvm profile push            # S3 に送る
```

`config.env` に `PROFILE=default` を書くと、次の起動から VM のエージェントに置かれます。認証情報らしい文字列を含むファイルや、`settings.json` / `config.toml` は送りません ([commands.md](commands.md#profile))。

## 複数の認証情報を使い分ける

個人用とチーム用など、別の認証情報を別名で登録して、セッションごとに選べます。

```bash
mvm secret set claude-oauth-token --name pmva/claude-oauth-token-team
mvm up t1 --claude-secret pmva/claude-oauth-token-team
```

名前は決まった接頭辞で始める必要があります ([auth.md](auth.md#複数アカウントの使い分け))。

## 費用を抑える

| やること | 効果 |
|---|---|
| 使い終わったら `mvm stop` / `mvm stop-all` | 止めれば計算料金はかからない |
| 毎晩の自動停止 (`NIGHTLY_STOP_TIME`) | 止め忘れを防ぐ |
| 軽い作業は `MEMORY_MIB=2048` | 1 台あたりの単価が約半分 |
| `IDLE_MAX_SECONDS` を短くする | 使っていない VM が早く一時停止する |
| 古いイメージを `mvm image prune` で消す | 1 版ごとの保存料金を減らす |
| 予算アラート (`BUDGET_MONTHLY_USD` / `BUDGET_EMAIL`) | 想定外の費用に早く気付く |

LLM の利用料 (サブスクリプションや API) は別です。複数台が同じ Claude のサブスクリプションを使うと、利用上限を全台で分け合います。詳しくは [cost.md](cost.md) にあります。
