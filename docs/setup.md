# 構築手順と運用

## 0. 手元 PC の準備 (Windows / macOS)

手元の CLI `mvm` は Python 標準ライブラリだけで書かれており、Windows と macOS のどちらでもそのまま動きます (WSL は不要)。
必要なのは次の 4 つだけです。

| ツール | 用途 | Windows (PowerShell) | macOS |
|---|---|---|---|
| Python 3.9 以上 | `mvm` 本体 | `winget install Python.Python.3.12` | 標準 (`xcode-select --install`) または `brew install python` |
| AWS CLI v2 (最新版) | `aws lambda-microvms`。古い CLI には無い (最新の v1 でも可) | `winget install Amazon.AWSCLI` | `brew install awscli` |
| OpenSSH クライアント | `mvm ssh`、VS Code Remote-SSH | Windows 10/11 に標準搭載 (`ssh -V` で確認) | 標準搭載 |
| Git | このリポジトリの取得 | `winget install Git.Git` | 標準 |

WebSocket の中継は `mvm` に組み込まれているので、websocat などの追加ツールは要りません。
環境変数 `HTTPS_PROXY` が設定されていれば、社内プロキシ経由 (CONNECT) でも接続します。

`claude setup-token` や `codex login` を手元で実行する認証方式 ([auth.md](auth.md)) を使う場合は、手元にも Claude Code / Codex をインストールしてください (トークンの発行にだけ使います)。

### mvm の呼び出し方

| OS | 実行方法 | 毎回 `mvm` と打てるようにする |
|---|---|---|
| Windows (PowerShell) | `.\bin\mvm.cmd <command>` | `Set-Alias mvm "$PWD\bin\mvm.cmd"` (`$PROFILE` に書くと常に有効) |
| Windows (コマンドプロンプト) | `bin\mvm <command>` | `bin` フォルダを PATH に追加 |
| macOS / Linux | `bin/mvm <command>` | `alias mvm="$PWD/bin/mvm"` (`~/.zshrc` に書くと常に有効) |

以降のドキュメントでは `mvm <command>` と書きます。

### SSH 鍵

SSH 接続を使う場合は鍵を用意します (既にあれば不要)。Windows / macOS とも同じコマンドです。

```bash
ssh-keygen -t ed25519
```

鍵は Windows では `C:\Users\<名前>\.ssh\id_ed25519.pub`、macOS では `~/.ssh/id_ed25519.pub` にできます。`config.env` の `SSH_PUBLIC_KEY_FILE=~/.ssh/id_ed25519.pub` は、どちらの OS でもこの場所を指します。

### Windows で clone するときの注意

MicroVM の中で動くスクリプトは改行コードが LF である必要があります。リポジトリの `.gitattributes` で LF に固定しており、`mvm image build` もアップロード前に LF へ変換するので、通常は何もしなくて大丈夫です。

## 1. 設定ファイル

```powershell
Copy-Item config.example.env config.env    # Windows (PowerShell)
```

```bash
cp config.example.env config.env           # macOS
```

`config.env` は `KEY=VALUE` 形式のテキストで、Windows / macOS 共通です (シェルとしては実行されません)。

主な項目は次のとおりです。

| 項目 | 既定値 | 説明 |
|---|---|---|
| `AWS_REGION` | `ap-northeast-1` | Lambda MicroVMs 対応リージョン |
| `MEMORY_MIB` | `4096` | 1 台のメモリ (= 2 vCPU)。重いビルドなら 8192 |
| `MAX_AGENTS` | `10` | 同時起動数の上限 |
| `IDLE_MAX_SECONDS` | `1800` | 接続が無いままこの秒数が経つとサスペンド |
| `DEFAULT_CLAUDE_AUTH` / `DEFAULT_CODEX_AUTH` | `oauth-token` / `api-key` | 既定の認証方式 ([auth.md](auth.md)) |
| `DEFAULT_CLAUDE_ARGS` | 空 | 例: `--dangerously-skip-permissions` (VM は隔離環境なので許可確認を省略してもよい) |

`AWS_PROFILE` を使う場合は `config.env` に書きます。

## 2. 前提チェック

```bash
mvm doctor
```

`list-managed-microvm-images` が成功し、`arn:aws:lambda:ap-northeast-1:aws:microvm-image:al2023-1` のようなベースイメージが表示されれば OK です。
ベースイメージの ARN が異なる場合は、`config.env` の `BASE_IMAGE_ARN` に設定します。

### クォータの確認 (並列で使う場合は必須)

既定のクォータでは 10 台を同時に動かせません (PoC で確認)。Service Quotas コンソールの「AWS Lambda」で次を確認し、足りなければ引き上げを申請してください。

| クォータ | 既定 (PoC のアカウント) | 10 台 × 4GB に必要な値 |
|---|---|---|
| Max allocated ARM_64 MicroVM memory (L-CD1C0CC4) | 8GB | 40GB 以上 |
| Rate of RunMicrovm API requests (L-535CA9B6) | 1 回/秒 | そのままで可 (`mvm fleet` は 1 台ずつ順に起動する) |

```bash
aws service-quotas get-service-quota --service-code lambda --quota-code L-CD1C0CC4 --region ap-northeast-1
```

`mvm doctor` でも「同時に何台まで起動できるか」を表示します。上限を超える `mvm up` / `mvm fleet` は、1 台も起動する前に止まります。

**引き上げの申請方法 (PoC で確認)**: PoC のアカウントでは、現在の値 (8GB) が AWS の既定値 (2048) より低く抑えられていました。この状態では Service Quotas の API / コンソールからの申請が「既定値より大きい値を指定してください」と拒否されます。その場合は AWS サポートセンターで「Service limit increase」のケースを作成し、次のように依頼してください (ベーシックサポートでも作成できます)。

> サービス: AWS Lambda (Lambda MicroVMs) / リージョン: アジアパシフィック (東京) / クォータ: Max allocated ARM_64 MicroVM memory (L-CD1C0CC4) / 希望値: 40 GB / 用途: AI コーディングエージェントを 4GB の MicroVM 10 台で並列実行するため

## 3. AWS リソースの作成

管理者権限のある認証情報で実行します。

```bash
mvm infra deploy
```

作成されるもの:

- S3 バケット `pmva-<account>-<region>`: イメージ資材 (`images/`)、ワークスペース (`workspaces/`、`workspace-history/`)
- IAM ロール `pmva-microvm-build` (イメージビルド用)、`pmva-microvm-execution` (MicroVM 実行時)
- CloudWatch Logs `/aws/lambda-microvms/pmva`
- 管理ポリシー `pmva-operator`: 普段 mvm を使う IAM ユーザー / SSO の権限セットにアタッチする
- (既定) `pmva-bedrock-invoke`: Bedrock を使わない場合は `config.env` で `ENABLE_BEDROCK=false` にしてから実行
- (既定) キープアライブ: Lambda 関数 `pmva-keepalive` と、それを 5 分ごとに呼ぶ EventBridge ルール ([下記](#pc-を閉じても作業を続ける-キープアライブ))
- (任意) 予算アラート `pmva-monthly`: `config.env` の `BUDGET_MONTHLY_USD` と `BUDGET_EMAIL` を両方設定したときだけ作る

### 操作用ポリシーの確認

普段は管理者ではなく、`pmva-operator` ポリシーだけを付けたユーザー (またはロール) で `mvm` を使うことを想定しています。PoC は管理者ユーザーで行ったため、このポリシーで足りるかはまだ実機で確認できていません (確認しようとした環境では、AWS へのリクエストが管理者の認証情報で署名し直されるため)。最初に次の手順で確認してください。

1. IAM ユーザー (または SSO の権限セット) に `pmva-operator` だけを付け、そのプロファイルを `config.env` の `AWS_PROFILE` に設定する
2. `mvm doctor` → `mvm image status` → `mvm up op-test --agent none` → `mvm peek op-test` → `mvm ssh op-test -- id` → `mvm stop op-test` を順に実行する
3. `AccessDenied` が出たら、エラーに出た操作 (例: `lambda:PassNetworkConnector`) を `infra/cloudformation.yaml` の `OperatorPolicy` に追加して `mvm infra deploy` する

`mvm infra deploy` / `mvm infra delete` は IAM ロールやスタックを作成・削除するので、管理者の権限で実行します。

## 4. 認証情報の登録

使う方式の分だけ登録します。詳細は [auth.md](auth.md) を参照してください。

```bash
mvm secret set claude-oauth-token            # Claude Pro/Max (claude setup-token の出力)
mvm secret set openai-api-key                # Codex を API キーで使う場合
mvm secret set github-token                  # リポジトリの clone / push 用
mvm secret ls
```

GitHub を使わない場合は `config.env` で `USE_GITHUB_TOKEN=false` にします。

## 5. イメージのビルド

```bash
mvm image build     # 初回は 10〜20 分程度
mvm image status
```

`image/` を zip にして S3 にアップロードし、`CreateMicrovmImage` を実行します。2 回目以降は `UpdateMicrovmImage` で新しいバージョンを作ります。
Claude Code / Codex は自動更新を無効にしているので、更新したいときはイメージを再ビルドします。
古いバージョンも保存料金がかかるので、不要になったら削除します (`aws lambda-microvms delete-microvm-image-version`)。

## 6. 1 台で動作確認

```bash
mvm up test-01 --repo https://github.com/<owner>/<repo>.git
mvm ls
mvm peek test-01
mvm ssh test-01            # tmux にアタッチ。Ctrl-b d で切断 (エージェントは動き続ける)。接続が切れたら自動で再接続する
mvm send test-01 "テストを実行して結果を要約して"   # "-" で始まる指示は mvm send test-01 -- "-h" のように書く
mvm save test-01
mvm stop test-01
```

[poc-checklist.md](poc-checklist.md) の項目もこの段階で確認してください。

## 7. 並列実行 (最大 10 本)

```bash
# 同じ設定で 10 台。ブランチは pmva/agent-01 〜 pmva/agent-10
mvm fleet 10 --repo https://github.com/<owner>/<repo>.git

# 1 行に 1 つのタスクを書いたファイルを渡して、各エージェントに別々の指示を与える
mvm fleet 5 --prefix feat --repo https://github.com/<owner>/<repo>.git --prompts-file tasks.txt

# Claude Code と Codex を混在させる (合計で MAX_AGENTS まで)
mvm fleet 6 --prefix cc --agent claude
mvm fleet 4 --prefix cx --agent codex --codex-auth chatgpt

mvm watch          # 全台の画面末尾を 60 秒ごとに表示
```

VS Code から接続する場合:

```bash
mvm ssh-config --install   # ~/.ssh/config (Windows は %USERPROFILE%\.ssh\config) に Host pmva-<name> を書き込む
```

書き込んだ後、VS Code の Remote-SSH で `pmva-agent-01` などに接続します。セッションを追加・作り直したら、もう一度実行してください (pmva のブロックだけが置き換わります)。
VS Code Server は MicroVM 側で動くので、手元の負荷は小さく済みます。

Windows の注意:

- Python は python.org 版か `winget` 版を使ってください。Microsoft Store 版の Python は実行ファイルのパスが特殊なため、ssh の ProxyCommand から起動できないことがあります。
- VS Code Remote-SSH の設定 `remote.SSH.path` は既定 (Windows 標準の OpenSSH) のままで使えます。

## PC を閉じても作業を続ける (キープアライブ)

MicroVM は、エンドポイントへの受信通信が `IDLE_MAX_SECONDS` (既定 30 分) 無いと一時停止します。エージェントが作業中でも、手元から何も接続していなければ止まってしまうので、AWS 側に「作業中の VM だけを起こし続ける」仕組みを置いています。

1. VM 内の `supervisor.py` が 30 秒ごとに、tmux の画面が変わったか、tmux 内のプロセス (エージェント、ビルド、テスト) が CPU を使ったかを調べ、最後に作業した時刻を 1 分ごとに S3 (`status/<name>.json`) に書く
2. Lambda 関数 `pmva-keepalive` が `KEEPALIVE_RATE_MINUTES` (既定 5 分) ごとに動き、最後の作業から `KEEPALIVE_ACTIVE_SECONDS` (既定 900 秒) 以内の VM にだけ `/healthz` を送る
3. 作業が終わった VM には何も送らないので、そのうち一時停止する (既定では作業終了から 15〜45 分ほど)

設定は `config.env` で変えられます (変更後に `mvm infra deploy`)。`KEEPALIVE_RATE_MINUTES` は `IDLE_MAX_SECONDS` より十分短くしてください。`KEEPALIVE_RATE_MINUTES=0` で無効になります。費用は Lambda がほぼ無料枠内、S3 への書き込みが 10 台 × 8 時間 × 22 日で月 $0.5 程度です。

## 完了・入力待ちの通知

エージェントの応答が終わったときや、許可・入力を待っているときに、Slack などの Webhook に通知できます。

1. Slack なら「Incoming Webhooks」で URL を発行する (Discord の Webhook URL、Teams のワークフローの URL でも可)
2. `mvm secret set notify-webhook` で URL を登録する
3. `config.env` で `NOTIFY_WEBHOOK=true` にするか、`mvm up <name> --notify` で起動する

| エージェント | 通知されるとき | 仕組み |
|---|---|---|
| Claude Code | 応答の完了 (`Stop`)、許可・入力待ち (`Notification`) | `~/.claude/settings.json` の hooks から `/opt/pmva/notify.sh` を呼ぶ |
| Codex | 応答の完了 (`agent-turn-complete`) | `~/.codex/config.toml` の `notify` から呼ぶ |

通知は `[<セッション名>] Claude Code の応答が終わりました` のような 1 行です。通知に失敗してもエージェントは止まりません。

## 予算アラート

`config.env` に `BUDGET_MONTHLY_USD=600` と `BUDGET_EMAIL=you@example.com` のように設定して `mvm infra deploy` すると、AWS Budgets のアラートを作ります。実績が予算の 80% を超えたときと、月末の予測が 100% を超えたときにメールが届きます。対象はアカウント全体の費用です (このリポジトリ以外の利用も含みます)。

## 8. 日々の運用

| やりたいこと | コマンド |
|---|---|
| 状態一覧 | `mvm ls` (`--all` で終了済みも表示) |
| 一時停止 / 再開 | `mvm suspend <name>` / `mvm resume <name>` (通信が来れば自動でも再開) |
| 8 時間が近い | `mvm renew <name>` (保存 → 終了 → 新しい VM で復元。同じ設定とブランチを引き継ぐ) |
| 前回の続きから始める | `mvm up <name> --restore latest ...` |
| 1 日の終わり | `mvm stop-all` (各台を保存してから終了) |
| 使わなくなったら片付ける | `mvm stop-all` → `mvm infra delete` → `mvm image delete` (イメージは置いておくと保存料金がかかる) |
| ログを見る | `mvm logs --follow` |
| 費用の確認 | `mvm cost`、Cost Explorer |

VM の中で使えるコマンド:

- `agent-attach`: tmux に接続
- `~/.pmva/bootstrap.log`: 初期化ログ
- `~/.pmva/state`: 初期化状態 (`ready` / `failed: ...`)
- `/opt/pmva/snapshot.sh`: 手動保存

## トラブルシューティング

| 症状 | 確認すること |
|---|---|
| `image build` が FAILED | `mvm logs`、`aws lambda-microvms list-microvm-image-builds`。`CONTAINER_BUILD_FAILED` なら Dockerfile の問題。手元で `docker build --platform linux/arm64 image/` を試す |
| `up` で AccessDenied | 操作者に `pmva-operator` ポリシーがあるか (`lambda:PassNetworkConnector`、`iam:PassRole` が必要) |
| `peek` が 502 | 起動直後なら数秒待つ。続く場合は `mvm shell` で入り、`~/.pmva/bootstrap.log` を確認 |
| `peek` が 401/403 | トークン期限切れ。`.state/tokens/<name>.*` を消して再実行 |
| bootstrap が `failed` | シークレット名・登録の有無、実行ロールの権限を確認 |
| エージェントが止まっている | アイドルサスペンドされていた可能性。通信すると再開する。キープアライブが無効になっていないか (`KEEPALIVE_RATE_MINUTES`) も確認 |
| SSH が `Broken pipe` で切れる | AWS 側で長時間の接続がまれに切られる (PoC で 6 回中 2 回)。`mvm ssh <name>` (コマンドなし) は自動で再接続する。原因を調べるときは `PMVA_DEBUG=<ファイル>` を設定すると切断理由を記録する |
