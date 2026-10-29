# はじめての人向けの最短手順

初めての人が、手元の PC (Windows / macOS) から **1 台目のエージェントを動かすまで**の手順を、上から順に並べたページです。
各手順の詳しい説明や選択肢は [setup.md](setup.md) にあります。迷ったときだけ参照してください。

| 手順 | やること | 目安の時間 |
|---|---|---|
| [1](#1-aws-アカウントを守る) | AWS アカウントを守る (MFA と予算) | 10 分 |
| [2](#2-手元-pc-にツールを入れる) | 手元 PC にツールを入れる | 10〜20 分 |
| [3](#3-このリポジトリを取得してセットアップする) | このリポジトリを取得してセットアップする | 5 分 |
| [4](#4-aws-の土台を作る-管理者で-1-回だけ) | AWS の土台を作る (管理者で 1 回だけ) | 10 分 |
| [5](#5-普段使う-iam-ユーザーに切り替える) | 普段使う IAM ユーザーに切り替える | 5 分 |
| [6](#6-エージェントの認証情報を登録する) | エージェントの認証情報を登録する | 5 分 |
| [7](#7-イメージをビルドする) | イメージをビルドする | 10〜20 分 (待つだけ) |
| [8](#8-1-台目を動かす) | 1 台目を動かす | 5 分 |
| [9](#9-使い終わったら止める) | 使い終わったら止める | 1 分 |

**費用の目安**: 4GB の MicroVM 1 台で 1 時間あたり約 $0.25〜0.29 (約 40 円)。止めれば計算料金はかかりません。ほかに保存などの固定費が月 $9 前後です ([cost.md](cost.md))。

以下、コマンドは Windows (PowerShell) と macOS (ターミナル) で書き方が違うところだけ分けて書きます。分けていないものは両方で同じです。

---

## 1. AWS アカウントを守る

AI エージェントに AWS を使わせるので、最初にアカウントの守りを固めます。**この手順を飛ばさないでください** (PoC では、管理者のキーを別の環境で使ったことで AWS の不正利用検知に引っかかり、アカウントが制限されました)。

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

SSH 鍵が無ければ作ります (両 OS 共通。質問はすべて Enter で構いません)。

```bash
ssh-keygen -t ed25519
```

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

## 4. AWS の土台を作る (管理者で 1 回だけ)

ここだけ管理者の権限が要ります。**自分の PC で**行ってください。

1. 管理者のプロファイルを用意する
   - IAM Identity Center (SSO) を使っている場合 (おすすめ): `aws configure sso` を実行し、プロファイル名を `admin` にする
   - 使っていない場合: 管理者の IAM ユーザーのアクセスキーを作り、`aws configure --profile admin` で入力する。**手順 5 が終わったらこのキーは削除する**
2. `config.env` をテキストエディタで開き、最後に `AWS_PROFILE=admin` を書き足す (リージョンは既定で東京 `ap-northeast-1`)
3. 確認してから作成する

```bash
mvm doctor           # 「ok AWS 認証」と「Lambda MicroVMs が利用可能」が出れば OK (管理者の警告 !! はこの段階では想定どおり)
mvm infra deploy     # S3・IAM ロール・ログなどを作る (数分)
```

`mvm doctor` の「同時に何台まで」の行も見ておきます。1 台目を動かすだけなら 1 台以上あれば十分です。10 台並べるにはクォータの引き上げが必要です ([setup.md](setup.md#クォータの確認-並列で使う場合は必須))。

## 5. 普段使う IAM ユーザーに切り替える

普段の操作は、`pmva-operator` ポリシーだけを付けた専用ユーザーで行います。続けて管理者のプロファイルのまま実行します。

```bash
mvm infra outputs                                     # OperatorPolicyArn の行の値をコピーしておく
aws iam create-user --user-name pmva-operator --profile admin
aws iam attach-user-policy --user-name pmva-operator --policy-arn <コピーした OperatorPolicyArn> --profile admin
aws iam create-access-key --user-name pmva-operator --profile admin
aws configure --profile pmva                          # 直前に表示された AccessKeyId と SecretAccessKey を入力。リージョンは ap-northeast-1
```

`config.env` の `AWS_PROFILE=admin` を `AWS_PROFILE=pmva` に書き換え、確認します。

```bash
mvm doctor           # 「権限の確認は省略しました」または「管理者権限ではありません」と出れば切り替え完了
```

手順 4 で管理者のアクセスキーを作った場合は、ここで IAM コンソールから削除します (`mvm infra deploy` をやり直すときは、その都度作って使い終わったら削除します)。

## 6. エージェントの認証情報を登録する

使うものを 1 つ選んで登録します。値は入力しても画面に表示されず、AWS Secrets Manager にだけ保存されます。

| 使いたいもの | 手元で行うこと | 登録コマンド |
|---|---|---|
| Claude Pro / Max (サブスクリプション) | 手元に Claude Code を入れて `claude setup-token` を実行し、表示されたトークンをコピー | `mvm secret set claude-oauth-token` |
| Anthropic API キー | Claude Console でキーを発行 | `mvm secret set anthropic-api-key` |
| Codex (OpenAI API キー) | OpenAI のダッシュボードでキーを発行 | `mvm secret set openai-api-key` |

既定の設定は「Claude Code をサブスクリプションで使う」です。ほかの方式 (Amazon Bedrock、ChatGPT アカウントの Codex など) や切り替え方は [auth.md](auth.md) を参照してください。

GitHub のリポジトリで作業させる場合は、対象リポジトリに絞った Fine-grained personal access token (Contents と Pull requests を Read and write) を発行して登録します。

```bash
mvm secret set github-token
```

GitHub を使わない場合は、`config.env` に `USE_GITHUB_TOKEN=false` を書き足します。

## 7. イメージをビルドする

Claude Code / Codex 入りの実行環境を AWS 上で作ります。初回は 10〜20 分かかるので、その間は待つだけです。

```bash
mvm image build      # 「ビルド成功」と出れば完了
```

## 8. 1 台目を動かす

```bash
mvm up test-01 --repo https://github.com/<owner>/<repo>.git --prompt "README を読んで概要を説明して"
```

GitHub を使わない場合は `--repo` を付けずに `mvm up test-01 --no-github --prompt "..."` とします。10 秒ほどで起動します。

```bash
mvm peek test-01                         # エージェントの画面の最後を表示する (接続せずに確認)
mvm send test-01 "テストを実行して結果を要約して"   # 指示を送る
mvm ssh test-01                          # 画面に入って直接操作する。抜けるときは Ctrl-b を押してから d
```

`mvm ssh` から抜けても、エージェントは AWS 上で作業を続けます。手元の PC を閉じても止まりません。
作業ブランチは `pmva/test-01` です。エージェントに commit と push をさせれば、成果は GitHub に残ります。

## 9. 使い終わったら止める

```bash
mvm stop test-01     # ワークスペースを保存してから終了する (全部まとめて止めるなら mvm stop-all)
```

次回は `mvm up test-01 --restore latest ...` で続きから始められます。
止め忘れが心配なら、`config.env` に `NIGHTLY_STOP_TIME=23:00` を書き足し、管理者のプロファイルで `mvm infra deploy` をやり直すと、毎晩その時刻に全台を保存してから止めます。

使った金額は、翌日以降に次のコマンドで確認できます (Cost Explorer への反映は最大 24 時間ほど遅れます)。

```bash
mvm cost --actual --days 7
```

---

## 次にやること

- 場面別の使い方 (並列実行、PC を閉じて任せる、8 時間を超える、費用を抑える): [usage.md](usage.md)
- 全コマンドと設定項目: [commands.md](commands.md)、[config.md](config.md)
- 何本も並べて動かす: `mvm fleet 3 --repo ...` ([setup.md の 7](setup.md#7-並列実行-最大-10-本))。10 本並べるにはクォータの引き上げが必要
- VS Code から MicroVM の中のファイルを開く: `mvm ssh-config --install` してから Remote-SSH で `pmva-test-01` に接続
- 応答が終わったら Slack などに通知する: [setup.md の通知](setup.md#完了入力待ちの通知)
- 毎日の操作のまとめ: [setup.md の 8](setup.md#8-日々の運用)

## うまくいかないとき

| 症状 | 確認すること |
|---|---|
| `mvm` が見つからない | 手順 3 の後でターミナルを開き直したか。フォルダの中で `bin/mvm` (Windows は `.\bin\mvm.cmd`) なら動くか |
| `mvm doctor` で「AWS 認証情報がありません」 | `config.env` の `AWS_PROFILE` と、`aws configure` で付けたプロファイル名が同じか。SSO なら `aws sso login --profile admin` |
| `mvm doctor` で「lambda-microvms に未対応」 | AWS CLI が古い。最新版に入れ直す |
| `mvm infra deploy` で AccessDenied | 管理者のプロファイル (`AWS_PROFILE=admin`) で実行しているか |
| `mvm up` で「メモリ上限を超えます」 | すでに起動中の VM を `mvm ls` で確認して止める。上限が 0GB なら AWS 側でアカウントが制限されている可能性 ([setup.md](setup.md#トラブルシューティング)) |
| `mvm peek` で bootstrap が `failed` | 手順 6 のシークレットを登録したか (`mvm secret ls`)。`mvm shell test-01` で入って `cat ~agent/.pmva/bootstrap.log` |
| `mvm ssh` で「SSH 公開鍵なしで起動されています」 | 手順 2 の `ssh-keygen` をしたか。作った後に `mvm renew test-01` で起動し直す (それまでは `mvm shell test-01` で入れる) |
| 手順 5 の後に `mvm image build` や `mvm up` で AccessDenied | `pmva-operator` ポリシーに足りない権限がある (このポリシーだけでの動作はまだ実機で確認できていない)。エラーに出た操作を [setup.md](setup.md#操作用の-iam-ユーザー) の手順で追加する |

ほかの症状は [setup.md のトラブルシューティング](setup.md#トラブルシューティング) を参照してください。
