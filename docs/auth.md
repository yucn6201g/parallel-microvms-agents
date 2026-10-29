# 認証方式

エージェントの認証情報は **AWS Secrets Manager** (`${PMVA_PREFIX}/...`) に置きます。MicroVM の起動時に `bootstrap.sh` が実行ロールの権限で取得するので、イメージやローカルの設定ファイルには残りません。
方式は `config.env` の既定値か、`mvm up` のオプションでセッションごとに選べます。
`mvm` の呼び出し方 (Windows は `.\bin\mvm.cmd`、macOS は `bin/mvm`) は [setup.md](setup.md#mvm-の呼び出し方) を参照してください。以下のコマンドは両 OS 共通です。

`mvm secret set` は値を入力すると画面に表示せずに登録します。値はコマンドライン引数に載せないので、シェルの履歴にも残りません。

```bash
mvm up a1 --agent claude --claude-auth bedrock
mvm up a2 --agent codex  --codex-auth chatgpt
mvm up a3 --agent both   --claude-auth oauth-token --codex-auth api-key   # 1 台で両方
```

## Claude Code

| `--claude-auth` | 課金 | 登録するシークレット | VM 内での設定 |
|---|---|---|---|
| `oauth-token` | Claude Pro / Max サブスクリプション | `claude-oauth-token` | `CLAUDE_CODE_OAUTH_TOKEN` |
| `api-key` | Anthropic API (従量) | `anthropic-api-key` | `~/.claude/settings.json` の `apiKeyHelper` |
| `bedrock` | Amazon Bedrock (AWS 請求) | 不要 (実行ロールを使用) | `CLAUDE_CODE_USE_BEDROCK=1`、`AWS_REGION` |
| `gateway` | 社内 LLM ゲートウェイ / プロキシ | `anthropic-gateway` (JSON) | `ANTHROPIC_BASE_URL`、`ANTHROPIC_AUTH_TOKEN` |
| `interactive` | 任意 | 不要 | 接続後に `claude` を起動して `/login` |

### サブスクリプション (oauth-token)

手元 PC で長期トークンを発行し、登録します。

```bash
claude setup-token                       # ブラウザでログインするとトークンが表示される
mvm secret set claude-oauth-token    # 表示されたトークンを貼り付け
```

注意: 複数台が同じサブスクリプションを使うと、利用上限 (レート制限) を全台で分け合います。上限にかかりやすい場合は、API キーや Bedrock と組み合わせてください。

### API キー

```bash
mvm secret set anthropic-api-key     # Claude Console で発行したキー
```

`apiKeyHelper` で渡すので、起動時に「この API キーを使いますか？」という確認は出ません。

### Amazon Bedrock

1. Bedrock コンソールで Anthropic のモデルへのアクセスを有効にする (初回は利用申請が必要。有効になっていないと `Access to Anthropic models is not allowed for this account.` と返る。PoC で確認)
2. `config.env` の `ENABLE_BEDROCK=true` (既定) のまま `mvm infra deploy` を実行する。実行ロールに `bedrock:InvokeModel*` が付く
3. 必要なら `config.env` の `BEDROCK_REGION` と `CLAUDE_MODEL` (Bedrock コンソールで確認した推論プロファイル ID) を設定する

```bash
mvm up a1 --claude-auth bedrock --model <推論プロファイル ID>   # 例: jp.anthropic.claude-haiku-5-5
```

推論プロファイル ID の一覧は `aws bedrock list-inference-profiles --region ap-northeast-1` で確認できます (`jp.` は国内、`global.` はグローバルのクロスリージョン推論)。

キーを管理する必要がなく、費用も AWS の請求にまとまります。

### LLM ゲートウェイ

次の内容のファイル (例: `gateway.json`) を作り、ファイルから登録します。Windows / macOS 共通です。登録後はファイルを削除してください。

```json
{"baseUrl": "https://llm-gateway.example.com", "authToken": "..."}
```

```bash
mvm secret set anthropic-gateway --file gateway.json
```

## Codex

| `--codex-auth` | 課金 | 登録するシークレット | VM 内での設定 |
|---|---|---|---|
| `api-key` | OpenAI API (従量) | `openai-api-key` | `codex login --with-api-key`、`OPENAI_API_KEY` |
| `chatgpt` | ChatGPT Plus / Pro / Business など | `codex-auth-json` | `~/.codex/auth.json` を配置 |
| `interactive` | 任意 | 不要 | 接続後に `codex login --device-auth` |

### API キー

```bash
mvm secret set openai-api-key
```

### ChatGPT アカウント

手元 PC で一度ログインし、生成された `auth.json` を登録します。

```bash
codex login                                            # ブラウザで ChatGPT にログイン
mvm secret set codex-auth-json --file ~/.codex/auth.json
```

`--file` の `~` は Windows でも `%USERPROFILE%` (`C:\Users\<名前>`) に展開されるので、上のコマンドはどちらの OS でも同じです。

注意: 同じ `auth.json` を複数の VM で同時に使うと、トークン更新のタイミングでログインが切れることがあります。その場合は VM ごとに `interactive` (デバイスコード認証) でログインするか、API キーを使ってください。
`codex login --device-auth` が使えるかどうかは、イメージに入っている Codex のバージョンで `codex login --help` を実行して確認してください。

## GitHub

clone / push 用のトークンです。対象リポジトリに絞った Fine-grained personal access token (Contents: Read and write、Pull requests: Read and write) を推奨します。

```bash
mvm secret set github-token
```

VM 内では git の credential store と `GH_TOKEN` (gh CLI 用) に設定されます。

## 複数アカウントの使い分け

シークレット名を変えて登録し、起動時に指定します (名前は `${PMVA_PREFIX}/` で始める必要があります)。

```bash
mvm secret set claude-oauth-token --name pmva/claude-oauth-token-team
mvm up t1 --claude-secret pmva/claude-oauth-token-team
mvm fleet 3 --prefix team --claude-secret pmva/claude-oauth-token-team
```

## 認証情報の扱い

- `runHookPayload` にはシークレットの名前だけを入れる (値は入れない)
- VM 内では `~/.pmva/env.sh`、`~/.pmva/secrets/`、`~/.codex/auth.json` に権限 600 で置く
- ワークスペーススナップショットからは `~/.claude/.credentials.json` と `~/.codex/auth.json` を除外する
- ローカルでは `mvm secret set` の値をコマンドライン引数に載せない (一時ファイル経由で渡す)。MicroVM のアクセストークンはキャッシュとして `.state/tokens/` (権限 600、最大 60 分有効) に保存される
