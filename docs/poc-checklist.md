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

1. 環境の設定 (タイトルバーのクラウド環境メニュー → Edit) のネットワークシークレットに **AWS SigV4** として `AWS_ACCESS_KEY_ID`、`AWS_SECRET_ACCESS_KEY` を登録する (一時認証情報なら `AWS_SESSION_TOKEN` も)。キーは作業環境に置かれず、プロキシが署名する (今回の PoC はこの方式で実施)
2. 同じ画面の Network access の Allowed domains に `*.lambda-microvm.ap-northeast-1.on.aws` を追加する。`mvm peek` / `send` / `ssh` が MicroVM のエンドポイントに接続するために必要
3. 新しいセッションを開始する (環境の設定は新しいセッションから反映される)
4. AWS CLI は `pip install -U awscli` で入れたものでも `lambda-microvms` に対応している

## A. 環境

- [x] `mvm doctor` がすべて OK になる (AWS CLI のバージョン、東京リージョン対応、ベースイメージ ARN)
  - ベースイメージ ARN は `arn:aws:lambda:ap-northeast-1:aws:microvm-image:al2023-1` (既定値のままで OK)
- [x] Service Quotas を確認した → **10 台並列には引き上げが必要**
  - `Max allocated ARM_64 MicroVM memory` (L-CD1C0CC4) が **8GB**。4GB の VM は同時に 2 台まで。10 台 × 4GB なら 40GB 以上に引き上げを申請する
  - `Rate of RunMicrovm API requests` (L-535CA9B6) が **1 回/秒** (バースト 1)。`mvm fleet` は 1 台ずつ順に起動し、1 台あたり約 4 秒かかるので通常は当たらない
  - `Max Execution Duration of a MicroVM` は 8 時間、イメージ数 100、イメージあたりのバージョン数 50
- [x] `mvm infra deploy` が成功した (操作者は管理者ユーザーだったので `pmva-operator` のアタッチは未確認)

## B. イメージビルド

- [x] `mvm image build` が SUCCESSFUL になる (4 回失敗して修正した上で、v5 で成功)
  - ベースイメージ `al2023-minimal` で `dnf` は使えた
  - nodejs.org、npm、github.com からダウンロードできた
  - 修正 1: gh の rpm リポジトリが GPG 検証に失敗 → リリースの tar.gz を checksum 付きで入れる方式に変更
  - 修正 2: validate フックで `tmux --version` (誤り。正しくは `-V`) を待って接続が切れた → 検証を裏で実行し、終わるまで 503 を返す方式に変更
  - 修正 3: `/run` フックの本文は `{"microvmId": "...", "runHookPayload": "<文字列>"}` で届く (ペイロードそのものではない) → 中身を取り出すように修正
- [x] スナップショットのサイズを記録した: メモリ約 713MB、イメージ本体約 2.8GB、追加ディスク約 25MB。Graviton 3 と 4 向けの 2 ビルドが作られる

## C. 起動と接続

- [x] `mvm up test-01 --agent none` で起動し、`mvm peek test-01` で `bootstrap=ready` になる (`run-microvm` が約 4 秒、ready まで 9〜10 秒)
- [x] `INGRESS_CONNECTORS` の `HTTP_INGRESS` の ARN は正しい。`SHELL_INGRESS` と併用して 8080 (peek / send) にアクセスできた
- [ ] AWS コンソールの Connect (未確認: クラウドセッションからはコンソールを操作できない)
- [x] `mvm shell test-01` で操作できる。入ったときのユーザーは **root**。接続直後に `{"type":"session_init",...}` という制御メッセージが届く (mvm 側で非表示にした)
  - 端末を割り当てない (標準入力をパイプにした) 状態でのみ確認。raw モードやウィンドウサイズ変更は C2 で手元 PC から確認する
- [x] `mvm ssh test-01` で tmux にアタッチ / デタッチできる (擬似端末を割り当てて確認)
  - 修正 4: `mvm ssh <name>` (コマンドなし) でヘルプが出る不具合 → 修正
  - 修正 5: SSH 経由では tmux が日本語を `_` に置き換えた (ロケールが引き継がれない) → `tmux -u` と `LANG=C.UTF-8` で修正し、v5 で日本語の表示を確認
  - 5MB の転送でハッシュが一致 (約 3.8 秒)
  - VM 内から見えるリソースは 8 vCPU / 16GB / ディスク 16GB (4GB ベースラインの 4 倍までバースト)
- [ ] トークンの有効期限 (60 分) を過ぎても確立済みの SSH 接続が維持されるか: 確かめられなかった。65 分の接続を試したところ、35 分 48 秒で通信相手からリセットされた (`ConnectionResetError: Connection reset by peer`)。このクラウドセッションは外部への通信が中継プロキシを通るため、AWS 側で切られたのか中継で切られたのかは区別できない。`mvm ssh` は切れると再接続し、そのたびに新しいトークンを使うので、実運用では期限は問題にならない
- [ ] VS Code Remote-SSH (未確認: C2 で手元 PC から確認する)

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

今回の PoC では Bedrock と `--agent none` のみ確認しました。

- [x] Claude Code: `bedrock` → VM から Bedrock まで届くことを確認。ただし、このアカウントでは Bedrock の Anthropic モデル利用が有効になっていないため `API Error: 400 Access to Anthropic models is not allowed for this account.` で止まる (手元から直接呼んでも同じエラー)。Bedrock コンソールでモデルの利用申請をすれば使える見込み
  - 推論プロファイルは `jp.anthropic.claude-haiku-5-5` で確認 (Claude Code の表示は「Haiku 5.5 · Amazon Bedrock」)
- [ ] Claude Code の `oauth-token` / `api-key` / `gateway` / `interactive` (未確認)
- [ ] Codex の `api-key` / `chatgpt` / `interactive` (未確認)
- [x] 起動直後にオンボーディングや「このフォルダを信頼しますか？」の確認で止まらない
- [x] `mvm up ... --prompt "..."` の初期指示が入力・実行される
- [x] `mvm send` で送った指示 (日本語を含む) がエージェントに届き、Enter で送信される
- [x] `sudo` は**使えない**: コンテナに no_new_privs が付いている (`sudo: The "no new privileges" flag is set`)。イメージから sudo を外し、root が必要な作業は `mvm shell` (root) で行う方針にした
- [ ] GitHub への clone / push、`gh pr create` (未確認: GitHub トークンを登録していない)

## E. ライフサイクル

- [x] 何も接続しない状態で `IDLE_MAX_SECONDS` 後にサスペンドされる (120 秒の設定で、最後の通信から約 132 秒で SUSPENDED)
- [x] `mvm ssh` で接続中はサスペンドされない (120 秒の設定で 3.5 分間接続し続け、ずっと RUNNING)
- [x] サスペンド → 自動レジュームで、tmux・バックグラウンドのプロセス・`/tmp` のファイルが残っていた。`peek` 1 回でレジュームし、応答まで 1.4 秒
  - エージェントが API を呼んでいる最中のサスペンドからの再接続は未確認 (Bedrock が使えなかったため)
- [x] `mvm save` で S3 に保存 (12.8 秒) され、`mvm renew` で別の VM に復元された (保存 → 終了 → 起動 → 復元まで 19 秒。git の履歴も復元)
- [x] terminate フックで保存される (`--no-save` で止めた VM の保存ファイルが `workspace-history/` に残っていた)
- [ ] `--idle off` で 8 時間後に終了する (確認を取りやめた)

## F. 並列化

2026-10-29 に、クォータ (8GB) の範囲内の 2 台で確認しました。

- [x] `mvm fleet 2` で 2 台が 9 秒で起動し、両方 `bootstrap=ready` になった。レート制限 (RunMicrovm 1 回/秒) には当たらなかった
- [x] 3 台目は mvm の事前チェックで止まる。チェックを通さずに AWS に直接 `run-microvm` すると `ServiceQuotaExceededException: The base maximum allocated memory limit has been reached` で拒否された → 上限は**ベースラインメモリの合計**で決まり、事前チェックの前提と一致
- [x] 2 台に同時に `mvm send` / `mvm ssh` できた
  - 修正 8: `mvm send <name> --window shell ...` のように名前の後に書いたオプションが指示文に混ざっていた → 修正
  - 修正 9: `-` で始まる指示を tmux がオプションとして解釈していた → `send-keys -l --` で修正し、AWS 上で確認
  - 修正 10: `stop-all` 直後の `infra delete` で、終了中の VM が terminate フックで S3 に保存したためバケットが空にならず、スタックの削除に失敗した → VM が終了しきるのを待ち、失敗時はもう一度バケットを空にして再試行するように修正
- [ ] `mvm fleet 10` で 10 台が起動する (未確認: クォータの引き上げが必要)
- [ ] `mvm watch` で全台を確認できる。手元 PC の CPU 使用率が下がったことを確認する (C2 で手元 PC から確認する)
- [ ] 1 日運用した後、Cost Explorer の実費と [cost.md](cost.md) の試算を比べる

## G. 追加機能 (2026-10-29)

- [x] キープアライブ: 定期処理 1 分、作業中とみなす時間 120 秒、アイドル 180 秒の設定で確認
  - 作業中 (画面に 10 秒ごとに出力) の VM には Lambda が毎分 `/healthz` を送り、180 秒を大きく超える 7.5 分間 RUNNING のままだった
  - 作業を止めると、120 秒後から Lambda は何も送らなくなり、その約 3 分後に SUSPENDED になった
  - 修正 11: 作業判定がイメージのスナップショット時点の時刻を引き継いでいて、起動直後の 1 回だけ「作業していない」と判定された → `/run` でリセット (再ビルドしたイメージで、最初の状態が起動時刻になることを確認)
- [x] 完了・入力待ちの通知: Claude Code の hooks と Codex の notify からの送信をローカルのテスト用 Webhook で確認 (自動テスト)。実際のエージェントからの通知は未確認 (Bedrock が使えず、トークンも未登録のため)
  - 修正 12: `~/.codex/config.toml` がまだ無いと初期化が止まる不具合 (テストで発見)
- [x] 予算アラート: テンプレートの検査 (cfn-lint) のみ。メールアドレス未設定のため作成は未確認
- [x] SSH の長時間接続: 出力のない SSH 接続が 41 秒後、4 分後、35 分後に切れることが 3 回あった (同じ条件の 150 秒 × 2 回、120 秒、15 分は切れなかった)。35 分後の切断は `PMVA_DEBUG` の記録で TCP のリセット (`Connection reset by peer`) と分かった。再現性はない
  - 修正 13: WebSocket の読み書きを 1 つのロックで排他 (SSL ソケットはスレッド間で同時に読み書きすると安全でないため。予防)
  - 修正 14: `mvm ssh <name>` (tmux へのアタッチ) は、接続が切れたら自動で再接続する (中継プロセスを強制終了して確認)
  - `PMVA_DEBUG=<ファイル>` で切断理由 (close フレーム、例外) を記録できるようにした
- [ ] 操作用ポリシー (`pmva-operator`) だけでの動作: 未確認。このクラウドセッションでは AWS へのリクエストが管理者の認証情報で署名し直されるため、権限を絞った確認ができなかった。IAM のポリシーシミュレーターも `lambda:PassNetworkConnector` を評価できない (リソースを `*` にしても拒否と出る)。手順は setup.md の「操作用ポリシーの確認」
- [ ] メモリのクォータ引き上げ: Service Quotas の API で申請すると「既定値 (2048) より大きい値を指定してください」と拒否された (現在値 8GB が既定値より低く抑えられているため)。AWS サポートのケースで申請する必要がある (setup.md に手順)

## 結果

| 項目 | 結果 / メモ |
|---|---|
| 実行日 / AWS CLI バージョン | 2026-10-28 / aws-cli 1.46.2 (botocore 1.43.62)。Claude Code のクラウドセッション (Linux) から実行 |
| 手元 PC の OS / Python バージョン | Linux (クラウドセッション) / Python 3.13。Windows / macOS は未確認 (C2) |
| ベースイメージ ARN | `arn:aws:lambda:ap-northeast-1:aws:microvm-image:al2023-1` |
| スナップショットサイズ (メモリ / ディスク) | 約 713MB / 約 25MB (イメージ本体約 2.8GB) |
| 起動時間 (run → ready) | run-microvm 約 4 秒、bootstrap ready まで 9〜10 秒。サスペンドからの自動レジュームは 1.4 秒 |
| シェル接続時のユーザー | root (SSH は agent) |
| SSH 接続 | OK (tmux アタッチ / デタッチ、日本語表示、5MB 転送) |
| 1 日あたりの実費 | 未計測。PoC で VM を動かしたのは 4GB × 延べ約 15 分 (試算では約 $0.07)。Cost Explorer に反映されたら確認する |
| PoC 中に直した不具合 | 10 件: gh のインストール、validate フック、/run の本文形式、mvm ssh の引数、tmux の日本語、mvm shell の制御メッセージと端末復元、mvm logs (AWS CLI v1 対応)、mvm send のオプション、`-` で始まる指示、infra delete の削除順。ほかに `mvm ls` の時刻表示 |
| 本番前に必要な対応 | MicroVM メモリのクォータ引き上げ (8GB → 40GB 以上)、Bedrock を使うならモデル利用申請 |
| 片付け | 2026-10-28・29 とも `mvm stop-all`、`mvm infra delete`、`mvm image delete` を実施し、何も残っていないことを確認 |
| 自動テスト (CI) | GitHub Actions で Windows / macOS (Python 3.9・3.13)、Linux (3.13) のテストと lint が成功。端末の表示や VS Code など目で見る項目は C2 に残る |
