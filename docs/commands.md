# コマンドリファレンス

`mvm` の全コマンドとオプションの一覧です。`mvm <command> --help` でも同じ内容を確認できます。

- 引数なしの `mvm`: 今の状態 (動いているセッション) と次の一手、よく使うコマンド 8 個 (`setup` / `up` / `ui` / `ls` / `send` / `result` / `ssh` / `stop`) を表示します。AWS に問い合わせるのは、手元の記録で終了していないセッションがあるときだけです。端末で打ったときは続けて、番号か名前の頭 (`se`、`/ui` など。8 個以外のコマンドも名前で選べる) でコマンドを選ぶと、要る引数 (名前、指示など) を聞いてから、同じ意味のコマンドを表示して実行します。Enter だけでやめます。パイプやエージェントから呼んだときは表示だけです
- `mvm -h`: 全部のコマンドを「よく使うコマンド」と「その他のコマンド」に分けて表示します
呼び出し方は Windows (PowerShell) が `.\bin\mvm.cmd`、macOS が `bin/mvm` です。インストールスクリプトでエイリアスを登録していれば、どちらも `mvm` で呼べます ([quickstart.md](quickstart.md#3-このリポジトリを取得してセットアップする))。

| 分類 | コマンド |
|---|---|
| 準備 | [`setup`](#setup) / [`doctor`](#doctor) / [`infra`](#infra) / [`secret`](#secret) / [`image`](#image) / [`profile`](#profile) / [`memory`](#memory) |
| 起動 | [`up`](#up) / [`fleet`](#fleet) |
| 確認・指示 | [`ls`](#ls) / [`peek`](#peek) / [`follow`](#follow) / [`view`](#view) / [`top`](#top) / [`send`](#send) / [`wait`](#wait) / [`result`](#result) / [`watch`](#watch) / [`ui`](#ui) / [`logs`](#logs) |
| 接続 | [`ssh`](#ssh) / [`forward`](#forward) / [`ssh-config`](#ssh-config) / [`shell`](#shell) / [`token`](#token) |
| 保存・停止 | [`save`](#save) / [`suspend` / `resume`](#suspend--resume) / [`stop`](#stop) / [`stop-all`](#stop-all) / [`renew`](#renew) |
| 司令塔 | [`orchestrator`](#orchestrator) |
| 費用 | [`cost`](#cost) |

セッション名 (`<name>`) は英小文字・数字・ハイフンで 31 文字以内です (例: `agent-01`)。名前ごとの状態は手元の `.state/sessions/<name>.json` に記録します。

---

## 準備

### setup

```bash
mvm setup [--profile <プロファイル名>]
```

導入の手順のうち済んでいないものを、次の順に 1 つずつ聞いて (`[Y/n]`、Enter で「はい」) 実行し、最後に `mvm doctor` で確かめます。済んだ手順は聞かずに飛ばすので、何度実行しても構いません (途中で止めたときや、設定を変えた後に)。

| 順番 | 済んでいないと判断するとき | 実行すること |
|---|---|---|
| 1 | スタック (`STACK_NAME`) が無い | `mvm infra deploy`。断ると中止する。スタックがあれば、`mvm doctor` と同じ確認の結果 (テンプレートとの違いなど) を表示するだけ |
| 2 | 今の認証情報がルートユーザーか管理者権限 (`AdministratorAccess`) | `mvm infra operator` (ユーザー `pmva-operator`、プロファイル `pmva`)。プロファイル `pmva` が既にあれば、作らずに `config.env` をそれに切り替える。切り替えた後、新しいキーが使えるまで最長 1 分待つ |
| 3 | 既定の認証方式・`USE_GITHUB_TOKEN`・`NOTIFY_WEBHOOK` に要るシークレットが無い | 値の用意のしかたを表示して `mvm secret set <kind>`。`github-token` を断ると `config.env` を `USE_GITHUB_TOKEN=false` にする |
| 4 | `SSH_PUBLIC_KEY_FILE` が無い | `ssh-keygen -t ed25519` (`ssh-keygen` があるときだけ) |
| 5 | イメージ (`IMAGE_NAME`) が無いか、使える版が無いか、`image/` や設定が最後のビルドから変わった | `mvm image build` |
| 6 | `claude` / `codex` のコマンドが手元にあるのに、利用者全体の skill (`mvm-orchestrator`) が入っていない | `mvm orchestrator install claude` / `codex` ([orchestrator](#orchestrator))。断っても「まだ残っている手順」には数えない。どちらも無ければ飛ばす |

- `--profile`: そのプロファイルを `config.env` の `AWS_PROFILE` に書いてから始めます。初回は管理者のプロファイルを渡します (1 と 2 に管理者の権限が要るため)。AWS CLI の設定にそのプロファイルが無ければ、何も書かずに止まります
- AWS の認証情報が無い・切れているときは、理由と `mvm setup --profile <管理者>` を案内して止まります
- 答えを読めない (標準入力が端末ではない) ときは、何もせずに止まります
- 断った手順や、スタックの確認で `!!` が出た項目があれば、最後に「まだ残っている手順」として一覧します。何も残っていないときだけ「準備ができました」と出します
- スタックの作成に失敗して出力が無い (`ROLLBACK_COMPLETE` など) ときと、AWS への問い合わせに失敗したときは、作り直しを勧めずに理由を出して止まります

### doctor

```bash
mvm doctor
```

前提ツール (Python、AWS CLI、ssh)、AWS の認証、リージョンで Lambda MicroVMs が使えるか、MicroVM メモリのクォータ (同時に何台まで起動できるか)、SSH 公開鍵を確認します。クォータで希望の台数に足りないときは、`MEMORY_MIB=2048` にした場合の台数と、引き上げの申請文 (リージョンと希望値入り) も表示します。
使っている認証情報がルートユーザー・管理者権限 (`AdministratorAccess`)・作成から 90 日を超えたアクセスキーなら `!!` で警告します。問題がなければ終了コード 0 です。

次のやり直し忘れや登録漏れも、起動する前に `!!` で知らせます。

| 確認すること | 知らせる場合 | 直し方 |
|---|---|---|
| AWS の構成 (CloudFormation のスタック) | スタックが無い (`NG`)、失敗した状態、手元のテンプレートと違う (mvm を更新した後など)、`config.env` の変更 (`ENABLE_BEDROCK` など) が反映されていない | `mvm infra deploy` |
| イメージ | `image/` や `IMAGE_*`・`MEMORY_MIB` などの設定が、この PC から最後に `mvm image build` したときから変わった | `mvm image build` (動いている VM には `mvm renew`) |
| 既定の認証方式 | `DEFAULT_CLAUDE_AUTH` / `DEFAULT_CODEX_AUTH`・`USE_GITHUB_TOKEN`・`NOTIFY_WEBHOOK` のシークレットが無い。`ENABLE_BEDROCK` / `ENABLE_ANTHROPIC_AWS` / `ANTHROPIC_AWS_WORKSPACE_ID` が足りない | `mvm secret set <kind>`、`config.env` |

- テンプレートとの比較には、操作用ポリシーの `cloudformation:GetTemplate` が要ります。古い構成では `--` (比べられない) と出るので、一度 `mvm infra deploy` をやり直してください
- イメージの比較は、この PC でビルドしたときの記録 (`.state/image-build.json`) と比べます。別の PC でビルドした場合は `--` と出ます

### infra

```bash
mvm infra deploy     # スタックを作成・更新する (管理者の権限で実行)
mvm infra outputs    # スタックの出力 (バケット名、ロールの ARN、OperatorPolicyArn など) を表示する
mvm infra operator [--user <IAM ユーザー名>] [--profile <プロファイル名>]   # 普段使う IAM ユーザーを作る (管理者の権限で実行)
mvm infra delete     # スタックと S3 バケットの中身を削除する (確認あり)
```

- `deploy`: CloudFormation のテンプレート (`INFRA_TEMPLATE=full` なら `infra/cloudformation.yaml`、`minimal` なら `infra/cloudformation-minimal.yaml`) を `config.env` の値で作成・更新します。予算アラート・キープアライブ・毎晩の自動停止などの設定は、変更後に `deploy` し直すと反映されます ([config.md](config.md))。前回の削除で残った Lambda 関数があると、先に知らせて止まります
- `operator`: `pmva-operator` ポリシー (`OperatorPolicyArn`) だけを付けた IAM ユーザー (既定 `pmva-operator`。既にあればそのまま使う) とアクセスキーを作り、AWS CLI のプロファイル (既定 `pmva`) として `~/.aws/credentials` と `~/.aws/config` (`AWS_SHARED_CREDENTIALS_FILE` / `AWS_CONFIG_FILE` があればそこ) に書き足して、`config.env` の `AWS_PROFILE` をそのプロファイルに書き換えます。秘密のアクセスキーは画面にもコマンドライン引数にも出しません。同じ名前のプロファイルが既にあるときと、今使っているプロファイルと同じ名前のときは、何も作らずに止まります。ユーザーのアクセスキーが上限 (2 個) のときは、古いキーの消し方を表示して止まります
- `delete`: 起動中のセッションがあると止まります (先に `mvm stop-all`)。終了処理中の VM を待ってからバケットを空にします。権限の拒否などで消せないリソースがあれば、そのリソースだけを残して残りを削除し、残したものと削除コマンドを表示します

### secret

```bash
mvm secret set <kind> [--name <名前>] [--file <ファイル>]
mvm secret ls
```

エージェントの認証情報を AWS Secrets Manager に登録します。値は入力しても画面に表示されず、コマンドライン引数にも載せません (シェルの履歴に残りません)。`--file` でファイルから、パイプで標準入力からも読めます。

トークンの種類 (JSON と URL 以外) は、値の途中の空白や改行を取り除いて登録します (端末の折り返しごとコピーしても使えるように)。

| kind | 中身 | 使う認証方式 |
|---|---|---|
| `claude-oauth-token` | `claude setup-token` で発行した長期トークン | `--claude-auth oauth-token` (Pro / Max) |
| `anthropic-api-key` | Anthropic の API キー | `--claude-auth api-key` |
| `anthropic-gateway` | `{"baseUrl":"...","authToken":"..."}` 形式の JSON | `--claude-auth gateway` |
| `claude-bedrock-api-key` | Amazon Bedrock の API キー (Claude Code 用) | `--claude-auth bedrock-api-key` |
| `anthropic-aws-api-key` | Claude Platform on AWS のワークスペースの API キー | `--claude-auth anthropic-aws-api-key` |
| `openai-api-key` | OpenAI の API キー | `--codex-auth api-key` |
| `codex-auth-json` | ChatGPT でログイン済みの `~/.codex/auth.json` の中身 | `--codex-auth chatgpt` |
| `codex-access-token` | Codex のアクセストークン (`at-...`) | `--codex-auth access-token` |
| `codex-gateway` | `{"baseUrl":"...","apiKey":"..."}` 形式の JSON ([auth.md](auth.md#ゲートウェイ-gateway)) | `--codex-auth gateway` |
| `claude-custom-auth` / `codex-custom-auth` | 環境変数・ファイル・設定をまとめた JSON ([auth.md](auth.md#どの方式にも当てはまらないとき-custom)) | `--claude-auth custom` / `--codex-auth custom` |
| `codex-bedrock-api-key` | Amazon Bedrock の API キー (Codex 用) | `--codex-auth bedrock-api-key` |
| `github-token` | GitHub の personal access token | clone / push (`USE_GITHUB_TOKEN=true`) |
| `notify-webhook` | Slack などの Webhook URL (`https://...`) | `--notify` |

名前の既定は `<PMVA_PREFIX>/<kind>` (例: `pmva/claude-oauth-token`) です。別名で登録するときも、MicroVM の実行ロールが読める接頭辞 (Claude 用は `anthropic-` / `claude-`、Codex 用は `openai-` / `codex-`、ほかに `github-` / `notify-`) で始める必要があります。詳しくは [auth.md](auth.md)。

### image

```bash
mvm image build                 # イメージをビルドする (初回 10〜20 分)
mvm image status                # イメージとバージョンの状態を表示する
mvm image base                  # 使えるベースイメージの一覧
mvm image delete [--yes]        # イメージを全バージョンごと削除する (確認あり)
mvm image prune [--keep N] [--yes]   # 古いバージョンだけを削除する (既定は 2 版残す。確認あり)
mvm image versions              # 固定している Claude Code / Codex の版と npm の最新版を比べる
mvm image update-versions [--claude <版>] [--codex <版>]   # image/Dockerfile の版を書き換える
```

- `build`: `image/` を zip にして S3 に置き、イメージ (2 回目からは新しいバージョン) を作ります。完了まで待ち、失敗したら原因を表示します
- `prune`: `build` のたびに増えるバージョンは自動では消えず、1 版ごとに保存料金がかかります ([cost.md](cost.md#イメージ保存))。最新の ACTIVE 版を含め、新しい順にビルドに成功した `--keep` 版 (既定 2) を残し、それより古い版とビルドに失敗した版を削除します。ビルド中・削除中の版と、起動中 (終了済み以外) の VM が使っている版は消しません (使っている VM は `mvm renew` で新しい版に移すか `mvm stop` で止めてから、もう一度実行します)。削除は非同期で進むので、状態は `status` で確かめます
- `update-versions`: 版を省略すると npm の最新版にします。書き換えた後は `build` し、動作を確認してから Dockerfile の変更を commit してください

### profile

```bash
mvm profile show [--agent claude|codex|both]                      # 送るものを表示する (送らない)
mvm profile push [--name <名前>] [--agent claude|codex|both] [--dry-run]
```

手元の Claude Code / Codex の自分用の設定を S3 に送り、VM の起動時にエージェントのホームに置きます。VM のエージェントも、手元と同じルールや skill で動くようになります。

| エージェント | 送るもの (あれば) |
|---|---|
| Claude Code | `~/.claude/` の `CLAUDE.md`、`skills/`、`agents/`、`commands/`、`output-styles/` |
| Codex | `~/.codex/` (`CODEX_HOME`) の `AGENTS.md`、`skills/`、`prompts/` |

- **送らないもの**: `settings.json` / `config.toml` (認証情報、MCP、手元のパス、フックを含みやすいため)、認証情報のファイル、`mvm-orchestrator` の skill、Codex が自分で入れる skill (`.system`)、`.git` / `node_modules` など
- **送る前に確かめること**: API キーやトークン、秘密鍵らしい文字列を含むファイル、1MB を超えるファイル、シンボリックリンクは送りません (理由を表示します)。手元のパス (`C:\Users\...`、`/Users/...`) を含むファイルは送りますが、VM では見つからないので注意を表示します
- Windows で書いたテキストの改行は LF にし、スクリプト (`.sh` / `.py` / `#!` で始まるもの) には実行権限を付けます
- 送った設定を使うには、`config.env` に `PROFILE=<名前>` を書くか、`mvm up --profile <名前>` で起動します。起動中の VM には反映されません (次の起動・`renew` から)
- VM の中では、skill・サブエージェント・コマンドを 1 つずつ丸ごと置き換えます。VM の中で作った別の名前のものは残します
- 名前は英小文字・数字・ハイフン (既定 `default`)。チーム用と個人用など、いくつかのまとまりを使い分けられます

### memory

```bash
mvm memory status|pull|push|sync [--dir <リポジトリのフォルダ>] [--dry-run] [--force]
mvm memory log [<名前>] [--origin <マシン名>] [--since <30m|2h|3d|日時>] [--json] [--dir <リポジトリのフォルダ>]
mvm memory revert <名前> [--to <時刻>] [--dry-run] [--force] [--dir <リポジトリのフォルダ>]
mvm memory rm <名前> [--dry-run] [--dir <リポジトリのフォルダ>]
mvm memory install-hooks|uninstall-hooks [--dir <リポジトリのフォルダ>]
```

Claude Code の自動メモリ (auto memory。Claude が `~/.claude/projects/<リポジトリ>/memory/` に書く、1 件 1 ファイルの `*.md` と索引の `MEMORY.md`) を、S3 経由で手元の PC と VM で共有します。`--dir` の既定は現在のフォルダです。

| コマンド | 動き |
|---|---|
| `status` | 何も書き換えずに、ファイルごとの状態 (同期済み / 手元だけ / S3 だけ / 手元が新しい / S3 が新しい / 競合 / 送らない / `rm` で消された) を表示する |
| `pull` | S3 から手元に取得する |
| `push` | 手元から S3 に送る |
| `sync` | `pull` してから `push` する |
| `log [<名前>]` | いつ・どのマシンが・どのメモリを書いた (消した) かを、古い順に表示する (下の「履歴・戻す・消す」)。何も書き換えない |
| `revert <名前>` | 誤ったメモリを、履歴の 1 つ前の版 (`--to` で指定した時刻の版) に戻す |
| `rm <名前>` | 誤ったメモリを S3 と手元から消し、他のマシンにも消えたことを伝える |
| `install-hooks` | このリポジトリの Claude Code に、自動で同期するフックを入れる (下の「自動の同期」) |
| `uninstall-hooks` | `install-hooks` で入れたフックを外す (ほかのフックや設定は残す) |

- **置き場所**: `s3://<バケット>/memory/<repo-id>/`。`<repo-id>` は `git remote get-url origin` から作ります (`https://github.com/Owner/Repo.git`、`git@github.com:Owner/Repo.git`、`ssh://git@github.com/Owner/Repo` はどれも `github.com/owner/repo`。URL に入っているトークンなどは使いません)。同じリポジトリなら、別の PC や VM も同じ場所を使います
- **手元のメモリのフォルダ**: リポジトリのルート (worktree でもメインのルート) の `.claude/settings.local.json`、次に `~/.claude/settings.json` の `autoMemoryDirectory` を探し、無ければ Claude Code の既定の `~/.claude/projects/<パスの英数字以外を - にしたもの>/memory` です。パスが長くて 200 文字を超えるときは、`.claude/settings.local.json` に `autoMemoryDirectory` を書いてください
- **VM との共有**: `config.env` の `MEMORY_SYNC=true` (または `mvm up --memory`) で、`--repo` 付きで起動した VM が起動時に取得し、定期保存 (`snapshot.sh`) のたびに送ります ([architecture.md](architecture.md#自動メモリの共有-mvm-memory))。VM の Claude Code には、次の「自動の同期」と同じフックが入ります
- **自動の同期 (フック)**: `install-hooks` は、リポジトリのフォルダの `.claude/settings.local.json` に Claude Code のフックを入れます。VM では `mvm up --memory` で同じフックが入ります
  - 会話の開始時 (SessionStart) と指示のたび (UserPromptSubmit。60 秒に 1 回まで) に `pull` し、他のマシンで書かれた・変わったメモリを本文ごと会話に渡します (合わせて約 9,500 文字まで。入りきらないファイルは名前だけ伝えます)。動いている会話にも、次の指示のときに届きます
  - Claude がメモリのフォルダに書いたら (Write / Edit の後。PostToolUse) すぐ `push` します。会話は待たせません (`async`)
  - 競合したファイルは自動では解かず、会話の開始時に名前を伝えます。`mvm memory status` で確かめて `--force` で解いてください
  - 同じリポジトリの同期は、フックと手動の `pull` / `push` / `sync` / `rm` / `revert` を合わせて 1 つずつ動きます。手動の同期は、フックの同期が終わるのを最長 60 秒待ち (「待っています」と表示)、それでも終わらなければ何もせずにエラーで止まるので、もう一度実行してください。フックは待ちきれなければその回を飛ばします
  - フックの失敗で会話は止めません。失敗は `.state/memory/<repo-id>.json.log` (VM では `~/.claude/pmva-memory.state.json.log`) に残ります
  - フックは入れたフォルダの Claude Code にだけ効きます。git worktree で作業するときは、その worktree のフォルダでも `install-hooks` を実行してください (メモリのフォルダは worktree でも同じです)。動いている Claude Code には、次に起動した会話から効きます
  - `settings.local.json` にはこの PC の Python と `mvm.py` のパスが入ります。git の管理から外れていなければ、そう表示するので `.gitignore` に足してください
- **送るもの**: フォルダ直下の `*.md` と `MEMORY.md` だけ。改行は LF にそろえます
- **送らないもの** (理由を表示します): frontmatter に `share: false` と書いたファイル (手元専用)、API キー・トークン・秘密鍵らしい文字列を含むファイル、1MB を超えるファイル。`share: false` のファイルを指す索引の行も送りません
- **3 者比較**: 最後に同期したときの内容 (`.state/memory/<repo-id>.json`) を基準に、手元と S3 を比べます。片方だけが変わっていれば、変わった側を相手に写します (`pull` は S3 の変更、`push` は手元の変更だけを写し、`push` は S3 のほうが新しいファイルを上書きせずに「先に pull」と表示します)
- **同時の書き込み**: S3 には条件付きで書き込みます (`--if-match` / `--if-none-match`)。比べた後、送るまでの間に他のマシンが同じファイルを書き換えていたら、上書きせずに「競合」として飛ばします。`MEMORY.md` は取り直してマージし直します (3 回まで)。条件付き書き込みを知らない古い AWS CLI では、警告を出して条件なしで送ります
- **競合の解き方**: 手元と S3 の両方が変わったファイルは、既定では飛ばして「競合」と表示します。どちらかを採用するときは `--force` (`pull` は S3 の内容、`push` は手元の内容で上書き) を付けます。上書きしたくない内容は、先に別の名前で退避してください
- **削除は、`rm` したものだけが伝わります**: ファイルを手で消しても S3 には残り、`pull` しても取得しません (「手元で消したファイル」と表示します)。ほかのマシンからも消したいときは `mvm memory rm <名前>` を使います (次の「履歴・戻す・消す」)
- **履歴・戻す・消す** (誤ったメモリを見つけて直すための機能):
  - **履歴**: `push` (フックの自動送信と、VM の定期保存を含む) で S3 に送るたびに、その中身の写しを `s3://<バケット>/memory/<repo-id>/.history/<名前>/<UTC の時刻>-<送ったマシン>-<md5 の先頭 8 文字>.md` に置きます (例 `.history/user_role.md/20261124T101503Z-agent-01-1a2b3c4d.md`)。送ったマシンは手元なら `local`、VM ならその VM の名前です。`MEMORY.md` の履歴は残しません。履歴を書けなくても同期は止めず、警告だけを出します。`--dry-run` では書きません。履歴は自動では消えません (必要なら `aws s3 rm --recursive` で消します)。この機能より前に送ったメモリには履歴がありません (次に変更を送ったときから残ります)
  - **`log [<名前>]`**: 履歴と削除の印を、古い順の表 (時刻は UTC `2026-11-24 10:15:03Z`、書いたマシン、書いた / 消した、名前) で表示します。`--origin <マシン名>` でそのマシンが書いたものだけ、`--since` で期間を絞れます (`30m` / `2h` / `3d` のように今からさかのぼる指定か、`2026-11-24T10:00:00Z` / `2026-11-24` の日時)。`--json` は `at`、`origin`、`action` (`write` / `delete`)、`name`、`md5`、`key` を持つオブジェクトの配列です。VM の作業の後に「VM が書いたメモリ」を洗い出すには `mvm memory log --since <開始時刻> --json` を使います
  - **`revert <名前>`**: 履歴から S3 と手元のファイルを戻します。既定は「今の内容 (S3、無ければ手元) と違う版のうち最新」= 1 つ前です。戻したことも履歴に積まれるので、続けて `revert` すると、さらに前ではなく戻す前の版に戻ります (取り消しになります)。2 つ以上前に戻すときは、`mvm memory log <名前>` で時刻を確かめて `--to` を付けてください。`--to 20261124T101503Z` (`log` の時刻の形。`2026-11-24T10:15:03Z` でも可) を付けると、その時刻以前で最新の版にします。手元に S3 とまだ同期していない変更があるときは止まります (`--force` で捨てて戻す)。戻したことも履歴に新しい版として残ります。戻す版が削除の印に載っていれば印を外し、索引 (`MEMORY.md`) からその行が消えていれば、消したときの行を手元と S3 に戻します。`rm` したメモリも戻せます。`--dry-run` は、戻す版と中身の先頭 5 行だけを表示します
  - **`rm <名前>`**: 名前は `user_role.md` のような `.md` のファイル名です (`MEMORY.md` は消せません)。手元・S3・最後に同期したときの内容 (基準) の中身の md5 を、S3 の `.deleted.json` (名前ごとに新しい順に 20 件まで。消した時刻、消したマシン、索引の行) に「削除の印」として書いてから、S3 のファイル、S3 の `MEMORY.md` の行、手元のファイルと手元の `MEMORY.md` の行を消します (途中で止まっても、もう一度実行すれば続きから消せます)。どこにも無い名前は「そのメモリはありません」で終了コード 1 です。`--dry-run` は、することだけを表示します。S3 のファイルを消すので、**手元の PC (操作用の認証情報) で実行してください**。VM の実行ロールには `s3:DeleteObject` がなく、AccessDenied になります
  - **消えたことの伝わり方**: ほかのマシンは `pull` (フックを含む) で `.deleted.json` を見て、手元の中身が印の中身と同じファイルを手元から消し、索引の行も消します ("他のマシンで消されたメモリ" と表示し、終わりの集計に「削除 N」を足します)。フックでは、会話にも「他のマシンで消されたメモリ: <名前> (手元からも消しました。この記憶には頼らないでください)」と伝えます。消された後に手元で書き換えたファイルは消しません。印と同じ中身は `push` も `pull` もしません (`status` には「消された (mvm memory rm)」と出ます)。消されたメモリを作り直した (中身が違う) ものは、普通のメモリとして同期します
  - **古い VM**: イメージ 8.0 以前の VM は削除の印を読まないので、`rm` したメモリを送り直すことがあります (送り直された中身は、新しい版のマシンの `pull` / `push` が印で見分けて取り込みません)。VM の中の取得・送信も新しくするには、`mvm image build` でイメージを作り直してください
- **`MEMORY.md`**: ファイルの内容ではなく、行の和集合でマージします。行はリンクの飛び先 (`](xxx.md)`) で見分けます。`pull` は S3 の索引の行のうち、手元の索引に無く、ファイルが手元にあるものを足し、`push` は手元の行のうち、S3 の索引に無く、ファイルが S3 にあるものを足します。行は消えません。200 行か 25KB を超えると警告します (Claude Code は先頭 200 行 / 25KB しか読みません)
- `--dry-run` は何も書き換えず (状態も保存せず)、することだけを表示します (`status` / `log` では無視)。終わりに「取得 N、送信 N、競合 N、飛ばした N」(他のマシンで消されたものがあれば「、削除 N」) を表示します。`--force` は `pull` / `push` では競合の解決、`revert` では手元の未同期の変更を捨てることを指します
- `--origin` は `log` だけで使います (`mvm memory` の送り元はいつも `local` です)
- そのリポジトリで初めて `pull` / `push` / `sync` したときだけ、VM と共有する設定 (`MEMORY_SYNC`) とフック (`install-hooks`) を案内します
- `mvm infra deploy` で更新した実行ロール (`memory/*` の読み書き) と操作用ポリシー (`memory/*` の削除) が要ります。`mvm memory` の同期そのものは、操作用ポリシーの既存の権限で足ります。`rm` は S3 のファイルを消すので操作用ポリシーの削除の権限を使い、`log` / `revert` は履歴 (`.history/`) の一覧と読み取りを使います

---

## 起動

### up

```bash
mvm up <name> [オプション]
```

MicroVM を 1 台起動し、エージェントを tmux の中で開始します。起動は 10 秒ほどです。

| オプション | 既定 | 説明 |
|---|---|---|
| `--agent claude\|codex\|both\|none` | `DEFAULT_AGENT` | 起動するエージェント。`none` はエージェントなし (シェルだけ) |
| `--claude-auth api-key\|oauth-token\|bedrock\|bedrock-api-key\|mantle\|anthropic-aws\|anthropic-aws-api-key\|gateway\|custom\|interactive\|none` | `DEFAULT_CLAUDE_AUTH` | Claude Code の認証方式 ([auth.md](auth.md)) |
| `--codex-auth api-key\|chatgpt\|access-token\|bedrock\|bedrock-api-key\|gateway\|custom\|interactive\|none` | `DEFAULT_CODEX_AUTH` | Codex の認証方式 |
| `--claude-secret` / `--codex-secret` / `--github-secret <名前>` | 認証方式から決まる | 既定と別名で登録したシークレットを使う |
| `--no-github` | | GitHub トークンを使わない |
| `--repo <URL>` | 下の「`--repo` を省いたとき」 | clone するリポジトリ |
| `--no-repo` | | リポジトリを clone しない |
| `--fresh` | | 前回の同じ名前のセッションを引き継がず、保存したワークスペースも復元しない (`up` だけ) |
| `--branch <ブランチ>` | `pmva/<name>` | 作業ブランチ。リモートにあれば追跡し、なければ作る |
| `--base-branch <ブランチ>` | リポジトリの既定ブランチ | 作業ブランチを新しく作るときの起点 |
| `--prompt "<指示>"` | | 起動時にエージェントへ渡す最初の指示 |
| `--restore latest\|s3://...` | | 保存したワークスペースから復元する |
| `--claude-args` / `--codex-args "<引数>"` | `DEFAULT_CLAUDE_ARGS` など | エージェントの追加引数 (例: `--claude-args "--dangerously-skip-permissions"`) |
| `--model <ID>` | `CLAUDE_MODEL` | Claude のモデル / Bedrock の推論プロファイル ID |
| `--bedrock-region <リージョン>` | `BEDROCK_REGION` | Bedrock (Mantle を含む) を呼ぶリージョン。Claude Code と Codex の両方に効く |
| `--idle <秒>\|off` | `IDLE_MAX_SECONDS` | 受信通信がこの秒数無いとサスペンド |
| `--max-duration <秒>` | `MAX_DURATION_SECONDS` | 寿命 (上限 28800 = 8 時間) |
| `--notify` / `--no-notify` | `NOTIFY_WEBHOOK` | 完了・入力待ちを Webhook に通知する |
| `--no-setup` | | リポジトリの `.pmva/setup.sh` を実行しない ([usage.md](usage.md#リポジトリごとの準備-pmvasetupsh)) |
| `--profile <名前>` / `--no-profile` | `PROFILE` | 手元から送った設定のまとまり ([profile](#profile)) を使う / 使わない |
| `--memory` / `--no-memory` | `MEMORY_SYNC` | Claude Code の自動メモリを手元と共有する ([memory](#memory))。`--repo` が無いときは共有しない |
| `--remote-control` | | Claude Code を Remote Control (claude.ai/code、スマホの Claude アプリ) から操作できるようにする ([usage.md](usage.md#remote-control-スマホや-claudeaicode-から操作する))。`--claude-auth interactive` で起動し、`mvm login <name>` でログインすると自動で起動する。`--agent claude` か `both` だけで、他の `--claude-auth` とは一緒に使えない |

`--repo` を省いたとき:

1. **同じ名前で前に起動したセッションがあれば、その続きから始めます。** 前回の `--repo`、`--branch`、`--base-branch` を引き継ぎ、そのセッション名で保存したワークスペース (`mvm stop` や毎晩の自動停止で保存したもの) があれば `--restore latest` と同じく復元します。保存が無ければ clone から始めます。引き継ぐのはこの 3 つと復元だけで、`--agent` や `--remote-control` などは毎回指定します。新しく始めるなら `--fresh` を付けます
2. **前のセッションが無いとき (または `--fresh` のとき) は、今いるフォルダの git の `origin` を clone します。** `git@github.com:...` や `ssh://` の URL は `https://` に直し、URL に入っているトークンは取り除きます。VM から clone できない `origin` (手元のフォルダなど) と、`mvm` 自身のフォルダで打ったときは、リポジトリなしで起動します

`--repo` を明示したときは、これまでどおり前回を引き継がずに新しく始めます (続きから始めるなら `--restore latest` を付けます)。前回のセッションの記録は、この PC の `.state/sessions/` にあります。別の PC で続きから始めるときは `--repo <URL> --restore latest` を指定してください。

注意:

- 起動時の設定 (指示文を含む) は 4KB までです。長い指示は起動後に `mvm send` で送ってください
- MicroVM メモリのクォータを超えるとき (と、`MAX_AGENTS` を設定していてその台数に達しているとき) は、起動せずに止まります。止まるときは、あと何台起動できるかと、`MEMORY_MIB=2048` にした場合の台数、引き上げの申請文を表示します
- 起動するエージェントの組み合わせに応じて、その認証情報だけを読める実行ロールが自動で選ばれます

### fleet

```bash
mvm fleet <台数> [--prefix <接頭辞>] [--prompts-file <ファイル>] [up と同じオプション]
```

`<接頭辞>-01` 〜 `<接頭辞>-NN` (既定の接頭辞は `agent`) をまとめて起動します。起動済みの名前は飛ばします。1 台目を起動する前に、全台が MicroVM メモリのクォータに収まるかを確かめ、収まらなければ 1 台も起動せず、`mvm up` と同じ案内 (あと何台か、`MEMORY_MIB` を下げた場合の台数、引き上げの申請文) を表示します。
`--prompts-file` には 1 行に 1 つずつ指示を書きます。n 行目が n 台目の最初の指示になります (空行の台は `--prompt` の指示か、指示なし)。
`--repo` を省くと今いるフォルダの `origin` を clone します。`up` と違い、前回の同じ名前のセッションは引き継ぎません (毎回新しいタスクとして始めます)。

---

## 確認・指示

### ls

```bash
mvm ls [--all] [--json]
```

セッションの一覧 (名前、状態、エージェント、認証方式、起動時刻、寿命の残り `REMAINING`、MicroVM の ID) です。残りは `起動時刻 + MAX_DURATION_SECONDS - 今` で、`2h10m` の形で出します (起動時刻が分からないときは `-`)。1 時間を切った台は `!! 45m` と出し、表の下に「`mvm renew <名前>` で延長できます」と案内します。サスペンド中も数えます。`--all` で終了済みも表示します。状態は `RUNNING` / `SUSPENDED` / `TERMINATED` などで、AWS から状態を取れなかったときは `UNKNOWN` と表示し (終了扱いにはしません)、一覧の後に理由とやるべきこと (`aws sso login` でログインし直す、`AWS_PROFILE` を確かめる、`mvm doctor` で確かめる、など) を標準エラーに出します。`watch` / `wait` / `top` も同じ理由を表示します。
`--json` で、リポジトリと作業ブランチ、寿命の残り秒数 (`remainingSeconds`。分からないときは `null`) も含めて JSON の配列で出力します (スクリプトや、手元のエージェントから読むとき)。`UNKNOWN` の行には、理由とやるべきことを `stateError` に入れます。

### peek

```bash
mvm peek <name> [行数] [--json]
```

エージェントの画面の末尾 (既定 30 行) と、エージェントの状態 (`claude=working` など、[wait](#wait) の表を参照)、VM のメモリの使用量 (`mem=1.2/3.8GiB`)、初期化の状態 (`bootstrap=ready` / `failed: ...`)、最後に保存した時刻を表示します。メモリ不足でプロセスが強制終了されていれば `!!` で知らせます。接続はしないので、手元の負荷はほぼありません。
`--json` で、VM 内の状態 API の応答をそのまま JSON で出力します。

### follow

```bash
mvm follow [<name>...] [--agent claude|codex] [--lines <件数>] [--chars <文字数>] [--full] [--once] [--json]
```

エージェントの推論の過程 (指示・思考・ツールの実行と結果・応答) を、会話の記録から流れるように表示します。名前を省略すると、動いている全セッションをまとめて表示します。Ctrl-C で終了します。

```text
[a1 claude] 指示: テストを直して
[a1 claude] 思考: まず失敗しているテストを見る
[a1 claude] 実行: Bash: pytest -x
[a1 claude] 結果: FAILED tests/test_api.py::test_login
    … (残り 12 行)
[a1 claude] 応答: 原因はトークンの期限切れでした。直してテストが通りました。
[a2 codex]  実行: exec_command: npm test
```

| オプション | 既定 | 説明 |
|---|---|---|
| `--agent claude\|codex` | 両方 | `--agent both` のセッションで片方だけ表示する |
| `--lines <件数>` | `20` | 最初に表示する、直前の出来事の数 |
| `--chars <文字数>` | `500` | 1 件あたりの最大の文字数。ツールの結果は、さらに先頭の 5 行だけ表示する |
| `--full` | | 省略せずに全文を表示する |
| `--interval <秒>` | `2` | 新しい出来事を確かめる間隔 |
| `--once` | | 直前の出来事を表示して終わる |
| `--json` | | 1 行に 1 件の JSON で出力する (`session`、`agent`、`kind`、`text` など) |

- 読むのは、Claude Code の `~/.claude/projects/*/*.jsonl` と Codex の `~/.codex/sessions/**/rollout-*.jsonl` です。エージェントを起動し直して新しい会話になると、`--- 新しい会話 ---` と表示して続けます
- 思考は、記録されている分だけ表示します。Claude Code は思考の本文を記録しないことがあり、Codex は思考の要約を記録します
- 表示している間は定期的に通信するので、そのセッションはサスペンドされません。VM が終了すると、そのセッションの表示をやめます

### view

```bash
mvm view <name> [--window claude|codex|shell] [--no-reconnect]
```

エージェントの画面を、そのまま読み取り専用で表示します (SSH で tmux に接続します)。キーを押してもエージェントには届かないので、うっかり入力して作業を邪魔することがありません。抜けるのは Ctrl-b → d です。
表示するウィンドウの既定は、Codex だけのセッションなら `codex`、それ以外は `claude` です。ほかの接続 (`mvm ssh`) で表示しているウィンドウは変わりません。操作もしたいときは [`ssh`](#ssh) を使います。

### top

```bash
mvm top [<name>...] [--json]
```

VM のメモリの使用量と、多く使っているプロセスをプロセス名ごとにまとめて表示します。名前を省略すると、動いている全セッションです。

```text
[a1] 使用 3.0 GiB / 4.0 GiB (空き 1.0 GiB)
  chrome                  9 個     1800 MiB
  node                    3 個      700 MiB
```

- プロセスごとの使用量 (RSS) を足しているので、共有しているメモリを重ねて数えます (特に Chrome)。大まかな目安として見てください
- メモリ不足でプロセスが強制終了されると、その回数を `!!` で表示します。通知 (`--notify`) が有効なら、Webhook にも知らせます。`MEMORY_MIB` を増やすか、同時に動かすテストやブラウザを減らしてください

### send

```bash
mvm send <name> "<指示>" [--window claude|codex|shell] [--no-enter]
```

指示を tmux に入力して Enter を押します。送り先の既定は、Codex だけのセッションなら `codex`、それ以外は `claude` です。
`-` で始まる文字列を送るときは `mvm send <name> -- -h` のように `--` の後に書きます。`--no-enter` で Enter を押さずに入力だけします。
`claude` / `codex` ウィンドウに Enter 付きで送ると、そのエージェントの状態を「作業中」にします ([wait](#wait) が送った直後に返ってしまわないように)。

### wait

```bash
mvm wait [<name>...] [--timeout <秒>] [--any] [--idle <秒>] [--interval <秒>] [--json]
```

エージェントの応答が終わるか、許可・入力を待つ状態になるまで待ちます。名前を省略すると、動いている全セッションを待ちます。終わった台から 1 行ずつ表示します。

| オプション | 既定 | 説明 |
|---|---|---|
| `--timeout <秒>` | `0` (無制限) | 最大の待ち時間。過ぎたら、まだ終わっていない台を表示して終了コード 2 で返る |
| `--any` | | どれか 1 台が終わったら返る (既定は全台が終わるまで待つ) |
| `--idle <秒>` | `300` | 作業中のはずなのに、画面も CPU もこの秒数動かなければ、止まっている (`stalled`) とみなして返る。`0` で無効 |
| `--interval <秒>` | `15` | 状態を確かめる間隔 |
| `--json` | | 全台の結果を JSON でまとめて出力する |

終了コードは、待ち終わったら 0、時間切れなら 2 です。

エージェントの状態は、VM の中でエージェントのフック (Claude Code の hooks、Codex の `notify`) が記録します。

| 状態 | 意味 | wait |
|---|---|---|
| `starting` | 起動の準備中 | 待つ |
| `working` | 作業中 (指示を受け取ってから、応答が終わるまで) | 待つ |
| `done` | 応答が終わった | 返る |
| `waiting` | 許可・入力を待っている (Claude Code のみ。何を待っているかも表示する) | 返る |
| `ready` | 最初の指示を待っている (`--prompt` なしで起動した) | 返る |
| `error` | 認証の失敗や利用上限など、API のエラーで止まった (Claude Code のみ。エラーの種類と文章、次にやることも表示する) | 返る |
| `exited` | エージェントが終了して、シェルに戻った | 返る |
| `failed` | 起動の準備 (bootstrap) に失敗した | 返る |

ほかに、セッション全体として `stalled` (上の `--idle`)、`suspended` (VM がサスペンド中。VM を起こさずに返す)、`terminated` (VM が終了した) でも返ります。`--agent both` のセッションは、両方のエージェントが終わるまで待ちます。
AWS や VM から状態を取れないときは `unreachable` として待ち続けます。AWS の認証切れなどが理由のときは、`--json` の `error` と時間切れのときの表示に、理由とやるべきことを出します。

- Codex は、許可・入力待ちを知らせる仕組みがないため、`waiting` になりません。承認待ちで止まったときは `--idle` の秒数が過ぎてから `stalled` で返ります。無人で動かすなら、承認を求めない設定 (`--codex-args "--dangerously-bypass-approvals-and-sandbox"` など) にしておきます
- 待っている間は状態を確かめる通信をするので、そのセッションはサスペンドされません (終わった台は確かめるのをやめます)
- `suspended` は、ふつうは作業が終わってアイドルで止まった状態です (作業中の VM はキープアライブが起こし続けます)。ただし手動で `mvm suspend` したときや、キープアライブの無い最小構成 (`INFRA_TEMPLATE=minimal`) では、作業の途中でもサスペンドします。そのため `suspended` で返ったときは、確かめ方 (`mvm peek <name>` で再開して状態を見て、作業中なら `mvm wait <name>` で待ち直す) も表示し、`--json` では `hint` に入れます。VM を起こさないのは、起こすとアイドルで止まるまで (`IDLE_MAX_SECONDS`) 課金されるためです
- 手元のエージェントから呼ぶときは、ツールの実行時間の上限 (10 分ほど) より短い `--timeout` を付け、終了コード 2 なら呼び直します
- このコマンドより前に作ったイメージは、エージェントの状態を返しません。その場合は画面と CPU が `--idle` の秒数止まったときだけ返ります。`mvm image build` で作り直してください

### result

```bash
mvm result <name> [--agent claude|codex] [--json]
```

エージェントの最後の応答の文章を表示します。画面ではなく応答そのものなので、長い報告も途中で切れません。`--agent both` のセッションは両方を表示します (片方だけなら `--agent`)。
まだ応答がないエージェントや、エラーで止まった (`error`) エージェントがあれば、終了コード 1 です。エラーのときは「(エラーで止まりました: <種類>) <エラーの文章>」と、次にやることを表示します (`--json` では `state` が `error`、`error` に種類)。

- Claude Code: 応答の終わり (`Stop` フック) で渡される最後の応答。渡されない版では、会話の記録 (`~/.claude/projects/` の JSONL) から、最後のツール実行より後の文章を取り出します
- Codex: 応答の終わり (`notify` の `agent-turn-complete`) で渡される最後の応答

### watch

```bash
mvm watch [秒] [--once] [--json] [--log <CSV のパス>]
```

終了していない全セッションを、1 台 1 行の一覧ボードで表示し直します (Ctrl-C で終了、終了コード 0)。台数が増えても 1 画面で見渡せ、要対応の台は表の下に `!!` の行で理由とやることを出します。

| オプション | 内容 |
|---|---|
| `秒` | 更新の間隔。既定は 10 |
| `--once` | 1 回だけ表示して終わる (描き直しの制御文字を出さない) |
| `--json` | 台ごとの状態を JSON の配列で 1 回出力する (`--once` を含む)。キーは `name`、`vm`、`state`、`agents`、`sinceSeconds`、`remainingSeconds` (寿命の残り秒数。分からないときは `null`)、`memory` (`usedMiB`、`totalMiB`、`peakMiB`)、`oomKills`、`lastEvent`、`notes` |
| `--log <パス>` | 更新のたびに台ごとに 1 行を CSV に追記する。列は `time,name,vm,state,usedMiB,totalMiB,peakMiB,oomKills,remainingSeconds`。ファイルが無ければ見出しの行を書く。前の版で書いた、列の違うファイルには追記しない。書けないときは理由と次にやることを出して止まる |

表示の例:

```
2026-11-24 15:00:12  4 台 (作業中 1 / 要対応 1 / 完了 1)  10 秒ごと / Ctrl-C で終了
名前  状態            経過  残り     メモリ (使用/全体 最大)  最後の出来事
a1    作業中          12分  2h10m    1.2/3.8G 最大1.5G        実行: Bash: pytest -x
a2    許可・入力待ち  3分   5h02m    0.9/3.8G 最大0.9G        Bash の実行の許可を求めています
a3    応答が終わった  1分   !! 45m   0.5/3.8G 最大0.8G        応答: 原因はトークンの期限切れでした
a4    SUSPENDED       -     7h30m    -                        -

!! a2 (claude): 許可・入力待ち: Bash の実行の許可を求めています
(表示中のセッションは自動サスペンドされません)
```

- 状態: エージェントの状態を [wait](#wait) と同じ考え方で 1 つにまとめて出します (作業中・応答が終わった・許可・入力待ち・エラーで止まった・動きが止まった、など)。VM が `RUNNING` 以外のときは、VM の状態 (`SUSPENDED` など) をそのまま出します。AWS から状態を取れないとき (`UNKNOWN`) は、表の下に理由とやることを出します
- 経過: 今の状態になってからの時間です (分からないときは `-`)
- 残り: MicroVM の寿命 (`MAX_DURATION_SECONDS`) の残りです。1 時間を切ると `!!` を付けます。延長は [renew](#renew)。`mvm ui` の一覧にも同じ列が出ます
- メモリ: 使用量/全体の量 (GiB) と、`watch` を動かしている間に見た使用量の最大 (`最大`。手元で台ごとに覚え、`watch` を終えると忘れます)。メモリ不足でプロセスが強制終了された (OOM) 台は `!!OOM×回数` を付け、表の下にも出します
- 最後の出来事: 会話の記録 ([follow](#follow) と同じもの) の最後の 1 件です。`--agent both` のセッションは、状態が最近変わった方を出します (分からなければ claude)
- 要対応: 許可・入力待ち、エラー、起動の準備の失敗、動きの停止、`UNKNOWN`、VM の状態 API の応答なし、OOM の台です。表の下に `!!` で理由を出します
- 画面の中身は出しません。画面は [peek](#peek) / [view](#view) で見てください
- 表は端末の幅で切ります (日本語は全角 2 桁として桁をそろえます)。標準出力が端末で、環境変数 `NO_COLOR` が無いときだけ色を付けます (要対応は黄、エラー・OOM は赤、完了は緑)。端末でなければ (リダイレクトやパイプ) 描き直さず、区切り線を挟んで追記します

通信の頻度:

- `get-microvm` (AWS CLI を呼びます) は、1 台につき 60 秒に 1 回までです。その間の回は、直前の状態が `RUNNING` なら VM の `/status` (画面は取りません) と `/events` だけを呼びます。`/status` が失敗した台は、次の回に `get-microvm` で確かめ直します
- `SUSPENDED` の VM には `/status` も `/events` も呼びません (起こすとアイドルで止まるまで課金されるため)。そのため、手動で再開した VM が一覧に反映されるまで最大 60 秒かかります
- 表示している間は、`RUNNING` の台に定期的に通信するので、その台は自動でサスペンドされません。見終わったら Ctrl-C で終了してください

`--log` は、MicroVM のメモリ (`MEMORY_MIB`) が足りるかを測るのに使えます。台数を増やした状態で `mvm watch --log mem.csv` を動かしたままにすると、台ごとの使用量と最大 (`peakMiB`)、OOM の回数が時系列で残ります。たとえば 2GB の VM で足りるかは、実際の作業を一通り流した後の `peakMiB` の最大値と `oomKills` で判断できます。

### ui

```bash
mvm ui [--port <番号>] [--no-open]
```

手元のブラウザで管理画面を開きます。コマンドを覚えなくても、全セッションの様子を見て操作できます。終わるときは、`mvm ui` を動かしている端末で Ctrl-C を押します。

| オプション | 内容 |
|---|---|
| `--port <番号>` | 待ち受けるポート。既定は空いている番号 |
| `--no-open` | ブラウザを開かず、URL を表示するだけにする |

一覧の列は [watch](#watch) と同じです (名前、状態、経過、寿命の残り、メモリ、最後の出来事)。

画面でできること:

- **一覧**: [watch](#watch) と同じ表 (状態、経過、メモリ、最後の出来事) を 10 秒ごとに更新します。要対応は黄、エラーと OOM は赤、完了は緑で示し、表の下に対応が要ることを出します
- **詳細**: 行をクリックすると、画面の末尾 ([peek](#peek)) と最後の応答 ([result](#result)) を出します。一時停止中の VM は、見る前に確認します (見ると VM が再開して課金されるため)
- **操作**: 指示を送る ([send](#send)、Ctrl+Enter でも送れる)、再開 ([resume](#suspend--resume))、作り直す ([renew](#renew))、保存して停止 ([stop](#stop))。renew と停止は確認してから裏で進め、終わったら「裏で進めている操作」に結果を出します
- **新しく起動**: 名前、リポジトリ URL、エージェント、最初の指示を入れて起動します ([up](#up) と同じ)。リポジトリ URL を空欄にすると、`mvm up <名前>` と同じく前回の同じ名前の続きから始め、前回が無ければ `mvm ui` を起動したフォルダの `origin` を clone します。「前回の続きにしない」は `--fresh` です。起動は裏で進め、終わると一覧に出ます。動いている名前を指定すると「裏で進めている操作」にエラーが出ます。認証方式や `--idle` などの細かい設定は、コマンドの `mvm up` で指定します

注意:

- 127.0.0.1 でだけ待ち受けます。URL の `?t=` は起動のたびに作り直す合言葉で、これが無いと画面も API も開けません。他の人に URL を渡さないでください
- タブを表示している間は、`RUNNING` の台に定期的に通信するので、その台は自動でサスペンドされません (タブが裏にあるときは更新を止めます)。見終わったらタブを閉じるか、`mvm ui` を終了してください
- 画面の中でのターミナル操作はできません。一緒に操作するなら [ssh](#ssh) か VS Code ([ssh-config](#ssh-config)) を使います

### logs

```bash
mvm logs [--follow]
```

MicroVM のログ (CloudWatch Logs) を表示します。既定は直近 1 時間分、`--follow` は直近 10 分から表示し続けます。

---

## 接続

### ssh

```bash
mvm ssh <name>                    # tmux にアタッチする。Ctrl-b → d で抜ける
mvm ssh <name> -- <コマンド>      # コマンドを 1 つ実行する (例: mvm ssh a1 -- git log -3)
mvm ssh <name> --no-reconnect     # 切れても再接続しない
```

SSH over WebSocket で `agent` ユーザーとして接続します。tmux へのアタッチは、接続が切れると自動で再接続します (接続してすぐ切れることが 6 回続いたらあきらめます)。
tmux は手元の端末で代替画面を使わないので、抜けた後は tmux の最後の画面が端末に残ります (Windows Terminal などで、端末の応答がシェルに入力として流れるのを防ぐため)。
起動時に SSH 公開鍵 (`SSH_PUBLIC_KEY_FILE`) が無かったセッションには使えません (鍵を作ってから `mvm renew`)。

### forward

```bash
mvm forward <name> <port>... [--no-reconnect]
```

VM の中のポート (開発サーバーなど) を、手元の PC のポートにつなぎます。`3000` なら手元の `http://localhost:3000/` で VM の 3000 番を開けます。手元のポートを変えるときは `8080:3000` (手元:VM) と書きます。複数のポートを並べられます。Ctrl-C で終了します。

- `mvm ssh` と同じ経路 (SSH over WebSocket) を使うので、SSH 公開鍵を付けて起動したセッションが対象です。切れたら自動でつなぎ直します
- 手元のポートが使われているときは、つなぐ前に止まります
- VM の中のサーバーは `localhost` (`127.0.0.1`) で待ち受けていれば十分です
- つないでいる間は通信があるので、その VM はサスペンドされません

### login

```bash
mvm login <name> [claude|codex] [--device] [--no-browser] [--console]
```

VM の中のエージェントに、手元のブラウザを使ってログインします (`--claude-auth interactive` / `--codex-auth interactive` で起動したとき)。エージェントを省くと、セッションのエージェント (`--agent both` のときは指定が必要) です。詳しくは [auth.md](auth.md#vm-の中でログインする-mvm-login)。

| オプション | 説明 |
|---|---|
| (なし) | Codex: VM の中で `codex login` を動かし、手元の 1455 番ポートをつないで、ログインの URL を手元のブラウザで開く |
| `--device` | Codex: コードを入力する方式 (`codex login --device-auth`) |
| `--no-browser` | Codex: ブラウザを自動で開かない (表示された URL を自分で開く) |
| `--console` | Claude Code: Anthropic Console (API の従量課金) でログインする。`--remote-control` の VM では使えない |

`--remote-control` で起動した VM では、Claude Code は `claude auth login --claudeai` (claude.ai のサブスクリプション) でログインします。ログインすると、`claude` のウィンドウで待っていた Claude Code が `--remote-control` つきで自動で起動します ([usage.md](usage.md#remote-control-スマホや-claudeaicode-から操作する))。

`mvm ssh` と同じ経路を使うので、SSH 公開鍵を付けて起動したセッションが対象です。

### ssh-config

```bash
mvm ssh-config               # ~/.ssh/config 用の設定を表示する
mvm ssh-config --install     # ~/.ssh/config の pmva のブロックを書き換える
```

起動中のセッションごとに `Host pmva-<name>` を書きます。VS Code の Remote-SSH や `scp` から `pmva-<name>` で接続できます。セッションを増やしたり作り直したりしたら、もう一度実行してください。

### shell

```bash
mvm shell <name>
```

AWS 標準のシェル接続で、**root** として入ります。SSH が使えないときの予備や障害調査に使います。入った後 `agent-attach` でエージェントの tmux に接続できます。`exit` で抜けます。

### token

```bash
mvm token <name> [ポート]
```

MicroVM のエンドポイントと、指定したポート (既定 8080) 用の認証トークン (最大 60 分有効) を表示します。自作のツールから直接つなぐときに使います。

---

## 保存・停止

### save

```bash
mvm save <name>
```

ワークスペース (`~/work`) と会話の履歴 (`~/.claude`、`~/.codex`) を S3 に保存します。認証情報は保存しません。起動中は `SNAPSHOT_INTERVAL_SECONDS` (既定 30 分) ごとにも自動で保存します。

### suspend / resume

```bash
mvm suspend <name>
mvm resume <name>
```

一時停止 / 再開します。一時停止中は計算料金がかかりません。通信が来ると自動でも再開します (`IDLE_AUTO_RESUME=true`)。

`suspend` は、サスペンドの前にワークスペースを保存します (`stop` と同じ。動いているときだけ。保存に失敗しても警告してサスペンドは続けます)。サスペンド中も寿命 (`MAX_DURATION_SECONDS`) には数えます。サスペンドのまま `IDLE_SUSPENDED_SECONDS` (既定 6 時間) たつと終了するので、実行後に残りの寿命とあわせて表示します。

### stop

```bash
mvm stop <name> [--no-save]
```

保存してから終了します。一時停止中なら、保存のために一度再開します。保存に失敗しても終了します (終了時のフックがもう一度保存を試みます)。`--no-save` で保存せずに終了します (終了時のフックでも保存しないので、最後に保存した `latest` はそのまま残ります。古いイメージの VM ではフックに伝えられず、終了時に保存されることがあります)。

### stop-all

```bash
mvm stop-all [--no-save]
```

全セッションを保存してから終了します。1 日の終わりに使います。

### renew

```bash
mvm renew <name>
```

保存 → 終了 → 同じ設定で新しい MicroVM を起動して復元、を 1 回で行います。8 時間の寿命が近いときや、新しいイメージに移るときに使います。作業ブランチも引き継ぎます (最初の指示は送り直しません)。
`config.env` の設定は前回の起動時の値を引き継ぎますが、SSH 公開鍵 (`SSH_PUBLIC_KEY_FILE`) は今のファイルを読み直すので、鍵なしで起動した後に鍵を作った場合は `renew` で `mvm ssh` を使えるようになります。
保存に失敗したとき、または状態を確認できないときは、作業を失わないよう**終了せずに止まります**。

---

## 司令塔

### orchestrator

```bash
mvm orchestrator install claude|codex [--dir <フォルダ>]
mvm orchestrator uninstall claude|codex [--dir <フォルダ>]
```

手元の Claude Code / Codex に、MicroVM へ作業を任せるための skill (`mvm-orchestrator`) と設定を入れます。使い方は [orchestrator.md](orchestrator.md)。`config.env` がなくても実行できます。

| | `--dir` なし | `--dir <フォルダ>` |
|---|---|---|
| 対象 | 利用者全体 (どのフォルダで起動しても使える) | そのフォルダで起動したときだけ |
| Claude Code | `~/.claude/skills/`、`~/.claude/settings.json` に確認用の `mvm` コマンドの許可 | `<dir>/.claude/skills/`、`<dir>/.claude/settings.local.json` に許可と、重い処理を止めるフック |
| Codex | `~/.codex/skills/` (`CODEX_HOME` があればそこ) | `<dir>/.agents/skills/`、`<dir>/AGENTS.md` に決まりを書き足す |

- 何度実行しても重複しません。`uninstall` は入れたものだけを取り除きます
- 設定ファイルが JSON として読めないときは、書き換えずに止まります

## 費用

### cost

```bash
mvm cost [試算のオプション]                 # 試算 (scripts/estimate_cost.py)
mvm cost --actual [--days N] [--daily]      # Cost Explorer の実績
```

試算の主なオプション: `--agents` (台数、既定 2)、`--memory-gb` (既定 4)、`--hours-per-day` (既定 8)、`--days-per-month` (既定 22)、`--suspended-ratio` (サスペンドしている割合)、`--usd-jpy` (既定 150)、`--tax-rate` (消費税率、既定 0.10)。単価は東京の実請求の値で、`--vcpu-second` / `--gb-second` / `--snapshot-read-gb` / `--snapshot-write-gb` で上書きできる。全オプションは `mvm cost --help`、結果の読み方は [cost.md](cost.md)。
`--actual` は直近 N 日 (既定 7、今日を含む) の実績をサービス別に表示します。アカウント全体の金額で、反映は最大 24 時間ほど遅れます。

---

## 環境変数

| 変数 | 説明 |
|---|---|
| `PMVA_CONFIG` | 設定ファイルの場所 (既定はリポジトリ直下の `config.env`) |
| `PMVA_STATE_DIR` | セッションの記録やトークンのキャッシュの場所 (既定は `.state/`) |
| `PMVA_DEBUG` | ファイル名を指定すると、接続の詳細 (切断の理由など) を追記する |
| `HTTPS_PROXY` / `NO_PROXY` | 社内プロキシを経由して MicroVM に接続する (CONNECT) |
| `PMVA_NPM_REGISTRY` | `image versions` が参照する npm レジストリ (既定 `https://registry.npmjs.org`) |

AWS のプロファイルやリージョンは環境変数ではなく `config.env` の `AWS_PROFILE` / `AWS_REGION` で指定します ([config.md](config.md))。
