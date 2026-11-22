# 認証方式

エージェントの認証情報は **AWS Secrets Manager** (`${PMVA_PREFIX}/...`) に置きます。MicroVM の起動時に `bootstrap.sh` が実行ロールの権限で取得するので、イメージやローカルの設定ファイルには残りません。
方式は `config.env` の既定値か、`mvm up` のオプションでセッションごとに選べます。
以下のコマンドは Windows / macOS で共通です。

`mvm secret set` は値を入力すると画面に表示せずに登録します。値はコマンドライン引数に載せないので、シェルの履歴にも残りません。

```bash
mvm up a1 --agent claude --claude-auth bedrock
mvm up a2 --agent codex  --codex-auth chatgpt
mvm up a3 --agent both   --claude-auth oauth-token --codex-auth api-key   # 1 台で両方
```

## どれを選ぶか

| したいこと | Claude Code | Codex |
|---|---|---|
| サブスクリプション (定額) で使う | `oauth-token` | `chatgpt` / `access-token` / `interactive` + `mvm login` |
| キーを管理せず、費用を AWS の請求にまとめる | `bedrock` / `mantle` / `anthropic-aws` | `bedrock` |
| 別の AWS アカウントの Bedrock を使う | `bedrock-api-key` | `bedrock-api-key` |
| API キー (従量) で使う | `api-key` / `anthropic-aws-api-key` | `api-key` |
| 社内の LLM ゲートウェイ、LiteLLM、Azure OpenAI などを通す | `gateway` | `gateway` |
| VM の中で、手元のブラウザを使ってログインする | `interactive` + `mvm login` | `interactive` + `mvm login` |
| 上のどれにも当てはまらない (エージェントの環境変数や設定を自分で書く) | `custom` | `custom` |

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
| `custom` | 書いた設定による | `claude-custom-auth` (JSON) | 環境変数、ファイル、`~/.claude/settings.json` ([下記](#どの方式にも当てはまらないとき-custom)) |
| `interactive` | 任意 | 不要 | 起動後に手元から `mvm login <name> claude` ([下記](#vm-の中でログインする-mvm-login)) |

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

1. Bedrock コンソールで Anthropic のモデルへのアクセスを有効にする (初回は利用申請が必要。有効になっていないと `Access to Anthropic models is not allowed for this account.` と返る)
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
| `access-token` | ChatGPT のアカウント | `codex-access-token` | `codex login --with-access-token`、`CODEX_ACCESS_TOKEN` |
| `bedrock` | Amazon Bedrock の OpenAI のモデル (AWS 請求) | 不要 (実行ロールを使用) | `~/.codex/config.toml` の `model_provider = "amazon-bedrock"` |
| `bedrock-api-key` | Amazon Bedrock (キーを発行した AWS アカウントに請求) | `codex-bedrock-api-key` | 上に加えて、キーをファイルから読む設定 |
| `gateway` | 接続先による (LiteLLM、Azure OpenAI、社内ゲートウェイなど) | `codex-gateway` (JSON) | `~/.codex/config.toml` の `model_provider = "pmva-gateway"` |
| `custom` | 書いた設定による | `codex-custom-auth` (JSON) | 環境変数、ファイル、`~/.codex/config.toml` ([下記](#どの方式にも当てはまらないとき-custom)) |
| `interactive` | 任意 | 不要 | 起動後に手元から `mvm login <name> codex` ([下記](#vm-の中でログインする-mvm-login)) |

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

注意: 同じ `auth.json` を複数の VM で同時に使うと、トークン更新のタイミングでログインが切れることがあります。その場合は VM ごとにログインする (`--codex-auth interactive` で起動して `mvm login`、[下記](#vm-の中でログインする-mvm-login))、アクセストークンを使う、API キーを使う、のどれかにしてください。

### アクセストークン (access-token)

ChatGPT のアカウントで発行した Codex のアクセストークン (`at-` で始まる個人用のトークン) を登録します。エージェント用の JWT も使えます。`auth.json` と違って Codex がトークンを書き換えないので、複数の VM で同時に使ってもログインが切れません。

```bash
mvm secret set codex-access-token
mvm up c1 --agent codex --codex-auth access-token
```

VM の中では `codex login --with-access-token` で保存し、環境変数 `CODEX_ACCESS_TOKEN` にも入れます (保存に失敗しても使えるように)。
トークンの発行のしかたと、使えるワークスペースの条件は ChatGPT 側の設定によります。発行できない場合は、`mvm login` でログインしてください。

### ゲートウェイ (gateway)

OpenAI の Responses API と同じ形で受け付ける接続先 (LiteLLM、Azure OpenAI の v1 API、社内ゲートウェイなど) を使います。次の形の JSON をファイルに書いて登録し、登録後はファイルを消してください。

```json
{"baseUrl": "https://llm-gateway.example.com/v1", "apiKey": "..."}
```

| 項目 | 必須 | 意味 |
|---|---|---|
| `baseUrl` | 必須 | 接続先 (`/responses` の手前まで) |
| `apiKey` | 任意 | キー。既定では `Authorization: Bearer` で送る |
| `apiKeyHeader` | 任意 | キーを別のヘッダーで送るときのヘッダー名 (Azure OpenAI なら `api-key`) |
| `queryParams` | 任意 | URL に付けるパラメータ (例: `{"api-version": "2025-04-01-preview"}`) |
| `headers` | 任意 | いつも付けるヘッダー。`config.toml` にそのまま書くので、秘密の値は入れない |

```bash
mvm secret set codex-gateway --file gateway.json
mvm up c1 --agent codex --codex-auth gateway --codex-args "--model <接続先のモデル名>"
```

- VM の中では、`~/.codex/config.toml` の先頭の `# pmva:provider begin` 〜 `end` に接続先 (`pmva-gateway`) を書きます。キーは `config.toml` に書かず、`Authorization: Bearer` なら `~/.pmva/secrets/` のファイルから、`apiKeyHeader` なら環境変数 `PMVA_CODEX_GATEWAY_KEY` から渡します
- 接続先が Responses API に対応している必要があります (Codex は Chat Completions API には対応していません)

### Amazon Bedrock (bedrock)

Codex に組み込みの `amazon-bedrock` (Bedrock の Mantle) で、OpenAI のモデルを AWS の請求で使います。実行ロールで呼ぶので、キーの登録は要りません (`mvm infra deploy` で `bedrock-mantle:*` の権限が付きます)。

```bash
mvm up c1 --agent codex --codex-auth bedrock
mvm up c2 --agent codex --codex-auth bedrock --codex-args "--model openai.gpt-5.5"   # モデルを選ぶ
```

- リージョンは `--bedrock-region` / `BEDROCK_REGION` (既定は `AWS_REGION`)。Codex 0.165.0 が対応しているのは us-east-1、us-east-2、us-west-2、ap-northeast-1、ap-south-1、ap-southeast-3、eu-central-1、eu-west-1、eu-west-2、eu-south-1、eu-north-1、sa-east-1 です (版によって変わります)
- モデルを指定しなければ、Codex が決めた既定 (版によって変わる) を使います。アカウントで使えるモデルは Bedrock コンソールで確認してください
- VM の中では `~/.codex/config.toml` の先頭に、`# pmva:provider begin` と `# pmva:provider end` で囲んだ設定を書きます。起動のたびに書き直すので、この部分は手で編集しないでください。`model_provider` を自分で書いている場合は、そちらを優先して警告を出します

別の AWS アカウントの Bedrock を使うときは、API キーを登録します。

```bash
mvm secret set codex-bedrock-api-key
mvm up c1 --agent codex --codex-auth bedrock-api-key
```

キーは環境変数に出さず、`~/.pmva/secrets/` のファイルから読ませます。そのため、`--agent both` で Claude Code と Codex に別々の Bedrock の API キーを使えます。
ただし、Claude Code を `bedrock-api-key`、Codex を `bedrock` (実行ロール) にすると、Codex も Claude Code 用のキー (環境変数 `AWS_BEARER_TOKEN_BEDROCK`) で呼びます (起動時に警告が出ます)。

## どの方式にも当てはまらないとき (custom)

エージェントが対応していても上の一覧にない方式や、エージェントの新しい版で増えた方式は、`custom` で使えます。エージェントに渡す環境変数、ファイル、設定を JSON に書いて登録します。

```json
{
  "env":   {"環境変数の名前": "値"},
  "files": {"ファイル名": "中身"},
  "claudeSettings": {"~/.claude/settings.json に足す設定": "..."},
  "codexConfig": "~/.codex/config.toml に足す TOML"
}
```

| 項目 | 意味 |
|---|---|
| `env` | 環境変数。`~/.pmva/env.sh` に書き、エージェントとシェルが読む。`PATH`、`HOME`、`LANG`、`PMVA_*` など VM の動作に要るものは使えない |
| `files` | ファイル。`~/.pmva/secrets/claude-custom/` (Codex は `codex-custom/`) に権限 600 で置く。証明書や鍵、キーを返すスクリプトなどに使う。スナップショットには含めない |
| `claudeSettings` | Claude Code だけ (`claude-custom-auth`)。`~/.claude/settings.json` に足す (`apiKeyHelper`、`awsAuthRefresh`、`env` など)。pmva のフックは消えない |
| `codexConfig` | Codex だけ (`codex-custom-auth`)。`~/.codex/config.toml` の先頭の `# pmva:provider begin` 〜 `end` に書く (`model_provider`、`model_providers.<名前>.*` などのトップレベルのキー) |

`env`、`claudeSettings`、`codexConfig` の値の中の `{{files}}` は、ファイルを置いた場所に置き換わります。JSON をファイルに書いて登録し、登録後はファイルを消してください。

```bash
mvm secret set claude-custom-auth --file claude-auth.json
mvm up a1 --claude-auth custom
mvm secret set codex-custom-auth --file codex-auth.json
mvm up c1 --agent codex --codex-auth custom
```

例: Bedrock の通常の入口 (Invoke API) と Mantle を 1 つのセッションで使う

```json
{"env": {"CLAUDE_CODE_USE_BEDROCK": "1", "CLAUDE_CODE_USE_MANTLE": "1", "AWS_REGION": "us-east-1"}}
```

例: クライアント証明書 (mTLS) と追加のヘッダーが要る社内ゲートウェイ

```json
{
  "env": {
    "ANTHROPIC_BASE_URL": "https://llm-gateway.example.com",
    "ANTHROPIC_AUTH_TOKEN": "...",
    "ANTHROPIC_CUSTOM_HEADERS": "X-Team: platform",
    "CLAUDE_CODE_CLIENT_CERT": "{{files}}/client.pem",
    "CLAUDE_CODE_CLIENT_KEY": "{{files}}/client.key"
  },
  "files": {"client.pem": "-----BEGIN CERTIFICATE-----\n...", "client.key": "-----BEGIN PRIVATE KEY-----\n..."}
}
```

例: 短い時間で切れるキーを、スクリプトで取り直す (Claude Code の `apiKeyHelper`)

```json
{
  "files": {"key.sh": "#!/bin/sh\naws secretsmanager get-secret-value --secret-id pmva/claude-rotating --query SecretString --output text\n"},
  "claudeSettings": {"apiKeyHelper": "sh {{files}}/key.sh"}
}
```

例: Codex を、Responses API に対応した独自の接続先で使う (キーは環境変数から)

```json
{
  "env": {"CORP_LLM_KEY": "..."},
  "codexConfig": "model_provider = \"corp\"\nmodel = \"corp-model\"\nmodel_providers.corp.name = \"corp\"\nmodel_providers.corp.base_url = \"https://llm.example.com/v1\"\nmodel_providers.corp.env_key = \"CORP_LLM_KEY\"\n"
}
```

注意:

- 書いた設定が正しいかは、エージェントが起動してから分かります。うまく動かないときは `mvm ssh` で接続し、エージェントのエラーや `~/.pmva/bootstrap.log` を確認してください
- `claudeSettings` は `~/.claude/settings.json` に残り、スナップショットにも含まれます。キーなどの秘密の値は `claudeSettings` に直接書かず、`files` や `env` に入れてください。別の方式で起動し直しても、足した設定は残ります
- `codexConfig` はトップレベルのキーだけを書いてください (`[テーブル]` を書くと、その後ろにある利用者の設定がテーブルの中に入ってしまいます)。`model_providers.corp.name = ...` のように、ドットでつないだ名前で書けます
- `codexConfig` のキーが、利用者の `~/.codex/config.toml` (スナップショットから戻したものを含む) にすでにあるときは、書かずに警告します。同じキーが 2 回あると、Codex が設定を読めずに起動しないためです
- 実行ロールが読めるシークレットは、Claude Code は `anthropic-` / `claude-`、Codex は `openai-` / `codex-` で始まる名前だけです (`{{files}}` のスクリプトから Secrets Manager を読む場合も同じ)

## VM の中でログインする (mvm login)

`--claude-auth interactive` / `--codex-auth interactive` で起動した VM に、手元のブラウザを使ってログインします。VM ごとに別のログインになるので、1 つの `auth.json` を複数の VM で使い回したときのようにログインが切れることがありません。

```bash
mvm up c1 --agent codex --codex-auth interactive
mvm login c1                 # Codex: 手元のブラウザが開く。ChatGPT にログインすると VM の Codex に届く
mvm login c1 --device        # Codex: コードを入力する方式 (ブラウザの戻り先をつながない)
mvm login a1 claude          # Claude Code: URL を開いてログインし、表示されたコードを貼り付ける
mvm login a1 claude --console   # Claude Code: Anthropic Console (API の従量課金) でログイン
```

- Codex (ブラウザ): VM の中で `codex login` を動かし、手元の 1455 番ポート (ログインの戻り先) を VM につなぎます。表示された URL を手元のブラウザで自動で開きます (開かなければ URL を貼り付ける)。手元で `codex login` を実行中などで 1455 番が使われているときは止まるので、`--device` を使ってください
- Codex (`--device`): 表示された URL を開き、表示されたコードを入力します。ChatGPT のワークスペースの設定で、デバイスコードでのログインが許可されている必要があります
- Claude Code: VM の中で `claude auth login` を動かします。ブラウザでログインした後に表示されるコードを、端末に貼り付けてください
- `mvm ssh` と同じ経路を使うので、SSH 公開鍵を付けて起動したセッションが対象です
- ログインしたときに、エージェントがすでにログイン画面で待っている場合は、そのウィンドウでエージェントを終了して起動し直してください (`mvm ssh` で接続)
- ログイン情報 (`~/.claude/.credentials.json`、`~/.codex/auth.json`) はスナップショットに含めないので、`mvm renew` や `--restore` で VM を作り直したら、もう一度ログインします

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

- 認証情報は Secrets Manager にだけ置き、MicroVM の起動時に取り出す。イメージ、起動時の設定、ワークスペースの保存には含めない (VM の中でログインした情報も保存しない)
- VM の中では `~/.pmva/env.sh`、`~/.pmva/secrets/` などに権限 600 で置く
- 各 VM が読めるのは、そのセッションで使うエージェントの認証情報だけ ([architecture.md](architecture.md#セキュリティ))
- 手元では、MicroVM に接続するための短命のトークン (最大 60 分) を `.state/tokens/` にキャッシュする
