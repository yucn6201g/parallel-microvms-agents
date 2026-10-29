# 変更履歴

利用者から見える変更をバージョンごとに記録します。書き方は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)、番号の付け方は [セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

## 未リリース

### 追加

- 最小構成の CloudFormation テンプレート `infra/cloudformation-minimal.yaml`。`config.env` の `INFRA_TEMPLATE=minimal` で、キープアライブ・毎晩の自動停止・予算アラートを作らずに使える

### 変更

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
