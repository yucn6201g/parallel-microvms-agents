# エージェント向けの作業ルール

このリポジトリを Claude Code / Codex などのコーディングエージェントで変更するときの決まりごとです (Claude Code は `CLAUDE.md` からこのファイルを読み込みます)。
背景と詳しい説明は [docs/development.md](docs/development.md) と [docs/architecture.md](docs/architecture.md) にあります。

## このリポジトリについて

Claude Code / Codex を AWS Lambda MicroVMs 上で並列に動かし、手元の PC (Windows / macOS) は画面の確認と指示だけにするための CLI (`mvm`)、MicroVM のイメージ、AWS の構成一式です。

| 場所 | 動く場所 |
|---|---|
| `bin/mvm.py`、`scripts/` | 手元 PC (Windows / macOS / Linux) |
| `image/` | MicroVM (ARM64、Amazon Linux 2023)。`supervisor.py`、`bootstrap.sh` など |
| `infra/cloudformation.yaml` | AWS (Lambda のコードもテンプレートに直接書いている) |
| `tests/` | 手元 / CI。AWS には一切つながない |

## 必ず守ること

- **`bin/mvm.py` と `scripts/` は Python 標準ライブラリだけで書く。** 外部パッケージ (`requests`、`boto3` など) を足さない。AWS の操作は AWS CLI を呼び出す
- **Python 3.9 で動く書き方にする。** `match` 文や `X | Y` の型表記など、3.10 以降の機能は使わない (macOS 標準の Python が 3.9 のため)
- **Windows でも動かす。** パスは `pathlib`、ファイルの読み書きは `encoding="utf-8"` を明示し、シェル固有の機能 (`fork`、シグナルなど) に頼らない
- **`image/` のスクリプトは改行を LF にし、bash で動かす。** 実行権限が要るファイルを足したら、`bin/mvm.py` の `build_artifact_zip` の `executables` と `image/Dockerfile` の `chmod` にも名前を足す
- **認証情報をコードやログ、コマンドライン引数、コミットに入れない。** `config.env` と `.state/` は git 管理外。テストでは偽の値を使う
- **メッセージ、コメント、ドキュメントは日本語で書く。** エラーメッセージには次に何をすればよいかを書く
- **設定項目を足したら** `bin/mvm.py` の `DEFAULTS`、`config.example.env`、`docs/config.md` の 3 か所をそろえる
- **コマンドやオプションを変えたら** `docs/commands.md` (と必要なら `docs/quickstart.md`、`docs/usage.md`) も直す

## 変更した後に必ず実行すること

```bash
python -m unittest discover -s tests -v
shellcheck -S warning bin/mvm scripts/install.sh image/bootstrap.sh image/snapshot.sh image/notify.sh image/agent-attach
cfn-lint infra/cloudformation.yaml
python -m py_compile bin/mvm.py image/supervisor.py scripts/estimate_cost.py tests/*.py
```

- `tests/test_supervisor.py` は Linux で `tmux` と `jq` があるときだけ動く
- 不具合を直すときは、先にそれを再現するテストを書き、失敗することを確かめてから直す
- AWS の応答はスタブ (`tests/aws_stub.py`) で作る。新しい AWS CLI の呼び出しを足したら、スタブにも応答を足す

## してはいけないこと

- テストを消す・飛ばす・弱めることで CI を通す
- AWS のリソースを作る・消すコマンドを、人の指示なしに実行する (`mvm infra deploy` / `delete`、`mvm up`、`aws ... delete-*` など)。費用がかかり、元に戻せないことがある
- 管理者権限の AWS 認証情報を求める、またはチャットに貼らせる
- `image/Dockerfile` の Claude Code / Codex の版を、`mvm image update-versions` を使わずに書き換える

## 変更の出し方

- 変更は main 以外のブランチで行い、プルリクエストにする。CI (Windows / macOS / Linux のテストと lint) がすべて通ってからマージする
- 利用者に見える変更は `CHANGELOG.md` の「未リリース」に書く
- MicroVM の中の動き (`image/`) や AWS の構成 (`infra/`) を変えたときは、自動テストに加えて実機での確認が必要なことをプルリクエストに書く
