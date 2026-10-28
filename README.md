# parallel-microvms-agents

Claude Code / Codex を **AWS Lambda MicroVMs** 上で最大 10 本並列に動かし、手元の PC は「画面を見る・指示を出す」だけにするための資材一式です。
ビルド・テスト・エージェント本体はすべてクラウド側の MicroVM で実行されるので、手元 PC の CPU 使用率と発熱を抑えられます。

```
 手元 PC (軽い)                               AWS (ap-northeast-1)
 Windows / macOS
┌──────────────────────┐   HTTPS / WSS     ┌───────────────────────────────────────────────┐
│ mvm (Python)         │  (IAM で発行した    │ Lambda MicroVMs  ※1 エージェント = 1 MicroVM  │
│  ├ peek / send / watch│──短命トークン)────▶│ ┌ MicroVM agent-01 ───────────────────────┐   │
│  ├ ssh (tmux に接続)  │                    │ │ supervisor.py  :8080 状態/操作 API        │   │
│  └ shell (AWS 標準)   │                    │ │                :9000 ライフサイクルフック │   │
│ ターミナル / VS Code  │                    │ │ tmux "main" ── claude / codex             │   │
└──────────────────────┘                    │ │ sshd ← websocat :7681 (SSH over WS)       │   │
                                             │ └───────────────────────────────────────────┘   │
                                             │   ... agent-02 〜 agent-10                      │
                                             │ Secrets Manager (認証情報) / S3 (ワークスペース) │
                                             └───────────────────────────────────────────────┘
```

## できること

| 要件 | 実現方法 |
|---|---|
| 最大 10 エージェントの並列実行 | `mvm fleet 10` / `mvm up <name>`。`MAX_AGENTS=10` で上限を制御 |
| 手元 PC から画面確認 | `mvm peek`（画面末尾を取得）、`mvm watch`（全台を一覧表示）、`mvm ssh`（tmux にアタッチ） |
| 手元 PC から指示 | `mvm send <name> "指示"`（接続せずに入力）、または `mvm ssh` で直接操作 |
| 多様な認証方式 | Claude Code: サブスクリプション (OAuth トークン) / API キー / Amazon Bedrock / LLM ゲートウェイ / 対話ログイン<br>Codex: API キー / ChatGPT ログイン (auth.json) / 対話ログイン |
| 8 時間制限への対応 | ワークスペースを S3 に定期保存し、`mvm renew` で新しい MicroVM に復元 |
| コスト試算 | `mvm cost`（[docs/cost.md](docs/cost.md)） |
| 手元 PC は Windows / macOS 両対応 | 手元 CLI は Python 標準ライブラリのみで実装。必要なのは Python 3.9 以上、AWS CLI v2、OpenSSH クライアント (両 OS に標準搭載) だけ |

## クイックスタート

手元 PC に Python 3.9 以上と AWS CLI v2 を入れてから始めます ([docs/setup.md](docs/setup.md#0-手元-pc-の準備-windows--macos))。

`mvm` の呼び出し方は OS によって違います。

| OS | 呼び出し方 | `mvm` だけで呼べるようにする |
|---|---|---|
| Windows (PowerShell) | `.\bin\mvm.cmd` | `Set-Alias mvm "$PWD\bin\mvm.cmd"` |
| macOS | `bin/mvm` | `alias mvm="$PWD/bin/mvm"` |

```powershell
# Windows (PowerShell)
Copy-Item config.example.env config.env
Set-Alias mvm "$PWD\bin\mvm.cmd"
```

```bash
# macOS
cp config.example.env config.env
alias mvm="$PWD/bin/mvm"
```

ここから先は両 OS 共通です。

```bash
mvm doctor                        # 前提ツール・AWS 認証・リージョン対応を確認
mvm infra deploy                  # S3 / IAM ロール / ログを作成
claude setup-token                # (サブスクリプションの場合) 長期トークンを発行
mvm secret set claude-oauth-token # 発行したトークンを Secrets Manager に登録
mvm secret set github-token       # clone / push 用の GitHub トークン
mvm image build                   # Claude Code / Codex 入りのイメージをビルド
mvm up test-01 --repo https://github.com/<owner>/<repo>.git --prompt "README を読んで概要を説明して"
mvm peek test-01                  # 画面を確認
mvm ssh test-01                   # tmux にアタッチして直接操作 (Ctrl-b d で切断)
```

詳しい手順は [docs/setup.md](docs/setup.md) を参照してください。

## ディレクトリ構成

```
bin/mvm.py                  ローカル操作 CLI 本体 (Python 標準ライブラリのみ。Windows / macOS / Linux 共通)
bin/mvm                     macOS / Linux 用の起動スクリプト
bin/mvm.cmd                 Windows 用の起動スクリプト (PowerShell / コマンドプロンプト)
config.example.env          設定テンプレート
infra/cloudformation.yaml   S3、ビルドロール、実行ロール、ログ、操作用 IAM ポリシー
image/                      MicroVM イメージ (zip 化して CreateMicrovmImage に渡す)
  Dockerfile                AL2023 + Node.js + Claude Code + Codex + tmux + sshd + websocat
  supervisor.py             ライフサイクルフック (:9000) と状態/操作 API (:8080)
  bootstrap.sh              /run 後の初期化 (認証・clone・tmux 起動)
  snapshot.sh               ワークスペースの S3 保存
scripts/estimate_cost.py    コスト試算
docs/
  architecture.md           設計と判断理由
  setup.md                  構築手順と日々の運用
  auth.md                   認証方式ごとの設定方法
  cost.md                   コスト試算
  poc-checklist.md          実機で最初に検証すべき項目
```

## 現状と注意点

- Lambda MicroVMs は 2026 年 6 月に提供開始されたサービスです。API の形式は AWS 公式の API モデル (botocore) と AWS のエージェント向けドキュメントで確認していますが、**この資材はまだ実機の AWS アカウントで動作確認していません**。最初に [docs/poc-checklist.md](docs/poc-checklist.md) の順に 1 台で検証してから並列化してください。
- 手元 CLI は Linux 上で、AWS CLI のスタブとローカルの WebSocket サーバーを使ってテストしています。Windows / macOS の実機での確認項目は [docs/poc-checklist.md](docs/poc-checklist.md) の「C2」にまとめています。
- MicroVM の寿命は最大 8 時間です。成果物は git に push するか、`mvm save` / 定期スナップショットで S3 に残してください。
- 料金単価はローンチ時に報告された値で試算しています。必ず AWS の料金ページで最新の単価を確認してください。
