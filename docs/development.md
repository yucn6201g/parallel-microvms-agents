# 開発者向けガイド

このリポジトリ自体を変更する人向けのページです。構成、テストの動かし方、変更するときの注意をまとめます。設計の背景は [architecture.md](architecture.md) を参照してください。

## 構成

| 場所 | 動く場所 | 役割 |
|---|---|---|
| `bin/mvm.py` | 手元 PC (Windows / macOS / Linux) | CLI 本体。AWS CLI を呼び出し、WebSocket クライアントと SSH の中継を内蔵する |
| `bin/mvm`、`bin/mvm.cmd` | 手元 PC | 起動スクリプト (macOS / Linux 用、Windows 用) |
| `scripts/install.sh`、`scripts/install.ps1` | 手元 PC | セットアップ (ツールの確認、`config.env`、エイリアス) |
| `scripts/estimate_cost.py` | 手元 PC | `mvm cost` の試算 |
| `image/Dockerfile` | AWS (イメージのビルド) | MicroVM のイメージ。Claude Code / Codex の版はここで固定する |
| `image/supervisor.py` | MicroVM | ライフサイクルフック (:9000)、状態・操作 API (:8080)、作業の判定 |
| `image/bootstrap.sh` | MicroVM | 起動直後の初期化 (認証情報、clone、SSH、tmux) |
| `image/snapshot.sh`、`image/notify.sh`、`image/agent-attach` | MicroVM | 保存、通知、tmux への接続 |
| `infra/cloudformation.yaml` | AWS | S3、IAM ロール、操作用ポリシー、キープアライブと毎晩の停止の Lambda (コードはテンプレートに直接書いている)、予算アラート |
| `tests/` | 手元 / CI | 自動テスト |
| `.github/workflows/` | GitHub Actions | CI (`ci.yml`)、版の確認 (`agent-versions.yml`) |

## 守っている方針

- **手元の CLI は Python 標準ライブラリだけで書く**: Windows / macOS で、Python と AWS CLI を入れるだけで動くようにするため。外部パッケージを足さない
- **Python 3.9 で動くようにする**: macOS 標準の Python が 3.9 のため。3.10 以降の構文 (`match`、`X | Y` の型表記など) は使わない
- **VM 内のスクリプトは改行を LF にする**: `.gitattributes` で固定しているが、Windows で新しいファイルを作るときは注意する。`mvm image build` も zip にするときに LF へ変換する
- **認証情報をイメージ、ログ、コマンドライン引数、スナップショットに入れない**
- **メッセージとドキュメントは日本語**: エラーメッセージには、次に何をすればよいかを書く

## テスト

```bash
python -m unittest discover -s tests -v
```

| ファイル | 内容 | 動く OS |
|---|---|---|
| `tests/test_mvm.py` | CLI の各コマンド、WebSocket クライアント (TLS、プロキシ、送受信の詰まり)、インストールスクリプト | すべて (PowerShell が無ければ `install.ps1` のテストは飛ばす) |
| `tests/test_supervisor.py` | `supervisor.py` と `bootstrap.sh` などを、パスとポートを差し替えてローカルで動かす結合テスト (認証方式ごとの初期化、送信、作業の判定、通知、保存の中身) | Linux で `tmux` と `jq` があるときだけ |
| `tests/test_keepalive.py` | テンプレートに書いた Lambda (キープアライブ、毎晩の停止) のコードを取り出し、模擬 API で動かす | すべて |
| `tests/aws_stub.py` | AWS CLI の代わり。テストが `PATH` の先頭に置き、呼び出しを `calls.jsonl` に記録して決まった応答を返す | — |

AWS には一切つながりません。AWS の応答を変えたいテストは、環境変数でスタブの動きを切り替えます (例: `PMVA_STUB_QUOTA_GB`、`PMVA_STUB_IAM=admin`、`PMVA_STUB_UNDELETABLE`。一覧は `tests/aws_stub.py` の冒頭とコード)。

Linux で `test_supervisor.py` を動かすには:

```bash
sudo apt-get install -y tmux jq   # Debian / Ubuntu
```

## lint

CI と同じものを手元で実行できます。

```bash
shellcheck -S warning bin/mvm scripts/install.sh image/bootstrap.sh image/snapshot.sh image/notify.sh image/agent-attach
pip install cfn-lint && cfn-lint infra/cloudformation.yaml
python -m py_compile bin/mvm.py image/supervisor.py scripts/estimate_cost.py tests/*.py
```

ワークフローを変えたときは `actionlint` (`pip install actionlint-py`) も通しておくと安心です。

## CI

`.github/workflows/ci.yml` が、プルリクエストと main への push で次を実行します。各ジョブは 15 分で打ち切ります。

- テスト: Windows / macOS (Python 3.9 と 3.13)、Linux (3.13)
- lint: shellcheck、cfn-lint、Python の構文チェック

`.github/workflows/agent-versions.yml` は毎週月曜に Claude Code / Codex の新しい版を確認し、あれば Issue を立てます (Actions タブから手動でも実行できます)。

## 変更するときの注意

**`image/` のファイルを足したとき**: 実行権限が必要なファイルは、`bin/mvm.py` の `build_artifact_zip` の `executables` と `image/Dockerfile` の `chmod` の両方に名前を足します (zip にするときに権限を付けるため)。

**MicroVM 内の動きを変えたとき**: 自動テストでは AWS の実際の動作 (フックの呼ばれ方、サスペンド、プロキシ) までは確かめられません。`mvm image build` で新しいイメージを作り、`mvm up <name> --agent none` などで実際に起動して確認してから main に入れます。確認した項目は [poc-checklist.md](poc-checklist.md) に記録します。

**Lambda のコードを変えたとき**: キープアライブと毎晩の停止の Lambda は `infra/cloudformation.yaml` に直接書いています。CloudFormation の制限で 4096 バイトまでなので、`tests/test_keepalive.py` が大きさも確かめます。

**IAM の権限を変えたとき**: 実行ロールはエージェントの組み合わせごとに 4 つあり、読めるシークレットを名前の接頭辞で分けています。`mvm` 側の `SECRET_NAME_PREFIXES` と、テンプレートの各ロールの `Resource` をそろえてください。操作用ポリシー (`OperatorPolicy`) に足すときは、`mvm` のどのコマンドで使うかをコメントに書きます。

**設定項目を足したとき**: `bin/mvm.py` の `DEFAULTS`、`config.example.env`、[config.md](config.md) の 3 か所に書きます。

## 変更の流れ

1. ブランチを作って変更する
2. テストと lint を手元で通す
3. プルリクエストを作る。CI がすべて通ったら main にマージする
4. MicroVM 内の動きを変えた場合は、マージの前に実機で確認する
