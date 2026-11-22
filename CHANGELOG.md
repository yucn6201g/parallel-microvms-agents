# 変更履歴

利用者から見える変更をバージョンごとに記録します。書き方は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)、番号の付け方は [セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

## 未リリース

### 追加

- `mvm image prune [--keep N] [--yes]`: イメージの古いバージョンだけを削除する。`build` のたびに増える版は自動では消えず、1 版 (約 2.3GB) ごとに約 $0.22/月の保存料金がかかっていた。既定では最新の ACTIVE 版を含めて 2 版を残し、ビルド中の版と起動中の VM が使っている版は消さない
- 最小構成の CloudFormation テンプレート `infra/cloudformation-minimal.yaml`。`config.env` の `INFRA_TEMPLATE=minimal` で、キープアライブ・毎晩の自動停止・予算アラートを作らずに使える
- `mvm wait`: エージェントの応答が終わるか、許可・入力待ちになるまで待つ (`--timeout`、`--any`、`--json`)。エージェントの状態 (作業中・完了・入力待ち・終了など) は、VM の中でエージェントのフックが記録する
- `mvm result`: エージェントの最後の応答の文章を表示する
- `mvm ls` / `mvm peek` の `--json`。`peek` と `watch` にエージェントの状態を表示
- `mvm follow`: エージェントの推論の過程 (指示・思考・ツールの実行と結果・応答) を、会話の記録から流れるように表示する。複数台をまとめて表示でき、`--json` で 1 行に 1 件の JSON も出せる
- `mvm view`: エージェントの画面を読み取り専用で表示する (入力はエージェントに届かない)
- `mvm top`: VM のメモリの使用量と、多く使っているプロセスを表示する。メモリ不足でプロセスが強制終了されたら `top` / `peek` に表示し、通知も送る
- `mvm orchestrator install claude|codex`: 手元の Claude Code / Codex を司令塔にする。MicroVM に作業を任せる skill (`mvm-orchestrator`)、確認用の `mvm` コマンドの許可、`--dir` ではそのフォルダで重い処理 (npm / pytest / ビルド / ブラウザ) を止めるフックを入れる ([docs/orchestrator.md](docs/orchestrator.md))
- リポジトリの `.pmva/setup.sh` を VM の起動時 (エージェントを起動する前) に実行する。依存関係のインストールやブラウザの用意に使う。`SETUP_TIMEOUT_SECONDS`、`mvm up --no-setup`
- イメージに Chromium (Playwright / Puppeteer) の実行に必要なライブラリと日本語フォント (`IMAGE_BROWSER_DEPS`)、uv、`pmva-node` (リポジトリが求める版の Node.js を入れる) を追加。`IMAGE_EXTRA_PACKAGES` で OS のパッケージを追加できる
- `mvm profile push` / `show`: 手元の Claude Code / Codex のルール (`CLAUDE.md` / `AGENTS.md`)、skill、サブエージェント、コマンドを VM に渡す。認証情報らしい文字列を含むファイルや設定ファイルは送らない。`config.env` の `PROFILE`、`mvm up --profile` / `--no-profile`。実行ロールに `profiles/*` の読み取りを追加 (`mvm infra deploy` が必要)
- AWS で使える認証方式を追加 ([docs/auth.md](docs/auth.md))。実行ロールの方式はキーの登録が要らない
  - Claude Code: `--claude-auth bedrock-api-key` (Bedrock の API キー)、`mantle` (Bedrock の Mantle)、`anthropic-aws` / `anthropic-aws-api-key` (Claude Platform on AWS。`ANTHROPIC_AWS_WORKSPACE_ID`、`ANTHROPIC_AWS_REGION`)
  - Codex: `--codex-auth bedrock` (Bedrock の OpenAI のモデル)、`bedrock-api-key`
  - 実行ロールに Bedrock の Mantle の呼び出し権限を追加し、Codex 用の実行ロールにも Bedrock の権限を付けた。Claude Platform on AWS の権限は `ENABLE_ANTHROPIC_AWS=true` のときだけ付ける (どちらも `mvm infra deploy` が必要)
- `mvm login <name> [claude|codex]`: VM の中のエージェントに、手元のブラウザでログインする。Codex は手元の 1455 番ポートを VM につないで `codex login` を動かし、ログインの URL を手元のブラウザで開く (`--device` でコードを入力する方式)。Claude Code は `claude auth login` (`--console`)
- Codex の認証方式 `--codex-auth access-token` (Codex のアクセストークン) と `gateway` (Responses API と同じ形の接続先。キーを Bearer か任意のヘッダーで送る、URL のパラメータ、追加のヘッダー)
- 認証方式 `--claude-auth custom` / `--codex-auth custom`: シークレットに書いた環境変数、ファイル (`~/.pmva/secrets/` に権限 600)、エージェントの設定 (`~/.claude/settings.json` に足す設定、`~/.codex/config.toml` の接続先) をそのまま入れる。一覧にない方式や、エージェントの新しい版で増えた方式に使う
- `mvm doctor` が、AWS の構成 (スタック) が手元のテンプレートと `config.env` のとおりか、イメージの元が最後のビルドから変わっていないか、既定の認証方式のシークレットと設定がそろっているかを確かめ、`mvm infra deploy` / `mvm image build` のやり直し忘れや登録漏れを知らせる。操作用ポリシーに `cloudformation:GetTemplate` を追加 (`mvm infra deploy` が必要)
- `mvm` をエージェント (Codex など) のサンドボックスの中で動かして、状態の保存先に書けない・VM に接続できないときは、足すべき設定 (`writable_roots`、`network_access`) を案内する
- `mvm forward <name> <port>`: VM の中のポート (開発サーバーなど) を手元の PC のポートにつなぐ
- CI の `image` ワークフロー: 本物のイメージを ARM64 でビルドし、ツール、headless の Chromium、起動時の初期化を確かめる
- CI の `agents-e2e`: 固定した版の Claude Code / Codex を LLM API の代わりにつないで動かし、フックと会話の記録の読み取りを確かめる

### 修正

- Windows Terminal や Claude のデスクトップアプリの端末から `mvm ssh` / `mvm view` でアタッチすると、シェルのプロンプトに `61;4;6;7;...c0;10;1c` のような文字列が入ることがあった (Enter を押すとコマンドとして実行される)。これらの端末 (ConPTY) は、代替画面に切り替えた直後の問い合わせに応答を 2 回返すことがあり、tmux が 2 回目をキー入力として扱っていた。tmux が手元の端末で代替画面を使わないようにした。そのため、Ctrl-b → d で抜けた後は、元の画面に戻らず tmux の最後の画面が残る (`mvm image build` で作り直すと直る)
- `mvm stop` / `stop-all` / `renew` と毎晩の自動停止で、終了のたびにワークスペースを 2 回保存していた (保存の数秒後に、VM の終了時のフックがもう一度保存していた)。保存の履歴と終了にかかる時間が倍になっていた。保存できたら終了時のフックでは保存しない。保存に失敗したときは、これまでどおり終了時のフックでもう一度試みる。古いイメージの VM では 2 回保存のまま (`mvm image build` で作り直すと直る。毎晩の自動停止は `mvm infra deploy` も必要)
- `mvm stop --no-save` / `stop-all --no-save` でも、VM の終了時のフックがワークスペースを保存し、`workspaces/<名前>/latest.tar.gz` を上書きしていた (`--restore latest` で前の保存に戻れなかった)。終了の前に VM に伝え、終了時のフックでも保存しない。古いイメージの VM では伝えられないので、注意を表示する (`mvm image build` で作り直すと直る)
- Claude Code が認証の失敗や利用上限などの API のエラーで止まっても、`mvm wait` が `waiting`、`mvm result` が「Claude is waiting for your input」と報告し、司令塔が失敗に気づけなかった。Claude Code の `StopFailure` フックで新しい状態 `error` を記録し、`mvm wait` / `mvm result` がエラーの種類と文章、次にやることを表示する (`mvm result` は終了コード 1)。通知 (`--notify`) も送る
- `mvm secret set`: 端末の折り返しごとコピーしたトークンを登録すると、途中に空白や改行が入って認証が 401 で失敗していた。トークンの種類 (JSON・URL 以外) は途中の空白や改行を取り除いて登録し、VM の起動時にも取り除く (登録済みの値も、イメージを作り直せば使える)。`claude-oauth-token` が `sk-ant-oat01-` で始まらないときは注意を表示する
- `mvm follow`: Codex がリポジトリの `AGENTS.md` を読み込んだとき、その中身を利用者の指示として表示していた
- Windows で `mvm ssh` / `view` / `forward` / `login` を使うと、カレントフォルダに `nul` という名前のファイル (VM のホスト鍵) ができていた。ホスト鍵は `.state/known_hosts/<name>` に書き、VM を起動するたびに消す。`mvm ssh-config --install` で書いた設定も同じなので、入れ直す。前の版で作られた `nul` は消してよい (Git Bash なら `rm nul`)
- `mvm ssh <name> --no-reconnect` (名前の後ろにオプションを書く、ドキュメントの書き方) で、`--no-reconnect` が ssh にコマンドとして渡り、接続できなかった
- `mvm doctor`: `mvm infra deploy` の直後でも「AWS の構成が、手元のテンプレートと違います」と出ていた。CloudFormation がテンプレートの日本語を `?` に置き換えて保存するため。比べる前に手元の側もそろえるようにした
- `printf '...' | mvm shell <name>` のように標準入力をパイプやファイルにすると、コマンドは VM で動いても結果が表示されずに終わっていた。読み終えたら Ctrl-D でシェルを終わらせ、VM 側が閉じるまで出力を表示するようにした (`exit` を書かなくても終わる)
- `mvm renew`: SSH 公開鍵なしで起動した後に鍵を作っても、鍵付きにならなかった (`mvm ssh` は「鍵を用意して `mvm renew`」と案内していた)。renew のときに `SSH_PUBLIC_KEY_FILE` を読み直す

### 変更

- AWS から VM の状態を取れなかったとき (`UNKNOWN`)、`mvm ls` / `watch` / `wait` / `top` / `stop` / `renew` / `infra delete` に、理由 (認証切れ、認証情報が無い、権限が無い、接続できない など) とやるべきこと (`aws sso login`、`AWS_PROFILE` の確認、`mvm doctor`) を表示する。`ls --json` は `stateError`、`wait --json` は `error` に入れる。`infra delete` は、状態を取れないセッションを「起動中」と言わずに理由を出して止める
- Claude Code の hooks と Codex の `notify` を、通知 (`--notify`) を使わないときも常に設定するようにした (状態の記録のため)。`~/.claude/settings.json` に利用者が書いたフックは残す。新しいイメージ (`mvm image build`) が必要
- 同時に動かす台数の目標を 10 台から「AWS アカウントのクォータの範囲内」に変更。`MAX_AGENTS` の既定を `0` (クォータで決まる台数まで) にし、`mvm doctor` はクォータに対して何台動かせるかを表示する。コストの試算 (`mvm cost`) の既定も 2 台にした
- `mvm cost` (試算) の単価を、東京リージョンの 2026 年 11 月の実際の請求に合わせた。low / high の 2 行をやめて 1 行 (`tokyo`) にし、スナップショットの読み込みと書き込みを別の単価 (`--snapshot-read-gb` / `--snapshot-write-gb`) で計算する。日本の消費税 10% を含めた税込の額も表示する (`--tax-rate 0` で除外)。`docs/cost.md` に実費との比較を追加
- MicroVM イメージの Claude Code を 2.1.312 から 2.1.330 に、Codex を 0.165.0 から 0.171.0 に更新。新しいイメージ (`mvm image build`) が必要で、起動中の VM は `mvm renew` で移れる

## [0.1.0] - 2026-10-29

最初のリリースです。

### 追加

- **手元 CLI `mvm`** (Windows / macOS / Linux。Python 標準ライブラリのみ)
  - 起動: `up`、`fleet` (タスクファイルで台ごとに別の指示)
  - 確認・指示: `ls`、`peek`、`send`、`watch`、`logs`
  - 接続: `ssh` (SSH over WebSocket、切れたら自動で再接続)、`ssh-config` (VS Code Remote-SSH 用)、`shell`、`token`
  - 保存・停止: `save`、`suspend` / `resume`、`stop`、`stop-all`、`renew` (8 時間の寿命を越えて続ける)
  - 準備: `doctor` (前提ツール、クォータ、管理者権限の認証情報の警告)、`infra`、`secret`、`image`
  - 費用: `cost` (試算)、`cost --actual` (Cost Explorer の実績)
- **認証方式**
  - Claude Code: サブスクリプション (`claude setup-token`)、API キー、Amazon Bedrock、LLM ゲートウェイ、VM 内でのログイン
  - Codex: API キー、ChatGPT アカウント (`auth.json`)、デバイスコードでのログイン
- **MicroVM イメージ** (ARM64 / Amazon Linux 2023): Claude Code 2.1.312 と Codex 0.165.0 を固定。`mvm image versions` / `update-versions` で更新
- **AWS の構成** (CloudFormation)
  - S3 (ワークスペースの保存)、エージェントごとに読める認証情報を分けた実行ロール、操作用ポリシー `pmva-operator`
  - キープアライブ: 作業中の VM だけを起こし続け、PC を閉じても作業を続ける
  - 毎晩の自動停止 (任意)、予算アラート (任意)
- **通知**: Claude Code / Codex の応答の完了・入力待ちを Slack などの Webhook に送る
- **セットアップ**: `scripts/install.sh` (macOS / Linux)、`scripts/install.ps1` (Windows)
- **ドキュメント**: 最短手順、使い方ガイド、コマンド・設定リファレンス、認証、構築と運用、FAQ、費用、設計、検証結果、開発者向けガイド
- **CI**: Windows / macOS (Python 3.9・3.13)、Linux (3.13) のテストと lint。Claude Code / Codex の新しい版を毎週確認して Issue で知らせるワークフロー

[0.1.0]: https://github.com/yucn6201g/parallel-microvms-agents/releases/tag/v0.1.0
