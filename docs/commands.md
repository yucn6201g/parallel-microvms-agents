# コマンドリファレンス

`mvm` の全コマンドとオプションの一覧です。`mvm <command> --help` でも同じ内容を確認できます。
呼び出し方は Windows (PowerShell) が `.\bin\mvm.cmd`、macOS が `bin/mvm` です。インストールスクリプトでエイリアスを登録していれば、どちらも `mvm` で呼べます ([quickstart.md](quickstart.md#3-このリポジトリを取得してセットアップする))。

| 分類 | コマンド |
|---|---|
| 準備 | [`doctor`](#doctor) / [`infra`](#infra) / [`secret`](#secret) / [`image`](#image) |
| 起動 | [`up`](#up) / [`fleet`](#fleet) |
| 確認・指示 | [`ls`](#ls) / [`peek`](#peek) / [`send`](#send) / [`watch`](#watch) / [`logs`](#logs) |
| 接続 | [`ssh`](#ssh) / [`ssh-config`](#ssh-config) / [`shell`](#shell) / [`token`](#token) |
| 保存・停止 | [`save`](#save) / [`suspend` / `resume`](#suspend--resume) / [`stop`](#stop) / [`stop-all`](#stop-all) / [`renew`](#renew) |
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

| kind | 中身 | 使う認証方式 |
|---|---|---|
| `claude-oauth-token` | `claude setup-token` で発行した長期トークン | `--claude-auth oauth-token` (Pro / Max) |
| `anthropic-api-key` | Anthropic の API キー | `--claude-auth api-key` |
| `anthropic-gateway` | `{"baseUrl":"...","authToken":"..."}` 形式の JSON | `--claude-auth gateway` |
| `openai-api-key` | OpenAI の API キー | `--codex-auth api-key` |
| `codex-auth-json` | ChatGPT でログイン済みの `~/.codex/auth.json` の中身 | `--codex-auth chatgpt` |
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
| `--claude-auth api-key\|oauth-token\|bedrock\|gateway\|interactive\|none` | `DEFAULT_CLAUDE_AUTH` | Claude Code の認証方式 ([auth.md](auth.md)) |
| `--codex-auth api-key\|chatgpt\|interactive\|none` | `DEFAULT_CODEX_AUTH` | Codex の認証方式 |
| `--claude-secret` / `--codex-secret` / `--github-secret <名前>` | 認証方式から決まる | 既定と別名で登録したシークレットを使う |
| `--no-github` | | GitHub トークンを使わない |
| `--repo <URL>` | | clone するリポジトリ |
| `--branch <ブランチ>` | `pmva/<name>` | 作業ブランチ。リモートにあれば追跡し、なければ作る |
| `--base-branch <ブランチ>` | リポジトリの既定ブランチ | 作業ブランチを新しく作るときの起点 |
| `--prompt "<指示>"` | | 起動時にエージェントへ渡す最初の指示 |
| `--restore latest\|s3://...` | | 保存したワークスペースから復元する |
| `--claude-args` / `--codex-args "<引数>"` | `DEFAULT_CLAUDE_ARGS` など | エージェントの追加引数 (例: `--claude-args "--dangerously-skip-permissions"`) |
| `--model <ID>` | `CLAUDE_MODEL` | Claude のモデル / Bedrock の推論プロファイル ID |
| `--bedrock-region <リージョン>` | `BEDROCK_REGION` | Bedrock を呼ぶリージョン |
| `--idle <秒>\|off` | `IDLE_MAX_SECONDS` | 受信通信がこの秒数無いとサスペンド |
| `--max-duration <秒>` | `MAX_DURATION_SECONDS` | 寿命 (上限 28800 = 8 時間) |
| `--notify` / `--no-notify` | `NOTIFY_WEBHOOK` | 完了・入力待ちを Webhook に通知する |

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
mvm ls [--all]
```

セッションの一覧 (名前、状態、エージェント、認証方式、起動時刻、MicroVM の ID) です。`--all` で終了済みも表示します。状態は `RUNNING` / `SUSPENDED` / `TERMINATED` などで、AWS から状態を取れなかったときは `UNKNOWN` と表示します (終了扱いにはしません)。

### peek

```bash
mvm peek <name> [行数]
```

エージェントの画面の末尾 (既定 30 行) と、初期化の状態 (`bootstrap=ready` / `failed: ...`)、最後に保存した時刻を表示します。接続はしないので、手元の負荷はほぼありません。

### send

```bash
mvm send <name> "<指示>" [--window claude|codex|shell] [--no-enter]
```

指示を tmux に入力して Enter を押します。送り先の既定は、Codex だけのセッションなら `codex`、それ以外は `claude` です。
`-` で始まる文字列を送るときは `mvm send <name> -- -h` のように `--` の後に書きます。`--no-enter` で Enter を押さずに入力だけします。

### watch

```bash
mvm watch [秒]
```

全セッションの状態と画面の末尾を、指定した秒数 (既定 60) ごとに表示し直します。Ctrl-C で終了します。表示している間は定期的に通信するので、そのセッションはサスペンドされません。

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
保存に失敗したとき、または状態を確認できないときは、作業を失わないよう**終了せずに止まります**。

---

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
