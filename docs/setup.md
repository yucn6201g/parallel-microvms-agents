# 構築と管理

AWS の構成、操作用の IAM ユーザー、イメージの更新、片付けなど、環境を用意して保つための手順です。
初めての人は、先に [quickstart.md](quickstart.md) で 1 台目を動かしてください。このページは、そこで省いた選択肢と理由を補います。

## 手元 PC の準備

| ツール | 用途 | Windows (PowerShell) | macOS |
|---|---|---|---|
| Python 3.9 以上 | `mvm` 本体 | `winget install Python.Python.3.13` | 標準 (`xcode-select --install`) または `brew install python` |
| AWS CLI v2 (最新版) | `aws lambda-microvms`。古い CLI には無い | `winget install Amazon.AWSCLI` | `brew install awscli` |
| OpenSSH クライアント | `mvm ssh`、VS Code Remote-SSH | Windows 10 / 11 に標準搭載 (`ssh -V` で確認) | 標準搭載 |
| Git | このリポジトリの取得 | `winget install Git.Git` | 標準 |

- Windows の Python は winget 版か python.org 版を使います。Microsoft Store 版は ssh の ProxyCommand から起動できないことがあります
- 環境変数 `HTTPS_PROXY` が設定されていれば、社内プロキシ経由 (CONNECT) でも接続します
- `claude setup-token` や `codex login` で認証情報を作る方式 ([auth.md](auth.md)) では、手元にも Claude Code / Codex を入れます (トークンの発行にだけ使います)

### インストールスクリプト

前提ツールの確認、`config.env` の作成、`mvm` エイリアスの登録をまとめて行います。何度実行しても大丈夫です。

```powershell
# Windows (PowerShell)。エイリアスは $PROFILE に書く
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

```bash
# macOS / Linux。エイリアスは ~/.zshrc (zsh の場合) か ~/.bashrc に書く
scripts/install.sh
```

登録先は `-ProfilePath <ファイル>` / `--rc <ファイル>` で変えられ、`-NoAlias` / `--no-alias` で登録しないこともできます。登録した行には `# pmva` が付いていて、再実行するとその行だけを書き換えます。
Windows で「このシステムではスクリプトの実行が無効になっている」と出たら、`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を 1 回実行します。

### mvm の呼び出し方

インストールスクリプトを使わない場合は、次のように呼び出します。ドキュメントではどれも `mvm <command>` と書きます。

| OS | 実行方法 | 毎回 `mvm` と打てるようにする |
|---|---|---|
| Windows (PowerShell) | `.\bin\mvm.cmd <command>` | `Set-Alias mvm "$PWD\bin\mvm.cmd"` を `$PROFILE` に書く |
| Windows (コマンドプロンプト) | `bin\mvm <command>` | `bin` フォルダを PATH に追加 |
| macOS / Linux | `bin/mvm <command>` | `alias mvm="$PWD/bin/mvm"` を `~/.zshrc` に書く |

### SSH 鍵

`mvm ssh` や VS Code で接続するには鍵が要ります (既にあれば不要)。

```bash
ssh-keygen -t ed25519
```

`config.env` の `SSH_PUBLIC_KEY_FILE` (既定 `~/.ssh/id_ed25519.pub`) は、Windows では `C:\Users\<名前>\.ssh\id_ed25519.pub` を指します。鍵なしで起動した VM には `mvm shell` だけで入れます。鍵を作った後に `mvm renew` で起動し直すと `mvm ssh` を使えます。

## 設定ファイル

`config.env` は `KEY=VALUE` 形式のテキストで、Windows / macOS で同じものを使えます (シェルとしては実行されません)。インストールスクリプトが `config.example.env` からコピーして作ります。
全項目と、変更が効くタイミングは [config.md](config.md) にあります。

## 前提の確認とクォータ

```bash
mvm doctor
```

前提ツール、AWS の認証、リージョンで Lambda MicroVMs を使えるか、クォータ、SSH 公開鍵を確認します。ルートユーザーや管理者権限の認証情報、作成から 90 日を超えたアクセスキーを使っていると `!!` で警告します。

同時に動かせる台数は、AWS アカウントの MicroVM メモリのクォータで決まります。`mvm doctor` の「同時に何台まで」に表示され、超える `mvm up` / `fleet` は 1 台も起動する前に止まります。1 台のメモリ (`MEMORY_MIB`) を小さくすると、同じクォータでより多く動かせます。

| クォータ | 例 | 意味 |
|---|---|---|
| Max allocated ARM_64 MicroVM memory (L-CD1C0CC4) | 8GB | 同時に起動している VM のメモリの合計。8GB なら 4GB の VM が 2 台 (2GB なら 4 台) |
| Rate of RunMicrovm API requests (L-535CA9B6) | 1 回/秒 | そのままでよい (`mvm fleet` は 1 台ずつ順に起動する) |

**台数を増やしたいとき**: クォータの引き上げを申請します。上限で止まったときや `mvm doctor` で足りないときは、希望値入りの申請文が表示されます。現在の値が AWS の既定値より低く抑えられているアカウントでは、Service Quotas からの申請が「既定値より大きい値を指定してください」と拒否されます。その場合は AWS サポートセンターで「Service limit increase」のケースを作り、次のように依頼します (ベーシックサポートでも作れます)。

> サービス: AWS Lambda (Lambda MicroVMs) / リージョン: アジアパシフィック (東京) / クォータ: Max allocated ARM_64 MicroVM memory (L-CD1C0CC4) / 希望値: <台数 × 1 台のメモリ> GB / 用途: AI コーディングエージェントを MicroVM で並列実行するため

## AWS の構成を作る

管理者の権限で、**自分の PC から**実行します (できれば IAM Identity Center (SSO) の一時的な認証情報で)。

```bash
mvm infra deploy
```

テンプレートは 2 種類あり、`config.env` の `INFRA_TEMPLATE` で選びます。後から切り替えるときは、値を変えて `mvm infra deploy` をやり直します。

| `INFRA_TEMPLATE` | 作るもの |
|---|---|
| `full` (既定) | 下の一覧のもの |
| `minimal` | 動かすのに必要なものだけ (S3、IAM ロール、ログ、操作用ポリシー、Bedrock の権限)。キープアライブ、毎晩の自動停止、予算アラートは作らない |

`minimal` では、手元から接続していない VM は作業中でも `IDLE_MAX_SECONDS` (既定 30 分) で一時停止します。PC を閉じて任せたいときは `full` にするか、`mvm up <name> --idle off` で起動します (止め忘れると寿命の 8 時間まで課金されます)。

`full` で作るもの:

- S3 バケット `pmva-<アカウント>-<リージョン>`: イメージの資材、ワークスペースの保存、手元から送った設定
- IAM ロール: イメージのビルド用 `pmva-microvm-build` と、MicroVM の実行ロール 4 つ (使うエージェントの組み合わせごとに、読める認証情報を分けている。[architecture.md](architecture.md#セキュリティ))
- CloudWatch Logs `/aws/lambda-microvms/pmva`
- 操作用の管理ポリシー `pmva-operator` ([下記](#操作用の-iam-ユーザー))
- Bedrock (Mantle を含む) の呼び出し権限。使わないなら `ENABLE_BEDROCK=false`
- Claude Platform on AWS の呼び出し権限 (`ENABLE_ANTHROPIC_AWS=true` のときだけ)
- キープアライブ。毎晩の自動停止と予算アラートは、設定したときだけ作る ([下記](#キープアライブ毎晩の自動停止予算アラート))

`mvm infra deploy` / `delete` は IAM ロールやスタックを作成・削除するので、いつも管理者の権限で実行します。

## 操作用の IAM ユーザー

普段の操作 (起動・接続・停止・イメージのビルド) は、**`pmva-operator` ポリシーだけを付けた専用の IAM ユーザー**で行います。作り方は [quickstart.md の手順 5](quickstart.md#5-普段使う-iam-ユーザーに切り替える) にあります。

IAM Identity Center (SSO) を使っている場合は、アクセスキーを作らずに済む権限セットがおすすめです。権限セットを作るときにカスタマーマネージドポリシーとして `pmva-operator` (パスは `/`) だけを付け、自分のユーザーにこのアカウントで割り当てます。続けて `aws configure sso` (既存の SSO セッションがあれば `~/.aws/config` に `sso_role_name = <権限セットの名前>` のプロファイルを足すだけ) でプロファイルを作り、`config.env` の `AWS_PROFILE` に設定します。`mvm doctor` で「権限の確認は省略しました」(`pmva-operator` だけでは IAM の情報を読めないため) と出て、ほかの項目が OK なら準備完了です。

守ること:

- ルートユーザーには MFA を設定し、普段は使わない。ルートと管理者のアクセスキーは作らない
- 他の環境 (クラウドの開発環境、CI、他人の PC) に渡すのは `pmva-operator` のキーだけにする
- アクセスキーは 90 日を目安に作り直し、古いキーを削除する (`mvm doctor` が警告する)
- キーをチャットやリポジトリに貼らない (`config.env` と `.state/` は `.gitignore` 済み)
- CloudTrail の証跡を有効にし、予算アラートを設定して、身に覚えのない操作や費用に早く気付けるようにする

**このポリシーで足りるかの確認**: `pmva-operator` だけで、起動・接続・指示・保存・サスペンド・停止・費用の確認・ログの表示が通ることを 2026-11-22 に確かめています ([poc-checklist.md](poc-checklist.md))。テンプレートを変えたときや、権限に心当たりのないエラーが出たときは、次の順で確かめてください。

1. `config.env` の `AWS_PROFILE` を操作用のプロファイルにする
2. `mvm doctor` → `mvm image status` → `mvm up op-test --agent none` → `mvm peek op-test` → `mvm ssh op-test -- id` → `mvm stop op-test` を順に実行する
3. `AccessDenied` が出たら、エラーに出た操作 (例: `lambda:PassNetworkConnector`) を `infra/cloudformation.yaml` の `OperatorPolicy` に足し、管理者で `mvm infra deploy` する

## 認証情報の登録

使う方式の分だけ Secrets Manager に登録します。方式ごとの手順は [auth.md](auth.md) にあります。

```bash
mvm secret set claude-oauth-token     # Claude Pro / Max (claude setup-token の出力)
mvm secret set github-token           # リポジトリの clone / push 用
mvm secret ls
```

GitHub を使わない場合は `config.env` で `USE_GITHUB_TOKEN=false` にします。

## イメージ

```bash
mvm image build     # 初回は 10〜20 分
mvm image status
```

`image/` を zip にして S3 に置き、MicroVM のイメージを作ります。2 回目からは同じイメージの新しいバージョンになります。
`MEMORY_MIB`、`IMAGE_BROWSER_DEPS`、`IMAGE_EXTRA_PACKAGES` を変えたときもビルドし直します。起動中の VM は `mvm renew <name>` で新しいイメージに移れます (作業は保存して引き継ぎます)。

### Claude Code / Codex の版を上げる

版は `image/Dockerfile` で固定しています。全台が同じ版で動き、VM の中での自動更新も止めています。

```bash
mvm image versions             # 固定している版と npm の最新版を比べる
mvm image update-versions      # Dockerfile を最新版に書き換える (--claude <版> --codex <版> で指定もできる)
mvm image build                # 新しいイメージを作る。動作を確かめたら Dockerfile の変更を commit する
mvm image prune                # 古いバージョンを削除する (既定で新しい 2 版を残す)
```

古いバージョンは自動では消えず、1 版ごとに保存料金がかかります ([cost.md](cost.md#イメージ保存))。
このリポジトリを GitHub で管理していれば、`.github/workflows/agent-versions.yml` が毎週新しい版を確かめ、出ていれば Issue で知らせます。

## キープアライブ・毎晩の自動停止・予算アラート

`INFRA_TEMPLATE=full` で作る、AWS 側の仕組みです。設定を変えたら `mvm infra deploy` をやり直します。

### キープアライブ

MicroVM は、受信通信が `IDLE_MAX_SECONDS` (既定 30 分) 無いと一時停止します。エージェントが作業中でも、手元から何も接続していなければ止まってしまうので、作業中の VM だけを AWS 側から起こし続けます。

- VM が、画面の変化や CPU の使用から最後に作業した時刻を S3 に書く
- Lambda 関数 `pmva-keepalive` が `KEEPALIVE_RATE_MINUTES` (既定 5 分) ごとに動き、最後の作業から `KEEPALIVE_ACTIVE_SECONDS` (既定 900 秒) 以内の VM にだけ通信を送る
- 作業が終わった VM には何も送らないので、15〜45 分ほどで一時停止する

`KEEPALIVE_RATE_MINUTES=0` で無効になります。費用は月 $0.1 程度です。仕組みの詳細は [architecture.md](architecture.md#キープアライブ-作業中の-vm-だけを起こし続ける) にあります。

### 毎晩の自動停止

`NIGHTLY_STOP_TIME=23:00` (タイムゾーンは `NIGHTLY_STOP_TIMEZONE`、既定 `Asia/Tokyo`) を設定すると、毎日その時刻に全台のワークスペースを保存してから終了します。翌日は `mvm up <name> --restore latest ...` で続きから始められます。

- 一時停止中の VM も、保存のために一度再開してから終了する
- 保存に失敗した VM も終了する (終了時のフックがもう一度保存を試みる)
- 空にすると無効になる。AWS 上での動作はまだ確認していない

### 予算アラート

`BUDGET_MONTHLY_USD=100` と `BUDGET_EMAIL=you@example.com` の両方を設定すると、AWS Budgets のアラートを作ります。実績が予算の 80% を超えたときと、月末の予測が 100% を超えたときにメールが届きます。対象はアカウント全体の費用です。

## 片付け

使わなくなったら、次の順で消します。イメージは置いておくだけで保存料金がかかります。

```bash
mvm stop-all          # 各台を保存してから終了
mvm infra delete      # スタックと S3 バケットの中身を削除 (管理者で。確認あり)
mvm image delete      # イメージを全バージョンごと削除
```

シークレットは Secrets Manager のコンソールで個別に削除します。`mvm infra delete` が消せなかったリソースは、表示された削除コマンドで消します ([troubleshooting.md](troubleshooting.md))。
