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

## どれを選ぶか

| したいこと | Claude Code | Codex |
|---|---|---|
| サブスクリプション (定額) で使う | `oauth-token` | `chatgpt` |
| キーを管理せず、費用を AWS の請求にまとめる | `bedrock` / `mantle` / `anthropic-aws` | `bedrock` |
| 別の AWS アカウントの Bedrock を使う | `bedrock-api-key` | `bedrock-api-key` |
| API キー (従量) で使う | `api-key` / `anthropic-aws-api-key` | `api-key` |
| 社内の LLM ゲートウェイを通す | `gateway` | (未対応) |

`bedrock`、`mantle`、`anthropic-aws`、Codex の `bedrock` は、MicroVM の実行ロールの権限で呼びます。シークレットの登録は要りません。

## Claude Code

| `--claude-auth` | 課金 | 登録するシークレット | VM 内での設定 |
|---|---|---|---|
| `oauth-token` | Claude Pro / Max サブスクリプション | `claude-oauth-token` | `CLAUDE_CODE_OAUTH_TOKEN` |
| `api-key` | Anthropic API (従量) | `anthropic-api-key` | `~/.claude/settings.json` の `apiKeyHelper` |
| `bedrock` | Amazon Bedrock (AWS 請求) | 不要 (実行ロールを使用) | `CLAUDE_CODE_USE_BEDROCK=1`、`AWS_REGION` |
| `bedrock-api-key` | Amazon Bedrock (キーを発行した AWS アカウントに請求) | `claude-bedrock-api-key` | `CLAUDE_CODE_USE_BEDROCK=1`、`AWS_BEARER_TOKEN_BEDROCK` |
| `mantle` | Amazon Bedrock の Mantle (AWS 請求) | 不要 (実行ロールを使用) | `CLAUDE_CODE_USE_MANTLE=1`、`AWS_REGION` |
| `anthropic-aws` | Claude Platform on AWS (AWS Marketplace 請求) | 不要 (実行ロールを使用) | `CLAUDE_CODE_USE_ANTHROPIC_AWS=1`、`ANTHROPIC_AWS_WORKSPACE_ID` |
| `anthropic-aws-api-key` | Claude Platform on AWS | `anthropic-aws-api-key` | 上に加えて `ANTHROPIC_AWS_API_KEY` |
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

### Amazon Bedrock の API キー (bedrock-api-key)

Bedrock コンソールで発行した API キーで呼びます。別の AWS アカウントの Bedrock を使いたいときに使います (実行ロールは、MicroVM と同じアカウントの Bedrock しか呼べません)。

```bash
mvm secret set claude-bedrock-api-key        # Bedrock コンソールで発行した API キー
mvm up a1 --claude-auth bedrock-api-key --bedrock-region us-east-1
```

- キーには有効期限があります (短期キーは最長 12 時間)。長く使うなら長期キーを発行するか、実行ロールの `bedrock` を使ってください
- モデルの指定 (`--model`) とリージョン (`--bedrock-region`) は `bedrock` と同じです

### Amazon Bedrock の Mantle (mantle)

Mantle は、Anthropic の API と同じ形で Claude を呼べる Bedrock の入口です。実行ロールで呼びます (`mvm infra deploy` で `bedrock-mantle:*` の権限が付きます)。

```bash
mvm up a1 --claude-auth mantle --model anthropic.claude-sonnet-5
```

- モデル ID は `anthropic.` で始まり、版の後ろ付けがない形です。使えるモデルは AWS から許可されたものだけで、通常の Bedrock (`bedrock`) とは一覧が違います
- 使えるリージョンも限られます (`--bedrock-region` / `BEDROCK_REGION`)

### Claude Platform on AWS (anthropic-aws)

Anthropic が運用する Claude API を、AWS の認証と AWS Marketplace の請求で使います。Anthropic の API と同じ機能・同じモデルがすぐ使えます。

1. AWS Marketplace で Claude Platform on AWS を購読し、ワークスペースを作る (ワークスペース ID は `wrkspc_...`)
2. `config.env` に次を書き、`mvm infra deploy` を実行する。実行ロールに `aws-external-anthropic:*` (推論とトークン数の計算、モデル一覧) の権限が付く

   ```bash
   ENABLE_ANTHROPIC_AWS=true
   ANTHROPIC_AWS_WORKSPACE_ID=wrkspc_01ABCDEFGHIJKLMN
   ANTHROPIC_AWS_REGION=us-east-1     # ワークスペースのリージョン (空なら AWS_REGION)
   ```

3. 起動する

   ```bash
   mvm up a1 --claude-auth anthropic-aws
   ```

実行ロールの代わりに、ワークスペースの API キー (AWS コンソールの Claude Platform on AWS → API keys で発行) を使うこともできます。キーを使う場合は `ENABLE_ANTHROPIC_AWS` は要りません。

```bash
mvm secret set anthropic-aws-api-key
mvm up a1 --claude-auth anthropic-aws-api-key
```

Claude Console (platform.claude.com) で発行した API キーは使えません (AWS Marketplace で購読すると、別の組織になるため)。

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
| `bedrock` | Amazon Bedrock の OpenAI のモデル (AWS 請求) | 不要 (実行ロールを使用) | `~/.codex/config.toml` の `model_provider = "amazon-bedrock"` |
| `bedrock-api-key` | Amazon Bedrock (キーを発行した AWS アカウントに請求) | `codex-bedrock-api-key` | 上に加えて、キーをファイルから読む設定 |
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

### Amazon Bedrock (bedrock)

Codex に組み込みの `amazon-bedrock` (Bedrock の Mantle) で、OpenAI のモデルを AWS の請求で使います。実行ロールで呼ぶので、キーの登録は要りません (`mvm infra deploy` で `bedrock-mantle:*` の権限が付きます)。

```bash
mvm up c1 --agent codex --codex-auth bedrock
mvm up c2 --agent codex --codex-auth bedrock --codex-args "--model openai.gpt-5.5"   # モデルを選ぶ
```

- リージョンは `--bedrock-region` / `BEDROCK_REGION` (既定は `AWS_REGION`)。Codex 0.165.0 が対応しているのは us-east-1、us-east-2、us-west-2、ap-northeast-1、ap-south-1、ap-southeast-3、eu-central-1、eu-west-1、eu-west-2、eu-south-1、eu-north-1、sa-east-1 です
- モデルを指定しなければ、Codex が決めた既定 (版によって変わる) を使います。アカウントで使えるモデルは Bedrock コンソールで確認してください
- VM の中では `~/.codex/config.toml` の先頭に、`# pmva:provider begin` と `# pmva:provider end` で囲んだ設定を書きます。起動のたびに書き直すので、この部分は手で編集しないでください。`model_provider` を自分で書いている場合は、そちらを優先して警告を出します

別の AWS アカウントの Bedrock を使うときは、API キーを登録します。

```bash
mvm secret set codex-bedrock-api-key
mvm up c1 --agent codex --codex-auth bedrock-api-key
```

キーは環境変数に出さず、`~/.pmva/secrets/` のファイルから読ませます。そのため、`--agent both` で Claude Code と Codex に別々の Bedrock の API キーを使えます。
ただし、Claude Code を `bedrock-api-key`、Codex を `bedrock` (実行ロール) にすると、Codex も Claude Code 用のキー (環境変数 `AWS_BEARER_TOKEN_BEDROCK`) で呼びます (起動時に警告が出ます)。

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
- VM 内では `~/.pmva/env.sh`、`~/.pmva/secrets/`、`~/.codex/auth.json` に権限 600 で置く。`~/.codex/config.toml` にはキーのファイルの場所だけを書き、キーそのものは書かない
- ワークスペーススナップショットからは `~/.claude/.credentials.json` と `~/.codex/auth.json` を除外する
- ローカルでは `mvm secret set` の値をコマンドライン引数に載せない (一時ファイル経由で渡す)。MicroVM のアクセストークンはキャッシュとして `.state/tokens/` (権限 600、最大 60 分有効) に保存される
