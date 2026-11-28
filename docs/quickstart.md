# はじめての人向けの最短手順

初めての人が、手元の PC (Windows / macOS) から **1 台目のエージェントを動かすまで**の手順を、上から順に並べたページです。
各手順の詳しい説明や選択肢は [setup.md](setup.md) にあります。迷ったときだけ参照してください。

| 手順 | やること | 目安の時間 |
|---|---|---|
| [1](#1-aws-アカウントを守る) | AWS アカウントを守る (MFA と予算) | 10 分 |
| [2](#2-手元-pc-にツールを入れる) | 手元 PC にツールを入れる | 10〜20 分 |
| [3](#3-このリポジトリを取得してセットアップする) | このリポジトリを取得してセットアップする | 5 分 |
| [4](#4-管理者のプロファイルを用意する) | 管理者のプロファイルを用意する | 5 分 |
| [5](#5-mvm-setup-で残りの準備をする) | `mvm setup` で残りの準備をする (AWS の土台、普段使うユーザー、認証情報、イメージ) | 20〜30 分 (大半は待つだけ) |
| [6](#6-1-台目を動かす) | 1 台目を動かす | 5 分 |
| [7](#7-使い終わったら止める) | 使い終わったら止める | 1 分 |

**費用の目安**: 4GB の MicroVM 1 台で 1 時間あたり約 $0.29 (税抜)。止めれば計算料金はかかりません。ほかに保存などの固定費が月 $4 前後です ([cost.md](cost.md))。

以下、コマンドは Windows (PowerShell) と macOS (ターミナル) で書き方が違うところだけ分けて書きます。分けていないものは両方で同じです。

---

## 1. AWS アカウントを守る

AI エージェントに AWS を使わせるので、最初にアカウントの守りを固めます。**この手順を飛ばさないでください**。

1. AWS マネジメントコンソールに**ルートユーザー**でサインインし、右上のアカウント名 →「セキュリティ認証情報」→「MFA を割り当てる」で MFA を設定する
2. 同じ画面の「アクセスキー」に何もないことを確認する (ルートのアクセスキーは作らない)
3. 「Billing and Cost Management」→「予算」で月額の予算アラートを作る (例: $100、実績が 80% を超えたらメール)。あとで `mvm` からも作れます ([setup.md](setup.md#予算アラート))
4. 以後、ルートユーザーは使わない。管理者の操作は、IAM Identity Center (SSO) のユーザーか、MFA を付けた管理者の IAM ユーザーで行う

> 守ること: 管理者のアクセスキーやルートのアクセスキーを、チャット・リポジトリ・クラウドの開発環境に渡さない。他の環境に渡すのは、手順 5 で作る権限を絞ったユーザーのキーだけ。

## 2. 手元 PC にツールを入れる

必要なのは Git、Python 3.9 以上、AWS CLI v2、OpenSSH クライアントの 4 つです。

**Windows (PowerShell)**

```powershell
winget install Git.Git
winget install Python.Python.3.13
winget install Amazon.AWSCLI
```

OpenSSH クライアントは Windows 10 / 11 に標準で入っています (`ssh -V` で確認。無ければ「設定」→「システム」→「オプション機能」で追加)。
インストール後は PowerShell を開き直してください。Python は Microsoft Store 版ではなく、上の winget 版か python.org 版を使います。

**macOS (ターミナル)**

```bash
xcode-select --install       # Git と Python 3 (すでに入っていれば不要)
brew install awscli          # Homebrew が無ければ https://brew.sh から入れる
```

SSH 鍵は、無ければ手順 5 の `mvm setup` が作ります。

## 3. このリポジトリを取得してセットアップする

```bash
git clone https://github.com/yucn6201g/parallel-microvms-agents.git
cd parallel-microvms-agents
```

インストールスクリプトが、ツールの確認、設定ファイル `config.env` の作成、`mvm` コマンドの登録を行います。

**Windows (PowerShell)**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

**macOS (ターミナル)**

```bash
scripts/install.sh
```

すべて `[OK]` なら成功です。`[NG]` が出たら、表示に従ってツールを入れてからもう一度実行してください。
**ここで新しいターミナルを開き直します** (開き直すと `mvm` と打つだけで動くようになります。Windows で「スクリプトの実行が無効」と出たら `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` を 1 回実行)。以降は `parallel-microvms-agents` フォルダで作業します。

## 4. 管理者のプロファイルを用意する

AWS の土台を作るところだけ管理者の権限が要ります。**自分の PC で**行ってください。

- IAM Identity Center (SSO) を使っている場合 (おすすめ): `aws configure sso` を実行し、プロファイル名を `admin` にする
- 使っていない場合: 管理者の IAM ユーザーのアクセスキーを作り、`aws configure --profile admin` で入力する。**手順 5 が終わったらこのキーは削除する**

リージョンは既定で東京 (`ap-northeast-1`) です。キープアライブ・毎晩の自動停止・予算アラートが要らなければ、手順 5 の前に `config.env` に `INFRA_TEMPLATE=minimal` を書き足すと、動かすのに必要なリソースだけを作ります ([setup.md](setup.md#aws-の構成を作る))。

## 5. `mvm setup` で残りの準備をする

```bash
mvm setup --profile admin
```

まだ済んでいない準備を 1 つずつ聞きながら進めます。聞かれたら Enter (はい) で進めてください。途中で止めても、もう一度 `mvm setup` を実行すれば続きから始まります。

| 順番 | やること | 聞かれたときに用意するもの |
|---|---|---|
| 1 | AWS の土台 (S3・IAM ロール・ログなど) を作る (`mvm infra deploy`、数分)。先に月の予算アラートを作るか聞かれる | なし。予算アラートが要るなら、月の金額 (USD) と通知先のメール。要らなければ Enter |
| 2 | 普段使う IAM ユーザー `pmva-operator` (`pmva-operator` ポリシーだけを付ける) を作り、プロファイル `pmva` に切り替える (`mvm infra operator`)。SSO の管理者には聞かず、権限セットを作る案内を出す | なし (キーは画面に出ず、`~/.aws/credentials` にだけ書かれる) |
| 3 | エージェントの認証情報を Secrets Manager に登録する (`mvm secret set`) | 既定の Claude Pro / Max なら、手元に Claude Code を入れて `claude setup-token` を実行し、表示されたトークン。GitHub のリポジトリで作業させるなら、対象リポジトリに絞った Fine-grained personal access token (Contents と Pull requests を Read and write)。GitHub を使わないなら `n` と答える |
| 4 | SSH 鍵が無ければ作る (`ssh-keygen`) | なし (質問はすべて Enter で構いません) |
| 5 | Claude Code / Codex 入りのイメージをビルドする (`mvm image build`、10〜20 分) | なし。待つだけ |
| 6 | 手元に Claude Code / Codex があれば、司令塔の skill を入れる (`mvm orchestrator install`) | なし。入れると、手元の Claude Code に「README を MicroVM で要約させて」のように頼むだけで、mvm のコマンドを代わりに打ちます ([orchestrator.md](orchestrator.md)) |

最後に `mvm doctor` で確かめ、「準備ができました」と出れば完了です。`config.env` の `AWS_PROFILE` は `pmva` に書き換わっています。
`mvm doctor` の「同時に何台まで」の行も見ておきます。この台数まで並列に動かせます。足りなければ引き上げを申請します (審査に数日かかることがあります。[setup.md](setup.md#前提の確認とクォータ))。

- 値の入力は画面に表示されず、AWS Secrets Manager にだけ保存されます
- 最初に認証方式を番号で聞かれます (1 Pro/Max、2 Anthropic の API キー、3 Amazon Bedrock、4 Codex の ChatGPT)。選んだ結果は `config.env` の `DEFAULT_AGENT` / `DEFAULT_CLAUDE_AUTH` / `DEFAULT_CODEX_AUTH` に書かれ、その方式に要る認証情報を聞かれます ([auth.md](auth.md))。それ以外の方式 (Mantle、ゲートウェイなど) は、先に `config.env` を書き換えておくと聞かれません
- IAM Identity Center (SSO) で管理者になっている場合、2 番目は聞かれません。[setup.md](setup.md#操作用の-iam-ユーザー) の手順で `pmva-operator` ポリシーだけを付けた権限セットを作り、そのプロファイルを `config.env` の `AWS_PROFILE` に書いてください
- 管理者で `mvm infra deploy` をやり直すときは `mvm infra deploy --admin-profile admin` のように付けます。`config.env` の `AWS_PROFILE` は環境変数 `AWS_PROFILE` より優先されるので、`$env:AWS_PROFILE` を設定しても切り替わりません
- 手順 4 で管理者のアクセスキーを作った場合は、ここで IAM コンソールから削除します (`mvm infra deploy` をやり直すときは、その都度作って使い終わったら削除します)

1 つずつ自分で実行したいときは、上の表のかっこの中のコマンドを順に実行しても同じです ([setup.md](setup.md))。

## 6. 1 台目を動かす

```bash
mvm up test-01 --repo https://github.com/<owner>/<repo>.git --prompt "README を読んで概要を説明して"
```

clone したいリポジトリのフォルダで打つなら、`--repo` は省けます (そのフォルダの `origin` を使います)。GitHub を使わない場合は `mvm up test-01 --no-repo --no-github --prompt "..."` とします。10 秒ほどで起動します。

```bash
mvm peek test-01                         # エージェントの画面の最後を表示する (接続せずに確認)
mvm send test-01 "テストを実行して結果を要約して"   # 指示を送る
mvm ssh test-01                          # 画面に入って直接操作する。抜けるときは Ctrl-b を押してから d
```

手順 5 で司令塔の skill を入れたなら、コマンドを打たずに手元の Claude Code (または Codex) へ「test-01 という MicroVM でこのリポジトリの README を要約させて」と頼んでも同じことができます。起動と停止の前には確認を求めてきます。
コマンドの代わりにブラウザで見るなら `mvm ui` です。全台の状態の一覧が開き、行をクリックすると画面と最後の応答が出て、指示もそこから送れます。「新しく起動」から次の台を起動することもできます。

`mvm ssh` から抜けても、エージェントは AWS 上で作業を続けます。手元の PC を閉じても止まりません。
作業ブランチは `pmva/test-01` です。エージェントに commit と push をさせれば、成果は GitHub に残ります。

## 7. 使い終わったら止める

```bash
mvm stop test-01     # ワークスペースを保存してから終了する (全部まとめて止めるなら mvm stop-all)
```

次回は `mvm up test-01` だけで続きから始められます (前回の repo とブランチを引き継ぎ、保存したワークスペースを戻します)。
止め忘れが心配なら、`config.env` に `NIGHTLY_STOP_TIME=23:00` を書き足し、管理者のプロファイルで `mvm infra deploy` をやり直すと、毎晩その時刻に全台を保存してから止めます。

使った金額は、翌日以降に次のコマンドで確認できます (Cost Explorer への反映は最大 24 時間ほど遅れます)。

```bash
mvm cost --actual --days 7
```

---

## 次にやること

- 場面別の使い方 (並列実行、PC を閉じて任せる、8 時間を超える、費用を抑える): [usage.md](usage.md)
- 全コマンドと設定項目: [commands.md](commands.md)、[config.md](config.md)
- 何台も並べて動かす: [usage.md](usage.md#別々のタスクを並列に進める)。同時に動かせる台数は `mvm doctor` で確認
- VS Code から MicroVM の中のファイルを開く: [usage.md](usage.md#vs-code-で開くファイルを手元に持ってくる)
- 応答が終わったら Slack などに通知する: [usage.md](usage.md#完了入力待ちを通知する)
- よく使うコマンドのまとめ: [usage.md](usage.md#よく使うコマンド)

## うまくいかないとき

| 症状 | 確認すること |
|---|---|
| `mvm` が見つからない | 手順 3 の後でターミナルを開き直したか。フォルダの中で `bin/mvm` (Windows は `.\bin\mvm.cmd`) なら動くか |
| `mvm setup` / `mvm doctor` で「AWS 認証情報がありません」「AWS に接続できません」 | `mvm setup --profile` に渡した名前と、`aws configure` で付けたプロファイル名が同じか。SSO なら `aws sso login --profile admin` |
| `mvm doctor` で「lambda-microvms に未対応」 | AWS CLI が古い。最新版に入れ直す |
| `mvm setup` の `mvm infra deploy` で AccessDenied | 管理者のプロファイルで実行しているか (`mvm setup --profile admin`) |
| `mvm up` で「メモリ上限を超えます」 | クォータで決まる台数まで起動している。`mvm ls` で確認して、使っていないものを止める ([setup.md](setup.md#前提の確認とクォータ)) |
| `mvm peek` で bootstrap が `failed` | 手順 5 でシークレットを登録したか (`mvm secret ls`)。`mvm shell test-01` で入って `cat ~agent/.pmva/bootstrap.log` |
| `mvm ssh` で「SSH 公開鍵なしで起動されています」 | 手順 5 で SSH 鍵を作ったか。作った後に `mvm renew test-01` で起動し直す (それまでは `mvm shell test-01` で入れる) |
| プロファイル `pmva` に切り替えた後に `mvm image build` や `mvm up` で AccessDenied | `pmva-operator` ポリシーに足りない権限がある (普段の操作はこのポリシーだけで通ることを確認済み。新しい機能で足りないことがある)。`mvm infra deploy` をやり直していなければ管理者のプロファイルでやり直し、それでも出るならエラーに出た操作を [setup.md](setup.md#操作用の-iam-ユーザー) の手順で追加する |

ほかの症状は [troubleshooting.md](troubleshooting.md) を参照してください。
