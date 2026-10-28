# 構築手順と運用

## 0. 手元 PC の準備 (Windows / macOS)

手元の CLI `mvm` は Python 標準ライブラリだけで書かれており、Windows と macOS のどちらでもそのまま動きます (WSL は不要)。
必要なのは次の 4 つだけです。

| ツール | 用途 | Windows (PowerShell) | macOS |
|---|---|---|---|
| Python 3.9 以上 | `mvm` 本体 | `winget install Python.Python.3.12` | 標準 (`xcode-select --install`) または `brew install python` |
| AWS CLI v2 (最新版) | `aws lambda-microvms`。古い CLI には無い | `winget install Amazon.AWSCLI` | `brew install awscli` |
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
mvm ssh test-01            # tmux にアタッチ。Ctrl-b d で切断 (エージェントは動き続ける)
mvm send test-01 "テストを実行して結果を要約して"
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

## 8. 日々の運用

| やりたいこと | コマンド |
|---|---|
| 状態一覧 | `mvm ls` (`--all` で終了済みも表示) |
| 一時停止 / 再開 | `mvm suspend <name>` / `mvm resume <name>` (通信が来れば自動でも再開) |
| 8 時間が近い | `mvm renew <name>` (保存 → 終了 → 新しい VM で復元。同じ設定とブランチを引き継ぐ) |
| 前回の続きから始める | `mvm up <name> --restore latest ...` |
| 1 日の終わり | `mvm stop-all` (各台を保存してから終了) |
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
| エージェントが止まっている | アイドルサスペンドされていた可能性。通信すると再開する。無人で走らせるなら `--idle off` |
