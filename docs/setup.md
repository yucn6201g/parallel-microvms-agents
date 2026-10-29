# 構築手順と運用

初めて使う場合は、まず [quickstart.md](quickstart.md) (最短手順) で 1 台目を動かしてください。このページは各手順の詳細と、日々の運用・トラブルシューティングをまとめたものです。

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

### インストールスクリプト (おすすめ)

前提ツールの確認、`config.env` の作成、`mvm` エイリアスの登録をまとめて行います。何度実行しても大丈夫です。

```powershell
# Windows (PowerShell)。エイリアスは $PROFILE (全ホスト共通のプロファイル) に書く
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

```bash
# macOS / Linux。エイリアスは ~/.zshrc (zsh の場合) か ~/.bashrc に書く
scripts/install.sh
```

登録先は `-ProfilePath <ファイル>` / `--rc <ファイル>` で変えられ、`-NoAlias` / `--no-alias` で登録しないこともできます。登録した行には `# pmva` が付いていて、再実行するとその行だけを書き換えます。
Windows で「このシステムではスクリプトの実行が無効になっている」と出てプロファイルが読み込まれない場合は、`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を 1 回実行します。

### mvm の呼び出し方

インストールスクリプトを使わない場合は、次のように呼び出します。

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

管理者権限のある認証情報で、**自分の PC から**実行します (できれば IAM Identity Center (SSO) の一時的な認証情報で)。管理者のアクセスキーを作って他の環境に渡すことはしないでください ([下記](#操作用の-iam-ユーザー))。

```bash
mvm infra deploy
```

作成されるもの:

- S3 バケット `pmva-<account>-<region>`: イメージ資材 (`images/`)、ワークスペース (`workspaces/`、`workspace-history/`)
- IAM ロール `pmva-microvm-build` (イメージビルド用)、`pmva-microvm-execution*` (MicroVM 実行時。使うエージェントの組み合わせごとに 4 つ。[architecture.md](architecture.md#セキュリティ))
- CloudWatch Logs `/aws/lambda-microvms/pmva`
- 管理ポリシー `pmva-operator`: 普段 mvm を使う IAM ユーザー / SSO の権限セットにアタッチする
- (既定) `pmva-bedrock-invoke`: Bedrock を使わない場合は `config.env` で `ENABLE_BEDROCK=false` にしてから実行
- (既定) キープアライブ: Lambda 関数 `pmva-keepalive` と、それを 5 分ごとに呼ぶ EventBridge ルール ([下記](#pc-を閉じても作業を続ける-キープアライブ))
- (任意) 予算アラート `pmva-monthly`: `config.env` の `BUDGET_MONTHLY_USD` と `BUDGET_EMAIL` を両方設定したときだけ作る
- (任意) 毎晩の自動停止: Lambda 関数 `pmva-nightly-stop` と EventBridge Scheduler のスケジュール (`NIGHTLY_STOP_TIME` を設定したとき)

### 操作用の IAM ユーザー

普段の `mvm` の操作 (起動・接続・停止・イメージのビルド) は、管理者ではなく **`pmva-operator` ポリシーだけを付けた専用の IAM ユーザー**で行います。`mvm doctor` は、ルートユーザーや管理者権限 (`AdministratorAccess`) の認証情報、作成から 90 日を超えたアクセスキーを使っていると警告します。

> **PoC で起きたこと**: PoC では管理者ユーザーの長期アクセスキーをクラウドの開発環境 (Claude Code on the web) に渡して作業しました。普段と違う場所から管理者のキーで IAM ロールや Lambda 関数を次々に作ったためか、AWS の不正利用検知で「第三者に不適切にアクセスされた可能性がある」と判断され、アカウントの一部サービスが制限されました (Lambda 関数の API の拒否、MicroVM メモリのクォータ 0)。解除には、ルートユーザーのパスワード変更と MFA の設定、CloudTrail の確認、AWS サポートへの連絡が必要です ([poc-checklist.md](poc-checklist.md) の G)。

作り方 (管理者の認証情報で、自分の PC から実行):

```bash
mvm infra outputs                                   # OperatorPolicyArn を確認
aws iam create-user --user-name pmva-operator
aws iam attach-user-policy --user-name pmva-operator --policy-arn <OperatorPolicyArn>
aws iam create-access-key --user-name pmva-operator # 出力されたキーを次のコマンドで入力する
aws configure --profile pmva                        # キーは手元の ~/.aws/credentials にだけ保存する
```

`config.env` に `AWS_PROFILE=pmva` を書き、`mvm doctor` で「管理者権限ではありません」と出ることを確認します。

守ること:

- 管理者のアクセスキー・ルートユーザーのアクセスキーは作らない。他の環境 (クラウドの開発環境、CI、他人の PC) に渡すのは `pmva-operator` のキーだけにする
- ルートユーザーには MFA を設定し、普段は使わない
- アクセスキーは 90 日を目安に作り直し、古いキーは削除する (`mvm doctor` が警告する)
- キーをチャットやリポジトリに貼らない (`config.env` と `.state/` は `.gitignore` 済み)
- 予算アラート ([下記](#予算アラート)) を設定し、身に覚えのない費用に早く気付けるようにする

**このポリシーで足りるかの確認**: PoC は管理者ユーザーで行ったため、`pmva-operator` だけで足りるかはまだ実機で確認できていません (確認しようとした環境では、AWS へのリクエストが管理者の認証情報で署名し直されるため)。最初に次の手順で確認してください。

1. 上の手順で作ったプロファイルを `config.env` の `AWS_PROFILE` に設定する
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
Claude Code / Codex の版は `image/Dockerfile` の `CLAUDE_CODE_VERSION` / `CODEX_VERSION` で固定しています (全台が同じ版で動き、VM 内の自動更新も無効)。更新するときは次の手順です。

```bash
mvm image versions             # 固定している版と npm の最新版を比べる
mvm image update-versions      # Dockerfile を最新版に書き換える (--claude 2.1.0 --codex 0.1.0 のように版を指定することもできる)
mvm image build                # 新しいイメージを作る。動作を確認したら Dockerfile の変更を commit する
mvm renew <name>               # 起動中の VM は renew で新しいイメージに移れる
```

GitHub で管理している場合は、`.github/workflows/agent-versions.yml` が毎週 (月曜 9:17) 新しい版を確認し、出ていれば Issue「Claude Code / Codex の新しい版があります」を立てます (Actions タブの「Agent versions」から手動でも実行できます)。
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

## 毎晩の自動停止 (止め忘れ防止)

`config.env` の `NIGHTLY_STOP_TIME=23:00` (`NIGHTLY_STOP_TIMEZONE=Asia/Tokyo`) を設定して `mvm infra deploy` すると、毎日その時刻にすべての MicroVM のワークスペースを S3 に保存してから終了します。翌日は `mvm up <name> --restore latest ...` で続きから始められます。

- EventBridge Scheduler が Lambda 関数 `pmva-nightly-stop` を呼び、このスタックのイメージから起動した VM (RUNNING / SUSPENDED) をすべて保存してから終了する (サスペンド中の VM は保存の通信で自動的にレジュームされる)
- 保存に失敗した VM も終了する (terminate フックがもう一度保存を試みる)
- 空にすると無効になる。費用はほぼ無料枠内
- 自動テストのみで、AWS 上での動作は未確認 ([poc-checklist.md](poc-checklist.md) の G)

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
| 費用の確認 | `mvm cost` (試算)、`mvm cost --actual --days 7 --daily` (Cost Explorer の実績。アカウント全体) |
| Claude Code / Codex の更新 | `mvm image versions` → `mvm image update-versions` → `mvm image build` |

VM の中で使えるコマンド:

- `agent-attach`: tmux に接続
- `~/.pmva/bootstrap.log`: 初期化ログ
- `~/.pmva/state`: 初期化状態 (`ready` / `failed: ...`)
- `/opt/pmva/snapshot.sh`: 手動保存

## GitHub リポジトリの設定 (任意)

このリポジトリを自分の GitHub で管理する場合は、次の設定をおすすめします。

1. **既定ブランチを `main` にする**: Settings → General → Default branch で `main` を選ぶ
2. **`main` を保護する**: Settings → Branches → Add branch ruleset (または Add rule) で `main` を対象にし、「Require status checks to pass」に次の CI ジョブを追加する
   - `lint`
   - `test (ubuntu-latest, Python 3.13)`
   - `test (windows-latest, Python 3.9)`、`test (windows-latest, Python 3.13)`
   - `test (macos-latest, Python 3.9)`、`test (macos-latest, Python 3.13)`
3. ひとりで使う場合は「Require a pull request before merging」のレビュー必須は付けない (自分のプルリクエストをマージできなくなる)

非公開リポジトリのブランチ保護は GitHub の有料プランが必要な場合があります。

## トラブルシューティング

| 症状 | 確認すること |
|---|---|
| `image build` が FAILED | `mvm logs`、`aws lambda-microvms list-microvm-image-builds`。`CONTAINER_BUILD_FAILED` なら Dockerfile の問題。手元で `docker build --platform linux/arm64 image/` を試す |
| `up` で AccessDenied | 操作者に `pmva-operator` ポリシーがあるか (`lambda:PassNetworkConnector`、`iam:PassRole` が必要) |
| `peek` が 502 | 起動直後なら数秒待つ。続く場合は `mvm shell` で入り、`~/.pmva/bootstrap.log` を確認 |
| `peek` が 401/403 | トークン期限切れ。`.state/tokens/<name>.*` を消して再実行 |
| bootstrap が `failed` | シークレット名・登録の有無、実行ロールの権限を確認 |
| エージェントが止まっている | アイドルサスペンドされていた可能性。通信すると再開する。キープアライブが無効になっていないか (`KEEPALIVE_RATE_MINUTES`) も確認 |
| SSH が `Broken pipe` で切れる | 途中の通信経路 (社内プロキシなど) が長時間の接続を切ることがある。PoC での切断は、すべてクラウドセッションの送信用中継が再起動したとき (WebSocket の close コード 1012 `server_draining`) で、AWS 側の切断ではなかった。`mvm ssh <name>` (コマンドなし) は自動で再接続する。原因を調べるときは `PMVA_DEBUG=<ファイル>` を設定すると切断理由を記録する |
| `up` で「メモリ上限を超えます (上限 0GB)」 | `mvm doctor` でクォータを確認。0 の場合は AWS の不正利用検知でアカウントが制限されている可能性がある (PoC 中に 8GB → 0GB に変わり、同時に Lambda 関数の API も拒否されるようになった)。AWS からのメールとサポートケースを確認する |
| `infra delete` で「削除できなかったため、スタックから外して残しました」と出る | Lambda 関数の API が拒否されているなどで消せなかったリソースだけを残し、残りは削除している。表示された `aws lambda delete-function ...` を、API が使えるようになってから実行する (残したままだと次の `infra deploy` が「前回の削除で残った Lambda 関数があります」で止まる) |
| `mvm ls` の状態が `UNKNOWN` | AWS から状態を取得できなかった (権限・通信・アカウントの制限)。終了扱いにはしていないので、原因を解消すれば元に戻る。`PMVA_DEBUG=<ファイル>` でエラーを記録できる |
| `mvm renew` が「保存に失敗したので、終了せずに止めました」 | 作業内容を失わないよう VM を残している。`mvm ssh` で入って原因 (ディスク不足など) を確認するか、保存せずに作り直すなら `mvm stop <name> --no-save` → `mvm up <name> --restore latest` |
| AWS から「アカウントが第三者に不適切にアクセスされた可能性」のメール | メールの手順 (ルートのパスワード変更、MFA、CloudTrail の確認、不要なリソースの確認) を行い、使っていたアクセスキーを無効化・削除してから、サポートケースに返信する。以後は[操作用の IAM ユーザー](#操作用の-iam-ユーザー)を使う |
| `infra deploy` で `pmva-keepalive` / `pmva-nightly-stop` の作成が AccessDenied | 操作者の権限 (`lambda:CreateFunction` など) と、AWS Organizations の SCP を確認。`aws lambda list-functions` も拒否される場合はアカウント側の制限 |
