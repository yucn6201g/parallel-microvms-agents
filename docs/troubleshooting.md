# うまくいかないとき

最初のセットアップでつまずいたときは、[quickstart.md の「うまくいかないとき」](quickstart.md#うまくいかないとき) も見てください。

## 調べ方

| 見るもの | 方法 |
|---|---|
| 前提と設定の漏れ | `mvm doctor`。AWS の構成やイメージのやり直し忘れ、シークレットの登録漏れも `!!` で知らせる |
| VM の初期化の状態 | `mvm peek <name>` の `bootstrap=` (`ready` / `setup` / `failed: ...`) |
| VM の中のログ | `mvm shell <name>` で root として入り、`cat ~agent/.pmva/bootstrap.log` (リポジトリの準備は `~agent/.pmva/setup.log`) |
| MicroVM のログ | `mvm logs --follow` (CloudWatch Logs) |
| 接続が切れる理由 | 環境変数 `PMVA_DEBUG=<ファイル>` を設定して実行すると、切断の理由などを追記する |

`mvm shell` で入った後は、`agent-attach` でエージェントの tmux に接続できます。

## 起動と準備

| 症状 | 確認すること |
|---|---|
| `mvm image build` が FAILED | `mvm logs` を見る。`CONTAINER_BUILD_FAILED` なら Dockerfile (や `IMAGE_EXTRA_PACKAGES`) の問題。原因を絞りたいときだけ、手元で `docker build --platform linux/arm64 image/` を試す (**デバッグ用・任意**。docker が無くても `mvm` は使えます。イメージのビルドは AWS 側で行います) |
| `mvm up` で「メモリ上限を超えます」 | `mvm doctor` でクォータと起動できる台数を確かめる。使っていない VM を止めるか、表示される「`MEMORY_MIB=2048` にした場合の台数」と申請文で、[引き上げを申請](setup.md#前提の確認とクォータ)する。AWS が `ServiceQuotaExceededException` で拒否したときは、別の PC や別のイメージの VM も数に入るので `aws lambda-microvms list-microvms` で確かめる |
| `mvm up` で AccessDenied | 操作者に `pmva-operator` ポリシーが付いているか (`lambda:PassNetworkConnector`、`iam:PassRole` が要る)。足りない操作は [setup.md](setup.md#操作用の-iam-ユーザー) の手順で足す |
| bootstrap が `failed` | シークレットを登録したか (`mvm secret ls`)、名前が既定と違うなら `--claude-secret` などで指定したか。ログは `bootstrap.log` |
| `ready (warnings: setup.sh failed ...)` | リポジトリの `.pmva/setup.sh` が失敗した。エージェントは動いているので、「~/.pmva/setup.log を見て直して」と頼める |
| `infra deploy` で `pmva-keepalive` / `pmva-nightly-stop` の作成が AccessDenied | 実行した認証情報の権限 (`lambda:CreateFunction` など) と、AWS Organizations の SCP |
| `infra deploy` が「前回の削除で残った Lambda 関数があります」で止まる | 前回の `infra delete` で消せなかった関数がある。表示された `aws lambda delete-function ...` を実行する |

## 接続と状態

| 症状 | 確認すること |
|---|---|
| `mvm peek` が 502 | 起動直後なら約 10 秒待つ。続くなら `mvm shell` で入り、`bootstrap.log` を見る |
| `mvm peek` が 401 / 403 | 手元のトークンの期限切れは、自動で取り直して 1 回だけやり直す。それでも出るなら、AWS の認証 (`mvm doctor`) と、`mvm ls` でセッションが動いているかを確かめる。キャッシュ (`.state/tokens/<name>.*`) は手で消してもよい |
| `peek` / `send` / `result` / `ssh` / `shell` / `top` などが「<name> は終了しています」「サスペンド中です」「起動の途中です」 | VM が操作を受け付けない状態。案内のとおり、終了していれば `mvm up <name>` (保存した時点の続きから始まる)、サスペンド中なら `mvm resume <name>`、起動中なら `mvm wait <name>` |
| `mvm send` が「終了した状態なので送りません」 | エージェントが終了してシェルに戻っている (`exited`)。送ると文字がシェルに入る。`mvm renew <name>` で作り直す (シェルに送るなら `--window shell`) |
| `mvm result` が「まだ作業中です」(終了コード 2) | エージェントが作業中。`mvm wait <name>` で終わるまで待つ |
| エージェントが認証エラー (`authentication_failed`) で止まった | `mvm secret set <種類>` で登録し直し、`mvm renew <name>` で作り直す (保存してから起動し直す)。`--claude-auth interactive` で起動したセッションは作り直すとログインが切れるので、続けて `mvm login <name>` |
| `mvm ssh` で「SSH 公開鍵なしで起動されています」 | `ssh-keygen -t ed25519` で鍵を作り、`mvm renew <name>` で起動し直す (それまでは `mvm shell` で入れる) |
| SSH が `Broken pipe` で切れる | 途中の通信経路 (社内プロキシなど) が長い接続を切ることがある。`mvm ssh <name>` (コマンドなし) は自動で再接続し、作業は tmux の中で続いている |
| VS Code の初回接続が `Connecting with SSH timed out` | もう一度開き直す。`mvm ssh-config --install` を最後に実行した後で VM を作り直したなら、もう一度実行する |
| 日本語が `_` に化ける | 手元のターミナルの文字コードが UTF-8 になっているか |
| エージェントが止まっている | 一時停止していた可能性がある。通信すると再開する。作業中なのに止まるなら、キープアライブ (`KEEPALIVE_RATE_MINUTES`) が無効になっていないか、`INFRA_TEMPLATE=minimal` ではないか |
| `mvm ls` の状態が `UNKNOWN` | AWS から状態を取れなかった (認証切れ、権限、通信)。表示された理由に従う。SSO なら `aws sso login --profile <プロファイル>` |
| `mvm wait` が `stalled` で返る | 画面も CPU も `--idle` の秒数動いていない。`mvm peek` で画面を見る。Codex の承認待ちはこの形で返る |

## 保存と片付け

| 症状 | 確認すること |
|---|---|
| `mvm renew` が「保存に失敗したので、終了せずに止めました」 | 作業を失わないよう VM を残している。`mvm ssh` で入って原因 (ディスク不足など) を調べる。保存せずに作り直すなら `mvm stop <name> --no-save` → `mvm up <name> --restore latest` (前回の保存に戻る) |
| `infra delete` で「削除できなかったため、スタックから外して残しました」 | 権限の不足などで消せなかったリソースだけを残し、残りは削除している。原因を解消してから、表示された削除コマンドを実行する |
| 片付けたのにリソースが残っている | イメージは `mvm image delete`、シークレットは Secrets Manager のコンソールで個別に削除する |
