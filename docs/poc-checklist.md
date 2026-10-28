# PoC チェックリスト

Lambda MicroVMs は提供開始から日が浅く、この資材は API 仕様 (AWS の API モデルとエージェント向けドキュメント) をもとに作っています。実機で未検証の点を、**1 台で上から順に**確認してから並列化してください。
結果は下の「結果」欄に記入して、このリポジトリにコミットしておくと後で役立ちます。

## A. 環境

- [ ] `bin/mvm doctor` がすべて OK になる (AWS CLI のバージョン、東京リージョン対応、ベースイメージ ARN)
- [ ] Service Quotas で、MicroVM の同時実行数とメモリ合計の上限が 10 台分 (4GB × 10 = 40GB) 以上ある。足りなければ引き上げを申請する
- [ ] `bin/mvm infra deploy` が成功し、操作者に `pmva-operator` ポリシーを付けた

## B. イメージビルド

- [ ] `bin/mvm image build` が SUCCESSFUL になる
  - ベースイメージ `al2023-minimal` に `dnf` があるか (無ければ `microdnf` を使う作りになっている)
  - ビルド中に nodejs.org、npm、github.com (websocat、gh) からダウンロードできるか
- [ ] スナップショットのサイズを記録する (`aws lambda-microvms get-microvm-image-build`)。起動・再開時間の目安になる

## C. 起動と接続

- [ ] `bin/mvm up test-01 --agent none` で起動し、`bin/mvm peek test-01` で `bootstrap=ready` になる
- [ ] `INGRESS_CONNECTORS` の `HTTP_INGRESS` の ARN が正しい。`SHELL_INGRESS` と併用しても 8080 にアクセスできる
  - 失敗する場合は `INGRESS_CONNECTORS="SHELL_INGRESS"` も試す
- [ ] AWS コンソールの MicroVM 詳細画面の Connect でシェルに入れる。入ったときのユーザー (`id`) を記録する
- [ ] `bin/mvm shell test-01` (websocat) で操作できる。raw モードの表示崩れや、ウィンドウサイズ変更の反映を確認する
- [ ] `bin/mvm ssh test-01` で tmux にアタッチできる (SSH over WebSocket)
- [ ] トークンの有効期限 (60 分) を過ぎても、確立済みの SSH 接続が維持されるか
- [ ] VS Code Remote-SSH で接続できる (`bin/mvm ssh-config`)

## D. エージェントと認証

使う予定の方式をすべて確認します。

- [ ] Claude Code: `oauth-token` / `api-key` / `bedrock` / `gateway` / `interactive`
- [ ] Codex: `api-key` / `chatgpt` / `interactive` (`codex login --device-auth`)
- [ ] 起動直後にオンボーディングや「このフォルダを信頼しますか？」の確認で止まらない
- [ ] `bin/mvm up ... --prompt "..."` の初期指示が実行される
- [ ] `bin/mvm send` で送った指示がエージェントに届く (Enter が送信として扱われる)
- [ ] `sudo dnf install -y <pkg>` が VM 内で使える (制限された Linux capabilities の下で sudo が動くか)
- [ ] GitHub への clone / push、`gh pr create` ができる

## E. ライフサイクル

- [ ] 何も接続しない状態で `IDLE_MAX_SECONDS` 後にサスペンドされる
- [ ] `mvm ssh` で接続中は (ServerAlive の通信があるので) サスペンドされない
- [ ] サスペンド → 自動レジューム後に tmux とエージェントのプロセスが残っており、作業を続けられる (API への再接続)
- [ ] `bin/mvm save` で S3 に保存され、`bin/mvm renew` で別の VM に復元される
- [ ] terminate フック (60 秒以内) で保存が完了するか (ワークスペースの大きさ次第)
- [ ] `--idle off` で 8 時間後に終了する

## F. 並列化

- [ ] `bin/mvm fleet 10` で 10 台が起動する (レート制限・クォータに当たらないか)
- [ ] `bin/mvm watch` で全台を確認できる。手元 PC の CPU 使用率が下がったことを確認する
- [ ] 1 日運用した後、Cost Explorer の実費と [cost.md](cost.md) の試算を比べる

## 結果

| 項目 | 結果 / メモ |
|---|---|
| 実行日 / AWS CLI バージョン | |
| ベースイメージ ARN | |
| スナップショットサイズ (メモリ / ディスク) | |
| 起動時間 (run → ready) | |
| シェル接続時のユーザー | |
| SSH 接続 | |
| 1 日あたりの実費 | |
