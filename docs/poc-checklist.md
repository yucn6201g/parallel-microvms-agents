# PoC チェックリスト

Lambda MicroVMs は提供開始から日が浅く、この資材は API 仕様 (AWS の API モデルとエージェント向けドキュメント) をもとに作っています。実機で未検証の点を、**1 台で上から順に**確認してから並列化してください。
結果は下の「結果」欄に記入して、このリポジトリにコミットしておくと後で役立ちます。

## 0. 事前確認済みの項目 (2026-10-28、Claude Code のクラウドセッションで実施)

AWS の認証情報がない状態で、確認できる範囲を先に検証しました。

| 項目 | 結果 |
|---|---|
| AWS API (STS / Lambda / CloudFormation / Secrets Manager / Logs, ap-northeast-1) への到達 | OK |
| AWS CLI が `lambda-microvms` コマンドに対応 (aws-cli 1.46.2 / botocore 1.43.62) | OK。`ListManagedMicrovmImages` まで到達し、認証で拒否された |
| Dockerfile のダウンロード元: Node.js 22.20.0 (arm64) と SHASUMS、websocat 1.13.0 (aarch64-musl) | OK (存在を確認) |
| Claude Code 2.1.312: Node 22 以上が必要。linux-arm64 のネイティブ版あり。`setup-token`、`--dangerously-skip-permissions`、`apiKeyHelper`、`CLAUDE_CODE_OAUTH_TOKEN`、`hasCompletedOnboarding`、`hasTrustDialogAccepted` が存在 | OK |
| Codex 0.165.0: linux-arm64 版あり。`codex login --with-api-key` (標準入力から読む)、`--device-auth`、`--dangerously-bypass-approvals-and-sandbox`、`trust_level` が存在 | OK |
| ARM64 のイメージを手元でビルド | 未確認。ベースイメージのダウンロードと `cdn.amazonlinux.com` がクラウドセッションのネットワーク制限で拒否された (実際のビルドは AWS 側で行うので影響しない) |

### クラウドセッション (Claude Code on the web) から PoC を実行する場合の準備

1. 環境の設定 (タイトルバーのクラウド環境メニュー → Edit) で環境変数を登録する: `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY` (一時認証情報なら `AWS_SESSION_TOKEN` も)
2. 同じ画面の Network access の Allowed domains に `*.lambda-microvm.ap-northeast-1.on.aws` を追加する。`mvm peek` / `send` / `ssh` が MicroVM のエンドポイントに接続するために必要
3. 新しいセッションを開始する (環境の設定は新しいセッションから反映される)
4. AWS CLI は `pip install -U awscli` で入れたものでも `lambda-microvms` に対応している

## A. 環境

- [ ] `mvm doctor` がすべて OK になる (AWS CLI のバージョン、東京リージョン対応、ベースイメージ ARN)
- [ ] Service Quotas で、MicroVM の同時実行数とメモリ合計の上限が 10 台分 (4GB × 10 = 40GB) 以上ある。足りなければ引き上げを申請する
- [ ] `mvm infra deploy` が成功し、操作者に `pmva-operator` ポリシーを付けた

## B. イメージビルド

- [ ] `mvm image build` が SUCCESSFUL になる
  - ベースイメージ `al2023-minimal` に `dnf` があるか (無ければ `microdnf` を使う作りになっている)
  - ビルド中に nodejs.org、npm、github.com (websocat、gh) からダウンロードできるか
- [ ] スナップショットのサイズを記録する (`aws lambda-microvms get-microvm-image-build`)。起動・再開時間の目安になる

## C. 起動と接続

- [ ] `mvm up test-01 --agent none` で起動し、`mvm peek test-01` で `bootstrap=ready` になる
- [ ] `INGRESS_CONNECTORS` の `HTTP_INGRESS` の ARN が正しい。`SHELL_INGRESS` と併用しても 8080 にアクセスできる
  - 失敗する場合は `INGRESS_CONNECTORS="SHELL_INGRESS"` も試す
- [ ] AWS コンソールの MicroVM 詳細画面の Connect でシェルに入れる。入ったときのユーザー (`id`) を記録する
- [ ] `mvm shell test-01` で操作できる。raw モードの表示崩れや、ウィンドウサイズ変更の反映を確認する
- [ ] `mvm ssh test-01` で tmux にアタッチできる (SSH over WebSocket)
- [ ] トークンの有効期限 (60 分) を過ぎても、確立済みの SSH 接続が維持されるか
- [ ] VS Code Remote-SSH で接続できる (`mvm ssh-config --install`)

## C2. 手元 PC の OS ごとの確認

Windows と macOS の両方で使う場合は、それぞれで確認します。

| 確認項目 | Windows (PowerShell / Windows Terminal) | macOS (ターミナル / iTerm2) |
|---|---|---|
| `mvm doctor` が OK | [ ] | [ ] |
| `mvm up` / `ls` / `peek` / `send` / `watch` | [ ] | [ ] |
| `mvm ssh` で tmux にアタッチ、日本語の表示・入力 | [ ] | [ ] |
| `mvm shell` (Windows はコンソールの VT モードを使う実装。表示・キー入力・Ctrl-C) | [ ] | [ ] |
| VS Code Remote-SSH (`mvm ssh-config --install`) | [ ] | [ ] |
| 社内プロキシ環境 (`HTTPS_PROXY`) での接続 (該当する場合) | [ ] | [ ] |
| ユーザー名に日本語やスペースを含むパスでの動作 (該当する場合) | [ ] | [ ] |

## D. エージェントと認証

使う予定の方式をすべて確認します。

- [ ] Claude Code: `oauth-token` / `api-key` / `bedrock` / `gateway` / `interactive`
- [ ] Codex: `api-key` / `chatgpt` / `interactive` (`codex login --device-auth`)
- [ ] 起動直後にオンボーディングや「このフォルダを信頼しますか？」の確認で止まらない
- [ ] `mvm up ... --prompt "..."` の初期指示が実行される
- [ ] `mvm send` で送った指示がエージェントに届く (Enter が送信として扱われる)
- [ ] `sudo dnf install -y <pkg>` が VM 内で使える (制限された Linux capabilities の下で sudo が動くか)
- [ ] GitHub への clone / push、`gh pr create` ができる

## E. ライフサイクル

- [ ] 何も接続しない状態で `IDLE_MAX_SECONDS` 後にサスペンドされる
- [ ] `mvm ssh` で接続中は (ServerAlive の通信があるので) サスペンドされない
- [ ] サスペンド → 自動レジューム後に tmux とエージェントのプロセスが残っており、作業を続けられる (API への再接続)
- [ ] `mvm save` で S3 に保存され、`mvm renew` で別の VM に復元される
- [ ] terminate フック (60 秒以内) で保存が完了するか (ワークスペースの大きさ次第)
- [ ] `--idle off` で 8 時間後に終了する

## F. 並列化

- [ ] `mvm fleet 10` で 10 台が起動する (レート制限・クォータに当たらないか)
- [ ] `mvm watch` で全台を確認できる。手元 PC の CPU 使用率が下がったことを確認する
- [ ] 1 日運用した後、Cost Explorer の実費と [cost.md](cost.md) の試算を比べる

## 結果

| 項目 | 結果 / メモ |
|---|---|
| 実行日 / AWS CLI バージョン | |
| 手元 PC の OS / Python バージョン | |
| ベースイメージ ARN | |
| スナップショットサイズ (メモリ / ディスク) | |
| 起動時間 (run → ready) | |
| シェル接続時のユーザー | |
| SSH 接続 | |
| 1 日あたりの実費 | |
