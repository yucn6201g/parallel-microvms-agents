# よくある質問

## 全体

**Q. 手元の PC の負荷は本当に下がりますか?**
エージェント本体と、エージェントが走らせるビルド・テスト・言語サーバーは、すべて MicroVM の中で動きます。手元で動くのは `mvm` (Python) と ssh、ターミナルの描画だけです。`mvm peek` / `watch` は数十秒に 1 回 HTTPS で画面を取得するだけなので、ほとんど負荷になりません。

**Q. 手元の PC は何が必要ですか? WSL は要りますか?**
Python 3.9 以上、AWS CLI v2、OpenSSH クライアント、Git の 4 つです。Windows でも WSL は要りません ([quickstart.md](quickstart.md#2-手元-pc-にツールを入れる))。

**Q. 費用はどれくらいかかりますか?**
4GB (2 vCPU) の MicroVM 1 台で 1 時間あたり約 $0.25〜0.29 です。2 台を 1 日 8 時間、月 22 日動かすと月 $93〜108 ほどです。止めている間は計算料金がかかりません。LLM の利用料 (サブスクリプションや API) は別です ([cost.md](cost.md))。

**Q. 何台まで同時に動かせますか?**
AWS アカウントの MicroVM メモリのクォータ (`L-CD1C0CC4`) で決まります。PoC のアカウントでは 8GB で、4GB の VM が 2 台 (2GB の VM なら 4 台) でした。`mvm doctor` で何台まで起動できるかを確認できます。超える起動は、1 台も起動する前に止まります。もっと増やしたいときは引き上げを申請します ([setup.md](setup.md#クォータの確認))。

**Q. どのリージョンで使えますか?**
既定は東京 (`ap-northeast-1`) です。ほかのリージョンは、`config.env` の `AWS_REGION` を変えて `mvm doctor` を実行すると、Lambda MicroVMs が使えるかを確かめられます。

## 認証

**Q. Claude の Pro / Max のサブスクリプションで使えますか?**
使えます。手元で `claude setup-token` を実行して発行した長期トークンを `mvm secret set claude-oauth-token` で登録します ([auth.md](auth.md#サブスクリプション-oauth-token))。複数台が同じサブスクリプションを使うと、利用上限を全台で分け合う点に注意してください。

**Q. ChatGPT のアカウントで Codex を使えますか?**
使えます。手元で ChatGPT にログインした Codex の `~/.codex/auth.json` を登録します ([auth.md](auth.md#chatgpt-アカウント))。

**Q. 認証情報はどこに保存されますか? 安全ですか?**
AWS Secrets Manager に保存し、MicroVM の起動時にだけ取り出します。イメージや手元の設定ファイルには残りません。各 MicroVM は、そのセッションで使うエージェントの認証情報だけを読めます。ワークスペースの保存 (スナップショット) にも認証情報は含めません ([architecture.md](architecture.md#セキュリティ))。

**Q. AWS の認証情報は何を使えばいいですか?**
普段は `pmva-operator` ポリシーだけを付けた専用の IAM ユーザーを使います。管理者の権限が要るのは `mvm infra deploy` / `delete` だけです。管理者のキーやルートユーザーのキーを他の環境に渡さないでください ([setup.md](setup.md#操作用の-iam-ユーザー))。

## 動作

**Q. PC を閉じたり、ネットワークが切れたりしたらエージェントは止まりますか?**
止まりません。エージェントは MicroVM の中の tmux で動いています。作業中の VM は AWS 側のキープアライブが起こし続け、作業が終わるとしばらくして一時停止します ([usage.md](usage.md#手元の-pc-を閉じてエージェントに任せる))。

**Q. 8 時間を超えるとどうなりますか?**
MicroVM は最大 8 時間で終了します。その前に `mvm renew <name>` で、保存 → 新しい VM で復元をしてください。ブランチと会話の履歴も引き継ぎます ([usage.md](usage.md#8-時間を超えて続ける))。

**Q. VM の中のファイルは、終了したら消えますか?**
VM のディスクは一時的なもので、終了すると消えます。`~/work` (リポジトリ) と `~/.claude`、`~/.codex` (会話の履歴) は、30 分ごとと `mvm stop` のときに S3 に保存され、`--restore latest` で戻せます。確実に残したい成果物は git に push してください。

**Q. VM の中で sudo は使えますか? パッケージを追加したいです。**
`agent` ユーザーは sudo を使えません (コンテナの制限)。root が必要な作業は `mvm shell <name>` (root で入る) で行います。毎回使うパッケージは `image/Dockerfile` に追加して `mvm image build` します。

**Q. Docker や GPU は使えますか?**
イメージに Docker は入っていません。GPU もありません。CPU (ARM64) で完結するビルドとテストが前提です。

**Q. 対応している CPU はどれですか?**
MicroVM は ARM64 (Graviton) だけです。x86 用のバイナリしかないツールは動きません。

## 接続

**Q. VS Code から使えますか?**
使えます。`mvm ssh-config --install` で `~/.ssh/config` に設定を書き、Remote-SSH で `pmva-<name>` に接続します ([usage.md](usage.md#vs-code-で-microvm-の中を開く))。

**Q. SSH が途中で切れます。**
途中の通信経路 (社内プロキシなど) が長い接続を切ることがあります。`mvm ssh <name>` (コマンドなし) は自動で再接続し、作業は tmux の中で続いています。原因を調べるときは `PMVA_DEBUG=<ファイル>` を設定すると、切断の理由を記録します。

**Q. 社内プロキシの内側から使えますか?**
環境変数 `HTTPS_PROXY` が設定されていれば、`mvm` は CONNECT で MicroVM に接続します。AWS CLI もプロキシの設定に従います。

## トラブル

**Q. 片付けたのにリソースが残っています。**
`mvm infra delete` は、消せなかったリソースがあると、それだけを残して残りを削除し、削除コマンドを表示します。表示されたコマンドを実行してください。イメージは `mvm image delete`、シークレットは Secrets Manager で個別に削除します ([setup.md](setup.md#8-日々の運用))。

ほかの症状は [setup.md のトラブルシューティング](setup.md#トラブルシューティング) と [quickstart.md](quickstart.md#うまくいかないとき) を参照してください。
