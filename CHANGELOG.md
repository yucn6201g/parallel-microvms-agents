# 変更履歴

利用者から見える変更をバージョンごとに記録します。書き方は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)、番号の付け方は [セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

## 未リリース

### 追加

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
- `mvm forward <name> <port>`: VM の中のポート (開発サーバーなど) を手元の PC のポートにつなぐ
- CI の `image` ワークフロー: 本物のイメージを ARM64 でビルドし、ツール、headless の Chromium、起動時の初期化を確かめる
- CI の `agents-e2e`: 固定した版の Claude Code / Codex を LLM API の代わりにつないで動かし、フックと会話の記録の読み取りを確かめる

### 修正

- `mvm follow`: Codex がリポジトリの `AGENTS.md` を読み込んだとき、その中身を利用者の指示として表示していた

### 変更

- Claude Code の hooks と Codex の `notify` を、通知 (`--notify`) を使わないときも常に設定するようにした (状態の記録のため)。`~/.claude/settings.json` に利用者が書いたフックは残す。新しいイメージ (`mvm image build`) が必要
- 同時に動かす台数の目標を 10 台から「AWS アカウントのクォータの範囲内」に変更。`MAX_AGENTS` の既定を `0` (クォータで決まる台数まで) にし、`mvm doctor` はクォータに対して何台動かせるかを表示する。コストの試算 (`mvm cost`) の既定も 2 台にした

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
