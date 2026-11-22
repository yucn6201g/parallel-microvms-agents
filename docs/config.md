# 設定リファレンス (config.env)

`config.env` の全項目です。テンプレートは `config.example.env` で、インストールスクリプトがコピーして作ります。
書式は `KEY=VALUE` で、行末の ` # ` から後ろはコメントです。値に空白を含むときは `"..."` で囲みます。シェルとしては実行されないので、Windows / macOS で同じファイルを使えます。

項目によって、変更が効くタイミングが違います。

| 反映 | 意味 |
|---|---|
| すぐ | 次に `mvm` を実行したときから効く |
| 起動時 | 次に `mvm up` / `fleet` で起動したセッションから効く。起動中のセッションは変わらない。`mvm renew` は前回の起動時の値を引き継ぐ (`SSH_PUBLIC_KEY_FILE` と `INGRESS_CONNECTORS` だけは今の値を使う) |
| image build | `mvm image build` で新しいイメージを作ると効く |
| infra deploy | 管理者の権限で `mvm infra deploy` をやり直すと効く |

## AWS

| 項目 | 既定 | 反映 | 説明 |
|---|---|---|---|
| `AWS_REGION` | `ap-northeast-1` | すぐ | 使うリージョン。Lambda MicroVMs に対応しているかは `mvm doctor` で確認 |
| `AWS_PROFILE` | (空) | すぐ | AWS CLI のプロファイル名。普段は `pmva-operator` だけを付けたユーザーのプロファイルにする ([setup.md](setup.md#操作用の-iam-ユーザー)) |
| `INFRA_TEMPLATE` | `full` | infra deploy | `full` (標準: キープアライブ、毎晩の自動停止、予算アラートを含む) / `minimal` (最小構成: 動かすのに必要なリソースだけ) ([setup.md](setup.md#aws-の構成を作る)) |
| `PMVA_PREFIX` | `pmva` | infra deploy | AWS のリソース名とシークレット名の接頭辞。変えると別の環境として扱われる |
| `STACK_NAME` | `pmva` | すぐ | CloudFormation のスタック名。1 つのアカウントで複数の環境を分けるときは `PMVA_PREFIX` と一緒に変える |
| `ENABLE_BEDROCK` | `true` | infra deploy | 実行ロールに Amazon Bedrock (Mantle を含む) を呼ぶ権限を付ける。Claude Code の `bedrock` / `mantle`、Codex の `bedrock` で使う |
| `ENABLE_ANTHROPIC_AWS` | `false` | infra deploy | 実行ロールに Claude Platform on AWS を呼ぶ権限を付ける。`--claude-auth anthropic-aws` で使う ([auth.md](auth.md#claude-platform-on-aws-anthropic-aws)) |
| `BUDGET_MONTHLY_USD` | `0` | infra deploy | 月額の予算 (USD)。`BUDGET_EMAIL` と両方を設定すると AWS Budgets のアラートを作る |
| `BUDGET_EMAIL` | (空) | infra deploy | 予算アラートの送り先。実績が予算の 80% を超えたとき、月末の予測が 100% を超えたときに届く |

## イメージと VM のサイズ

| 項目 | 既定 | 反映 | 説明 |
|---|---|---|---|
| `IMAGE_NAME` | `pmva-agent` | image build | MicroVM イメージの名前 |
| `BASE_IMAGE_ARN` | (空) | image build | ベースイメージ。空なら `arn:aws:lambda:<リージョン>:aws:microvm-image:al2023-1` |
| `MEMORY_MIB` | `4096` | image build | 1 台のメモリ (MiB)。2GB あたり 1 vCPU (4096 = 2 vCPU)。重いビルドなら `8192`。クォータの計算にも使う |
| `IMAGE_BROWSER_DEPS` | `true` | image build | Chromium (Playwright / Puppeteer) を動かすライブラリと日本語フォントを入れる。ブラウザを使わないなら `false` でイメージが小さくなる |
| `IMAGE_EXTRA_PACKAGES` | (空) | image build | イメージに追加する Amazon Linux 2023 のパッケージ (空白区切り)。MicroVM の中では root になれないので、DB などはここで入れる。例: `postgresql16-server valkey sqlite` |

## 並列数とライフサイクル

| 項目 | 既定 | 反映 | 説明 |
|---|---|---|---|
| `MAX_AGENTS` | `0` | すぐ | 同時に起動するセッションの上限。`0` なら MicroVM メモリのクォータで決まる台数まで。クォータより少なく絞りたいときだけ設定する |
| `MAX_DURATION_SECONDS` | `28800` | 起動時 | 1 セッションの寿命 (上限 8 時間)。過ぎると終了するので、その前に `mvm renew` で作り直す |
| `IDLE_MAX_SECONDS` | `1800` | 起動時 | 受信通信がこの秒数無いとサスペンドする。`off` で無効 |
| `IDLE_SUSPENDED_SECONDS` | `21600` | 起動時 | サスペンドのまま放置されたら終了するまでの秒数 |
| `IDLE_AUTO_RESUME` | `true` | 起動時 | 通信が来たら自動で再開する |
| `INGRESS_CONNECTORS` | `"HTTP_INGRESS SHELL_INGRESS"` | 起動時 | 受信の接続口。`SHELL_INGRESS` を外すと `mvm shell` とコンソールの Connect が使えなくなる |
| `SNAPSHOT_INTERVAL_SECONDS` | `1800` | 起動時 | ワークスペースを S3 に保存する間隔。`0` で定期保存しない |
| `PROFILE` | (空) | 起動時 | `mvm profile push` で送った手元の設定のまとまりの名前 (例: `default`)。VM の Claude Code / Codex に、手元のルール・skill・サブエージェント・コマンドを置く ([commands.md](commands.md#profile))。`mvm up --profile` / `--no-profile` で 1 回だけ変えられる |
| `SETUP_TIMEOUT_SECONDS` | `1800` | 起動時 | リポジトリの `.pmva/setup.sh` を打ち切るまでの秒数 ([usage.md](usage.md#リポジトリごとの準備-pmvasetupsh)) |
| `KEEPALIVE_RATE_MINUTES` | `5` | infra deploy | 作業中の VM を AWS 側から起こし続ける間隔 (分)。`0` で無効。`IDLE_MAX_SECONDS` より十分短くする |
| `KEEPALIVE_ACTIVE_SECONDS` | `900` | infra deploy | 最後の作業 (画面の変化や CPU の使用) からこの秒数以内なら作業中とみなす |
| `NIGHTLY_STOP_TIME` | (空) | infra deploy | 毎日この時刻 (`HH:MM`) に全台を保存してから止める。空なら止めない。テンプレートでは `23:00` |
| `NIGHTLY_STOP_TIMEZONE` | `Asia/Tokyo` | infra deploy | `NIGHTLY_STOP_TIME` のタイムゾーン |

## エージェントの既定値

すべて起動時に効き、`mvm up` のオプションでセッションごとに変えられます ([commands.md](commands.md#up))。

| 項目 | 既定 | 説明 |
|---|---|---|
| `DEFAULT_AGENT` | `claude` | `claude` / `codex` / `both` / `none` |
| `DEFAULT_CLAUDE_AUTH` | `oauth-token` | Claude Code の認証方式 ([auth.md](auth.md)) |
| `DEFAULT_CODEX_AUTH` | `api-key` | Codex の認証方式 |
| `DEFAULT_CLAUDE_ARGS` | (空) | claude の追加引数。例: `"--dangerously-skip-permissions"` (許可の確認を省く。VM は隔離されているので、使い捨ての作業なら選択肢になる) |
| `DEFAULT_CODEX_ARGS` | (空) | codex の追加引数 |
| `BEDROCK_REGION` | `AWS_REGION` と同じ | Bedrock (Mantle を含む) を呼ぶリージョン。Claude Code の `bedrock` / `bedrock-api-key` / `mantle`、Codex の `bedrock` / `bedrock-api-key` で使う |
| `ANTHROPIC_AWS_WORKSPACE_ID` | (空) | Claude Platform on AWS のワークスペース ID (`wrkspc_...`)。`--claude-auth anthropic-aws` / `anthropic-aws-api-key` では必須 |
| `ANTHROPIC_AWS_REGION` | `AWS_REGION` と同じ | Claude Platform on AWS のワークスペースのリージョン |
| `CLAUDE_MODEL` | (空) | Claude のモデル / Bedrock の推論プロファイル ID。空ならエージェントの既定 |
| `USE_GITHUB_TOKEN` | `true` | `<PMVA_PREFIX>/github-token` を clone / push に使う。トークンを登録していないなら `false` にする (登録しないまま `true` だと初期化が失敗する) |
| `NOTIFY_WEBHOOK` | `false` | `true` で、応答の完了・入力待ちを `<PMVA_PREFIX>/notify-webhook` の URL に通知する |

## Git と SSH

| 項目 | 既定 | 反映 | 説明 |
|---|---|---|---|
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | (空) | 起動時 | VM 内の git のコミット作成者。空なら GitHub トークンの持ち主のログイン名と noreply のアドレス (`<id>+<login>@users.noreply.github.com`)。トークンを使わない (`--no-github`) か、トークンから持ち主を読めない (GitHub App のトークンなど) ときは、設定しないと VM の中でコミットできない |
| `SSH_PUBLIC_KEY_FILE` | `~/.ssh/id_ed25519.pub` | 起動時 | `mvm ssh` で使う公開鍵。`~` は macOS ではホーム、Windows では `%USERPROFILE%`。ファイルが無ければ SSH の受け口を作らない (`mvm shell` は使える)。鍵なしで起動した後に鍵を作ったら `mvm renew` で鍵付きに起動し直せる |

## 例: 用途別の設定

**Bedrock だけで使う (キーの管理をしない)**

```
DEFAULT_CLAUDE_AUTH=bedrock
CLAUDE_MODEL=<Bedrock コンソールで確認した推論プロファイル ID>
DEFAULT_CODEX_AUTH=bedrock          # Codex も Bedrock の OpenAI のモデルで使う
USE_GITHUB_TOKEN=false
```

**最小構成で使う (キープアライブ・毎晩の自動停止・予算アラートを作らない)**

```
INFRA_TEMPLATE=minimal
```

予算アラート、キープアライブ、毎晩の自動停止の設定 (`BUDGET_*`、`KEEPALIVE_*`、`NIGHTLY_STOP_*`) は使われません。キープアライブが無いので、手元から接続していない VM は、作業中でも `IDLE_MAX_SECONDS` で一時停止します (`--idle off` で起動すれば止まりません)。

**費用を抑える (小さめの VM、早めにサスペンド、毎晩止める)**

```
MEMORY_MIB=2048
IDLE_MAX_SECONDS=900
NIGHTLY_STOP_TIME=22:00
BUDGET_MONTHLY_USD=100
BUDGET_EMAIL=you@example.com
```

`MEMORY_MIB` は `mvm image build`、ほかの AWS 側の設定は `mvm infra deploy` をやり直すと効きます。`MEMORY_MIB=2048` にすると、同じクォータで同時に動かせる台数が 2 倍になります。

**1 つのアカウントで 2 つの環境を分ける**

```
PMVA_PREFIX=pmva2
STACK_NAME=pmva2
IMAGE_NAME=pmva2-agent
```

別のフォルダに clone したリポジトリで使うか、`PMVA_CONFIG` と `PMVA_STATE_DIR` で設定ファイルと記録の置き場所を分けます ([commands.md](commands.md#環境変数))。
