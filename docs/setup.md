# 構築手順と運用

## 0. 手元 PC の準備

| ツール | 用途 | macOS の例 |
|---|---|---|
| AWS CLI v2 (最新版) | `aws lambda-microvms` コマンド。古い CLI には無い | `brew install awscli` |
| jq / curl / zip | mvm の内部処理 | `brew install jq` |
| websocat | `mvm ssh` / `mvm shell` の WebSocket 接続 | `brew install websocat` |
| ssh | `mvm ssh` | 標準搭載 |
| python3 | `mvm cost` | 標準搭載 |

Windows は WSL2 上で使ってください。

SSH 接続を使う場合は鍵を用意します (既にあれば不要)。

```bash
ssh-keygen -t ed25519
```

## 1. 設定ファイル

```bash
cp config.example.env config.env
```

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
bin/mvm doctor
```

`list-managed-microvm-images` が成功し、`arn:aws:lambda:ap-northeast-1:aws:microvm-image:al2023-1` のようなベースイメージが表示されれば OK です。
ベースイメージの ARN が異なる場合は、`config.env` の `BASE_IMAGE_ARN` に設定します。

## 3. AWS リソースの作成

管理者権限のある認証情報で実行します。

```bash
bin/mvm infra deploy
```

作成されるもの:

- S3 バケット `pmva-<account>-<region>`: イメージ資材 (`images/`)、ワークスペース (`workspaces/`、`workspace-history/`)
- IAM ロール `pmva-microvm-build` (イメージビルド用)、`pmva-microvm-execution` (MicroVM 実行時)
- CloudWatch Logs `/aws/lambda-microvms/pmva`
- 管理ポリシー `pmva-operator`: 普段 mvm を使う IAM ユーザー / SSO の権限セットにアタッチする
- (既定) `pmva-bedrock-invoke`: Bedrock を使わない場合は `ENABLE_BEDROCK=false bin/mvm infra deploy`

## 4. 認証情報の登録

使う方式の分だけ登録します。詳細は [auth.md](auth.md) を参照してください。

```bash
bin/mvm secret set claude-oauth-token            # Claude Pro/Max (claude setup-token の出力)
bin/mvm secret set openai-api-key                # Codex を API キーで使う場合
bin/mvm secret set github-token                  # リポジトリの clone / push 用
bin/mvm secret ls
```

GitHub を使わない場合は `config.env` で `USE_GITHUB_TOKEN=false` にします。

## 5. イメージのビルド

```bash
bin/mvm image build     # 初回は 10〜20 分程度
bin/mvm image status
```

`image/` を zip にして S3 にアップロードし、`CreateMicrovmImage` を実行します。2 回目以降は `UpdateMicrovmImage` で新しいバージョンを作ります。
Claude Code / Codex は自動更新を無効にしているので、更新したいときはイメージを再ビルドします。
古いバージョンも保存料金がかかるので、不要になったら削除します (`aws lambda-microvms delete-microvm-image-version`)。

## 6. 1 台で動作確認

```bash
bin/mvm up test-01 --repo https://github.com/<owner>/<repo>.git
bin/mvm ls
bin/mvm peek test-01
bin/mvm ssh test-01            # tmux にアタッチ。Ctrl-b d で切断 (エージェントは動き続ける)
bin/mvm send test-01 "テストを実行して結果を要約して"
bin/mvm save test-01
bin/mvm stop test-01
```

[poc-checklist.md](poc-checklist.md) の項目もこの段階で確認してください。

## 7. 並列実行 (最大 10 本)

```bash
# 同じ設定で 10 台。ブランチは pmva/agent-01 〜 pmva/agent-10
bin/mvm fleet 10 --repo https://github.com/<owner>/<repo>.git

# 1 行に 1 つのタスクを書いたファイルを渡して、各エージェントに別々の指示を与える
bin/mvm fleet 5 --prefix feat --repo https://github.com/<owner>/<repo>.git --prompts-file tasks.txt

# Claude Code と Codex を混在させる (合計で MAX_AGENTS まで)
bin/mvm fleet 6 --prefix cc --agent claude
bin/mvm fleet 4 --prefix cx --agent codex --codex-auth chatgpt

bin/mvm watch          # 全台の画面末尾を 60 秒ごとに表示
```

VS Code から接続する場合:

```bash
bin/mvm ssh-config >> ~/.ssh/config   # Host pmva-<name> が追加される
```

追加後、VS Code の Remote-SSH で `pmva-agent-01` などに接続します。セッションを追加・作り直したら再生成してください。
VS Code Server は MicroVM 側で動くので、手元の負荷は小さく済みます。

## 8. 日々の運用

| やりたいこと | コマンド |
|---|---|
| 状態一覧 | `bin/mvm ls` (`--all` で終了済みも表示) |
| 一時停止 / 再開 | `bin/mvm suspend <name>` / `bin/mvm resume <name>` (通信が来れば自動でも再開) |
| 8 時間が近い | `bin/mvm renew <name>` (保存 → 終了 → 新しい VM で復元。同じ設定とブランチを引き継ぐ) |
| 前回の続きから始める | `bin/mvm up <name> --restore latest ...` |
| 1 日の終わり | `bin/mvm stop-all` (各台を保存してから終了) |
| ログを見る | `bin/mvm logs --follow` |
| 費用の確認 | `bin/mvm cost`、Cost Explorer |

VM の中で使えるコマンド:

- `agent-attach`: tmux に接続
- `~/.pmva/bootstrap.log`: 初期化ログ
- `~/.pmva/state`: 初期化状態 (`ready` / `failed: ...`)
- `/opt/pmva/snapshot.sh`: 手動保存

## トラブルシューティング

| 症状 | 確認すること |
|---|---|
| `image build` が FAILED | `bin/mvm logs`、`aws lambda-microvms list-microvm-image-builds`。`CONTAINER_BUILD_FAILED` なら Dockerfile の問題。手元で `docker build --platform linux/arm64 image/` を試す |
| `up` で AccessDenied | 操作者に `pmva-operator` ポリシーがあるか (`lambda:PassNetworkConnector`、`iam:PassRole` が必要) |
| `peek` が 502 | 起動直後なら数秒待つ。続く場合は `mvm shell` で入り、`~/.pmva/bootstrap.log` を確認 |
| `peek` が 401/403 | トークン期限切れ。`.state/tokens/<name>.*` を消して再実行 |
| bootstrap が `failed` | シークレット名・登録の有無、実行ロールの権限を確認 |
| エージェントが止まっている | アイドルサスペンドされていた可能性。通信すると再開する。無人で走らせるなら `--idle off` |
