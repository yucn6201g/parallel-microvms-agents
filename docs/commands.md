# コマンドリファレンス

`mvm` の全コマンドとオプションの一覧です。`mvm <command> --help` でも同じ内容を確認できます。
呼び出し方は Windows (PowerShell) が `.\bin\mvm.cmd`、macOS が `bin/mvm` です。インストールスクリプトでエイリアスを登録していれば、どちらも `mvm` で呼べます ([quickstart.md](quickstart.md#3-このリポジトリを取得してセットアップする))。

| 分類 | コマンド |
|---|---|
| 準備 | [`doctor`](#doctor) / [`infra`](#infra) / [`secret`](#secret) / [`image`](#image) / [`profile`](#profile) |
| 起動 | [`up`](#up) / [`fleet`](#fleet) |
| 確認・指示 | [`ls`](#ls) / [`peek`](#peek) / [`follow`](#follow) / [`view`](#view) / [`top`](#top) / [`send`](#send) / [`wait`](#wait) / [`result`](#result) / [`watch`](#watch) / [`logs`](#logs) |
| 接続 | [`ssh`](#ssh) / [`forward`](#forward) / [`ssh-config`](#ssh-config) / [`shell`](#shell) / [`token`](#token) |
| 保存・停止 | [`save`](#save) / [`suspend` / `resume`](#suspend--resume) / [`stop`](#stop) / [`stop-all`](#stop-all) / [`renew`](#renew) |
| 司令塔 | [`orchestrator`](#orchestrator) |
| 費用 | [`cost`](#cost) |

セッション名 (`<name>`) は英小文字・数字・ハイフンで 31 文字以内です (例: `agent-01`)。名前ごとの状態は手元の `.state/sessions/<name>.json` に記録します。

---

## 準備

### doctor

```bash
mvm doctor
```

前提ツール (Python、AWS CLI、ssh)、AWS の認証、リージョンで Lambda MicroVMs が使えるか、MicroVM メモリのクォータ (同時に何台まで起動できるか)、SSH 公開鍵を確認します。
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
mvm infra delete     # スタックと S3 バケットの中身を削除する (確認あり)
```

- `deploy`: CloudFormation のテンプレート (`INFRA_TEMPLATE=full` なら `infra/cloudformation.yaml`、`minimal` なら `infra/cloudformation-minimal.yaml`) を `config.env` の値で作成・更新します。予算アラート・キープアライブ・毎晩の自動停止などの設定は、変更後に `deploy` し直すと反映されます ([config.md](config.md))。前回の削除で残った Lambda 関数があると、先に知らせて止まります
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
mvm image versions              # 固定している Claude Code / Codex の版と npm の最新版を比べる
mvm image update-versions [--claude <版>] [--codex <版>]   # image/Dockerfile の版を書き換える
```

- `build`: `image/` を zip にして S3 に置き、イメージ (2 回目からは新しいバージョン) を作ります。完了まで待ち、失敗したら原因を表示します
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
| `--repo <URL>` | | clone するリポジトリ |
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
| `--no-setup` | | リポジトリの `.pmva/setup.sh` を実行しない ([setup.md](setup.md#リポジトリごとの準備-pmvasetupsh)) |
| `--profile <名前>` / `--no-profile` | `PROFILE` | 手元から送った設定のまとまり ([profile](#profile)) を使う / 使わない |

注意:

- 起動時の設定 (指示文を含む) は 4KB までです。長い指示は起動後に `mvm send` で送ってください
- MicroVM メモリのクォータを超えるとき (と、`MAX_AGENTS` を設定していてその台数に達しているとき) は、起動せずに止まります
- 起動するエージェントの組み合わせに応じて、その認証情報だけを読める実行ロールが自動で選ばれます

### fleet

```bash
mvm fleet <台数> [--prefix <接頭辞>] [--prompts-file <ファイル>] [up と同じオプション]
```

`<接頭辞>-01` 〜 `<接頭辞>-NN` (既定の接頭辞は `agent`) をまとめて起動します。起動済みの名前は飛ばします。1 台目を起動する前に、全台が MicroVM メモリのクォータに収まるかを確かめ、収まらなければ 1 台も起動しません。
`--prompts-file` には 1 行に 1 つずつ指示を書きます。n 行目が n 台目の最初の指示になります (空行の台は `--prompt` の指示か、指示なし)。

---

## 確認・指示

### ls

```bash
mvm ls [--all] [--json]
```

セッションの一覧 (名前、状態、エージェント、認証方式、起動時刻、MicroVM の ID) です。`--all` で終了済みも表示します。状態は `RUNNING` / `SUSPENDED` / `TERMINATED` などで、AWS から状態を取れなかったときは `UNKNOWN` と表示します (終了扱いにはしません)。
`--json` で、リポジトリと作業ブランチも含めて JSON の配列で出力します (スクリプトや、手元のエージェントから読むとき)。

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
| `exited` | エージェントが終了して、シェルに戻った | 返る |
| `failed` | 起動の準備 (bootstrap) に失敗した | 返る |

ほかに、セッション全体として `stalled` (上の `--idle`)、`suspended` (VM がサスペンド中。作業中の VM はサスペンドしないので、作業していない)、`terminated` (VM が終了した) でも返ります。`--agent both` のセッションは、両方のエージェントが終わるまで待ちます。

- Codex は、許可・入力待ちを知らせる仕組みがないため、`waiting` になりません。承認待ちで止まったときは `--idle` の秒数が過ぎてから `stalled` で返ります。無人で動かすなら、承認を求めない設定 (`--codex-args "--dangerously-bypass-approvals-and-sandbox"` など) にしておきます
- 待っている間は状態を確かめる通信をするので、そのセッションはサスペンドされません (終わった台は確かめるのをやめます)
- 手元のエージェントから呼ぶときは、ツールの実行時間の上限 (10 分ほど) より短い `--timeout` を付け、終了コード 2 なら呼び直します
- このコマンドより前に作ったイメージは、エージェントの状態を返しません。その場合は画面と CPU が `--idle` の秒数止まったときだけ返ります。`mvm image build` で作り直してください

### result

```bash
mvm result <name> [--agent claude|codex] [--json]
```

エージェントの最後の応答の文章を表示します。画面ではなく応答そのものなので、長い報告も途中で切れません。`--agent both` のセッションは両方を表示します (片方だけなら `--agent`)。
まだ応答がないエージェントがあれば、終了コード 1 です。

- Claude Code: 応答の終わり (`Stop` フック) で渡される最後の応答。渡されない版では、会話の記録 (`~/.claude/projects/` の JSONL) から、最後のツール実行より後の文章を取り出します
- Codex: 応答の終わり (`notify` の `agent-turn-complete`) で渡される最後の応答

### watch

```bash
mvm watch [秒]
```

全セッションの状態 (エージェントの状態を含む) と画面の末尾を、指定した秒数 (既定 60) ごとに表示し直します。Ctrl-C で終了します。表示している間は定期的に通信するので、そのセッションはサスペンドされません。

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
| `--console` | Claude Code: Anthropic Console (API の従量課金) でログインする |

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

### stop

```bash
mvm stop <name> [--no-save]
```

保存してから終了します。一時停止中なら、保存のために一度再開します。保存に失敗しても終了します (終了時のフックがもう一度保存を試みます)。`--no-save` で保存せずに終了します。

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

試算の主なオプション: `--agents` (台数、既定 2)、`--memory-gb` (既定 4)、`--hours-per-day` (既定 8)、`--days-per-month` (既定 22)、`--suspended-ratio` (サスペンドしている割合)、`--usd-jpy` (既定 150)。全オプションは `mvm cost --help`、結果の読み方は [cost.md](cost.md)。
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
