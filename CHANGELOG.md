# 変更履歴

利用者から見える変更をバージョンごとに記録します。書き方は [Keep a Changelog](https://keepachangelog.com/ja/1.1.0/)、番号の付け方は [セマンティック バージョニング](https://semver.org/lang/ja/) に従います。

## 未リリース

### 追加

- 端末で引数なしの `mvm` を打つと、よく使うコマンド 8 個に番号を付けて出し、番号か名前の頭 (`se`、`/ui` など) で選べるようにした。名前や指示など要る引数はその場で聞き (名前のときは今のセッションを見せる)、実行の前に同じ意味のコマンド (`実行: mvm send a1 '...'`) を表示する。8 個以外も名前を打てば選べる。Enter だけでやめる。パイプやエージェントから呼んだときは今までどおり表示だけで止まらない
- `mvm ui` の「新しく起動」: 名前、リポジトリ URL、エージェント、最初の指示を入れて、画面から `mvm up` と同じ起動ができる。リポジトリ URL が空欄なら、前回の同じ名前の続き → `mvm ui` を起動したフォルダの `origin` の順に使う (「前回の続きにしない」で `--fresh`)。起動は裏で進め、結果は「裏で進めている操作」に出る
- `mvm setup` に 6 つ目の手順を足した。手元に `claude` / `codex` のコマンドがあり、司令塔の skill (`mvm-orchestrator`) が入っていなければ、入れるか聞いて `mvm orchestrator install` を実行する。入れると、mvm のコマンドを覚えなくても、手元の Claude Code / Codex に日本語で頼めば代わりに打つ。断っても「まだ残っている手順」には数えない
- コマンドを打ち間違えたとき、英語の `invalid choice` と全コマンドの一覧の代わりに「`pek` というコマンドはありません。もしかして: `mvm peek`」と出す。近いものが無ければ `mvm -h` を案内する
- 引数なしの `mvm` で、今の状態と次の一手を表示する。`config.env` が無い → コピーして `mvm setup`、この PC でまだ何もしていない → `mvm setup`、動いているセッションがある → その一覧と `mvm ui` / `send` / `stop`、無い → `mvm up <名前>`、AWS の認証切れなどで状態が取れない → 理由と `mvm doctor`。続けてよく使うコマンド 8 個を出す。`mvm -h` は約 30 個のコマンドを平たく並べていたのを、「よく使うコマンド」と「その他のコマンド」に分けた
- `mvm setup [--profile <名前>]`: 導入の手順のうち済んでいないもの (AWS の構成の作成、普段使う IAM ユーザーへの切り替え、既定の認証方式と GitHub のシークレットの登録、SSH 鍵の作成、イメージのビルド) を 1 つずつ聞いて実行し、最後に `mvm doctor` で確かめる。何度実行しても済んだ手順は飛ばす。`--profile` で `config.env` の `AWS_PROFILE` を書くので、`config.env` を手で書き換えなくてよい。GitHub のトークンを断ると `USE_GITHUB_TOKEN=false` にする。quickstart の手順 4〜7 を「管理者のプロファイルを用意する」と「`mvm setup --profile admin`」の 2 つにまとめた (全体で 9 手順 → 7 手順)。実機では未確認
- `mvm ui [--port <番号>] [--no-open]`: 手元のブラウザで管理画面を開く。`mvm watch` と同じ一覧を 10 秒ごとに更新し (タブを表示している間だけ)、行をクリックすると画面の末尾と最後の応答を出す。指示の送信、再開、renew、保存して停止もボタンでできる (renew と停止は確認してから裏で進める)。127.0.0.1 でだけ待ち受け、起動のたびに作る合言葉と Host ヘッダーを確かめる。Python の標準ライブラリだけで動く
- `mvm up <name>` だけで前回の続きから始める。`--repo` を省くと、同じ名前で前に起動したセッションの repo・ブランチ・起点のブランチを引き継ぎ、保存したワークスペースがあれば復元する (`--fresh` で引き継がない)。前のセッションが無ければ、今いるフォルダの git の `origin` を clone する (`ssh` の URL は `https` に直し、URL のトークンは取り除く。`mvm` 自身のフォルダでは使わない)。`mvm fleet` も `--repo` を省くと `origin` を使う。clone しないときは `--no-repo`。`--repo` を明示したときの動きは変わらない
- `mvm infra operator [--user <名前>] [--profile <名前>]`: 普段使う IAM ユーザー (`pmva-operator` ポリシーだけを付ける) とアクセスキーを作り、AWS CLI のプロファイル (既定 `pmva`) に保存して `config.env` の `AWS_PROFILE` を切り替える。quickstart の手順 5 (ARN のコピー、`aws iam` を 3 回、`aws configure`、`config.env` の書き換え) が 1 回のコマンドになる。秘密のアクセスキーは画面にもコマンドライン引数にも出さない。管理者のプロファイルで実行する。実機では未確認
- `mvm up --remote-control`: VM の Claude Code を、claude.ai/code やスマホの Claude アプリ (Remote Control) から操作できるようにする。`--claude-auth interactive` で起動し、`mvm login <name>` (この VM では `claude auth login --claudeai`) でログインすると、待っていた `claude` のウィンドウが `claude --remote-control pmva-<name>` で自動で起動する。claude.ai のサブスクリプションのフル権限のログインが要り、`oauth-token`・API キー・Bedrock・Codex では使えない (エラーで案内する)。この VM だけ、非必須通信をまとめて止める `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` などを外し、テレメトリなどは個別の変数で止める。`mvm renew` / `--restore` ではログインし直し。`image/` を変えたので `mvm image build` が要る。実機では未確認 ([docs/poc-checklist.md](docs/poc-checklist.md) の K)
- MicroVM のメモリのクォータで起動が止まったときの案内に、具体的な数字を足した。`mvm up` / `fleet` が上限で止まったときと `mvm doctor` で台数が足りないときに、「`MEMORY_MIB=2048` にすれば同じクォータで N 台」と、AWS サポートの「Service limit increase」のケースに貼れる申請文 (リージョンと希望値の GB を埋めたもの) を出す。AWS が `ServiceQuotaExceededException` で拒否したときは、この PC の `mvm ls` に出ない VM もクォータを使うことと、`aws lambda-microvms list-microvms` での確認を案内する
- Claude Code の自動メモリ (Claude が覚えたこと) を、手元の PC と MicroVM で共有する。`mvm memory status|pull|push|sync` で手元と S3 (`memory/<repo-id>/`) を同期し、`config.env` の `MEMORY_SYNC=true` (または `mvm up --memory`) で、`--repo` 付きの VM が起動時に取得し、定期保存のときに送る。最後に同期した内容を基準に比べ、競合は `--force` で解く。`MEMORY.md` は行の和集合でマージする。frontmatter に `share: false` と書いたファイル、認証情報らしい文字列を含むファイル、1MB を超えるファイルは送らない。ファイルを手で消しても伝わらない (`mvm memory rm` で消したものだけは、次の項目の印で伝わる)。実行ロールに `memory/*` の読み書きを、操作用ポリシーに `memory/*` の削除 (間違ったメモリを `aws s3 rm` で消すため) を追加 (`mvm infra deploy` が必要)。VM の中の動き (取得と送信) は実機で未確認 ([docs/poc-checklist.md](docs/poc-checklist.md))
- 自動メモリをフックで自動で同期する。`mvm memory install-hooks` (外すときは `uninstall-hooks`) で、手元の Claude Code が会話の開始時と指示のたび (60 秒に 1 回まで) に S3 から取得して、他のマシンで書かれたメモリを会話に渡し、メモリを書いたらすぐ送る。`mvm up --memory` の VM にも同じフックが入る。S3 への書き込みは条件付きにして、同時に書き込まれたときに他のマシンの内容を上書きしない (`MEMORY.md` は取り直してマージし直す)。フックの動きは実機で未確認
- 自動メモリの誤りを見つけて戻す・消す機能。S3 に送るたびに `memory/<repo-id>/.history/<名前>/` へ写しを残す。`mvm memory log [<名前>] [--origin <マシン名>] [--since 30m|2h|3d|日時] [--json]` で、いつ・どのマシンが何を書いた (消した) かを一覧し、`mvm memory revert <名前> [--to <時刻>] [--dry-run] [--force]` で履歴の版 (既定は 1 つ前。続けて実行すると取り消し) に戻し、`mvm memory rm <名前> [--dry-run]` で全部のマシンから消す。`rm` した削除は S3 の `.deleted.json` に残す印で他のマシンに伝わり、`pull` (フックを含む) が手元のファイルと索引の行を消して、会話にも「他のマシンで消されたメモリ」と伝える。それまでは削除が伝わらなかった。イメージ 8.0 以前の VM は印を読まないので、`rm` したメモリを送り直すことがある (`mvm image build` で作り直す)。`rm` は手元の PC で実行する (VM の実行ロールに `s3:DeleteObject` はない)。VM の司令塔 skill に、VM が書いたメモリを確かめて戻す・消す手順を追加。実機 (本物の S3、VM の会話) は未確認 ([docs/poc-checklist.md](docs/poc-checklist.md) の J3)
- `mvm image prune [--keep N] [--yes]`: イメージの古いバージョンだけを削除する。`build` のたびに増える版は自動では消えず、1 版 (約 2.3GB) ごとに約 $0.22/月の保存料金がかかっていた。既定では最新の ACTIVE 版を含めて 2 版を残し、ビルド中の版と起動中の VM が使っている版は消さない
- 最小構成の CloudFormation テンプレート `infra/cloudformation-minimal.yaml`。`config.env` の `INFRA_TEMPLATE=minimal` で、キープアライブ・毎晩の自動停止・予算アラートを作らずに使える
- `mvm wait`: エージェントの応答が終わるか、許可・入力待ちになるまで待つ (`--timeout`、`--any`、`--json`)。エージェントの状態 (作業中・完了・入力待ち・終了など) は、VM の中でエージェントのフックが記録する
- `mvm result`: エージェントの最後の応答の文章を表示する
- `mvm ls` / `mvm peek` の `--json`。`peek` と `watch` にエージェントの状態を表示
- `mvm follow`: エージェントの推論の過程 (指示・思考・ツールの実行と結果・応答) を、会話の記録から流れるように表示する。複数台をまとめて表示でき、`--json` で 1 行に 1 件の JSON も出せる
- `mvm view`: エージェントの画面を読み取り専用で表示する (入力はエージェントに届かない)
- `mvm top`: VM のメモリの使用量と、多く使っているプロセスを表示する。メモリ不足でプロセスが強制終了されたら `top` / `peek` に表示し、通知も送る
- `mvm orchestrator install claude|codex`: 手元の Claude Code / Codex を司令塔にする。MicroVM に作業を任せる skill (`mvm-orchestrator`)、確認用の `mvm` コマンドの許可、`--dir` ではそのフォルダで重い処理 (npm / pytest / ビルド / ブラウザ) を止めるフックを入れる ([docs/orchestrator.md](docs/orchestrator.md))
- リポジトリの `.pmva/setup.sh` を VM の起動時 (エージェントを起動する前) に実行する。依存関係のインストールやブラウザの用意に使う。`SETUP_TIMEOUT_SECONDS`、`mvm up --no-setup`
- イメージに Chromium (Playwright / Puppeteer) の実行に必要なライブラリと日本語フォント (`IMAGE_BROWSER_DEPS`)、uv、`pmva-node` (リポジトリが求める版の Node.js を入れる) を追加。`IMAGE_EXTRA_PACKAGES` で OS のパッケージを追加できる
- `mvm profile push` / `show`: 手元の Claude Code / Codex のルール (`CLAUDE.md` / `AGENTS.md`)、skill、サブエージェント、コマンドを VM に渡す。認証情報らしい文字列を含むファイルや設定ファイルは送らない。`config.env` の `PROFILE`、`mvm up --profile` / `--no-profile`。実行ロールに `profiles/*` の読み取りを追加 (`mvm infra deploy` が必要)
- AWS で使える認証方式を追加 ([docs/auth.md](docs/auth.md))。実行ロールの方式はキーの登録が要らない
  - Claude Code: `--claude-auth bedrock-api-key` (Bedrock の API キー)、`mantle` (Bedrock の Mantle)、`anthropic-aws` / `anthropic-aws-api-key` (Claude Platform on AWS。`ANTHROPIC_AWS_WORKSPACE_ID`、`ANTHROPIC_AWS_REGION`)
  - Codex: `--codex-auth bedrock` (Bedrock の OpenAI のモデル)、`bedrock-api-key`
  - 実行ロールに Bedrock の Mantle の呼び出し権限を追加し、Codex 用の実行ロールにも Bedrock の権限を付けた。Claude Platform on AWS の権限は `ENABLE_ANTHROPIC_AWS=true` のときだけ付ける (どちらも `mvm infra deploy` が必要)
- `mvm login <name> [claude|codex]`: VM の中のエージェントに、手元のブラウザでログインする。Codex は手元の 1455 番ポートを VM につないで `codex login` を動かし、ログインの URL を手元のブラウザで開く (`--device` でコードを入力する方式)。Claude Code は `claude auth login` (`--console`)
- Codex の認証方式 `--codex-auth access-token` (Codex のアクセストークン) と `gateway` (Responses API と同じ形の接続先。キーを Bearer か任意のヘッダーで送る、URL のパラメータ、追加のヘッダー)
- 認証方式 `--claude-auth custom` / `--codex-auth custom`: シークレットに書いた環境変数、ファイル (`~/.pmva/secrets/` に権限 600)、エージェントの設定 (`~/.claude/settings.json` に足す設定、`~/.codex/config.toml` の接続先) をそのまま入れる。一覧にない方式や、エージェントの新しい版で増えた方式に使う
- `mvm doctor` が、AWS の構成 (スタック) が手元のテンプレートと `config.env` のとおりか、イメージの元が最後のビルドから変わっていないか、既定の認証方式のシークレットと設定がそろっているかを確かめ、`mvm infra deploy` / `mvm image build` のやり直し忘れや登録漏れを知らせる。操作用ポリシーに `cloudformation:GetTemplate` を追加 (`mvm infra deploy` が必要)
- `mvm` をエージェント (Codex など) のサンドボックスの中で動かして、状態の保存先に書けない・VM に接続できないときは、足すべき設定 (`writable_roots`、`network_access`) を案内する
- `mvm forward <name> <port>`: VM の中のポート (開発サーバーなど) を手元の PC のポートにつなぐ
- CI の `image` ワークフロー: 本物のイメージを ARM64 でビルドし、ツール、headless の Chromium、起動時の初期化を確かめる
- CI の `agents-e2e`: 固定した版の Claude Code / Codex を LLM API の代わりにつないで動かし、フックと会話の記録の読み取りを確かめる

### 修正

- `mvm memory rm` で消したメモリを手元で同じ名前で作り直した後に、古い VM (イメージ 8.0 以前) が消された内容を S3 に送り直すと、そのメモリの同期が詰まっていた。`push` は「競合」や「S3 のほうが新しい。先に pull してください」と出して飛ばし、`pull` は消された内容を取得しないので何もせず、`--force` でも直らないことがあった。S3 にあるのが消された内容なら、手元が新しいものとして `push` で手元の内容に置き換える (`share: false` などの送らないファイルは置き換えない)。`status` は「手元が新しい (S3 にあるのは … 送り直されたもの …)」と表示する。VM 側はイメージの作り直し (`mvm image build`) で反映
- メモリ本体は S3 に送れて、履歴 (`.history/`) の書き込みだけが AccessDenied になったときの警告が、「memory/ の権限がありません。mvm infra deploy でスタックを更新してください」と案内していた。この場合はスタックの外 (後から付けたポリシー、バケットポリシー、SCP など) が拒否しているので、スタックを更新しても直らない。本体は送れたこと、`.history/` だけが拒否されたこと、スタックの更新では直らないこと、確かめる場所を表示する。VM 側はイメージの作り直し (`mvm image build`) で反映
- `mvm memory status` が、`mvm memory rm` で消したメモリを古い VM (イメージ 8.0 以前) が S3 に送り直したものを、ふつうの「S3 だけ」と表示していた。`pull` はそれを取得しないので、取得されるように見えて食い違っていた。消された内容が送り直されたものであること、取得しないこと、手元の PC で `mvm memory rm <名前>` を実行すれば S3 から消せることを表示する。VM 側はイメージの作り直し (`mvm image build`) で反映
- 手動の `mvm memory pull` / `push` / `sync` / `rm` / `revert` と VM の終了時の送信 (snapshot.sh) が、メモリ共有のフックの同期と同時に動くことがあった。どちらも同じ状態ファイル (最後に同期したときの内容) を書き換えるので、片方の記録が消え、次の同期で実際には無い競合が出るおそれがあった。手動の同期もフックと同じロックを取り、フックの同期が終わるのを最長 60 秒待つ (VM の終了時の送信は、ワークスペースの保存が打ち切られないよう 10 秒まで)。あわせて、ロックを持っている間は AWS CLI を呼ぶたびにロックの時刻を新しくし、ファイルが多くて 120 秒を超える同期が、途中で他の同期にロックを奪われないようにした。VM 側はイメージの作り直し (`mvm image build`) で反映
- Windows で、複数の会話のメモリ共有のフック (`mvm memory hook`) が同時に動くと、まれにトレースバック (`PermissionError: [Errno 13]`) で落ちていた。他の会話が外している最中のロックを作ろうとしたときに起きていた。待って取り直すようにした。あわせて、メモリのファイルの置き換えと削除 (`mvm memory rm`、他のマシンで消されたメモリの pull)、ロックの解放、S3 との受け渡しに使う一時フォルダの後始末も、他のプロセスがファイルを使用中で一時的に失敗したときは少し待ってやり直す
- `--claude-args "--dangerously-skip-permissions"` (または `DEFAULT_CLAUDE_ARGS` に同じ指定) で起動すると、Claude Code が許可を省くことへの同意の確認画面 (既定の選択肢は「No, exit」) で止まり、そこへ `mvm send` すると Claude Code が終了していた。VM の `~/.claude/settings.json` に同意済みの設定 (`skipDangerousModePermissionPrompt`) を書き、確認画面を出さずに作業を始める。イメージの作り直し (`mvm image build`) が必要
- AWS の認証が切れているときなど、問い合わせに失敗しただけなのに「無い」と案内していた。`mvm image status` / `up` / `build` は「イメージはまだありません (mvm image build)」、`up` / `infra outputs` は「スタックが見つかりません。先に mvm infra deploy を実行してください」、`up --profile` は「設定のまとまりがまだありません」と出て、要らない作り直しを招くおそれがあった。失敗の理由 (認証切れなら `aws sso login`、権限、通信) と次にやることを出して止める。`mvm doctor` のスタックとシークレットの項目も同じ
- `mvm image prune` / `mvm image delete` (`--yes` なし) と `mvm infra delete` を、端末の無いところ (司令塔のエージェント、CI、パイプ) から実行すると、確認の答えを読めずに `EOFError` のトレースバックで落ちていた。何も削除せずに、`--yes` を付ける (`infra delete` は端末から実行する) よう案内して中止する。パイプで `y` を渡したときは従来どおり進む
- 版の確認のワークフロー (`agent-versions.yml`) が、固定した版を最新に上げた後も Issue「Claude Code / Codex の新しい版があります」を古い内容のまま開いておいた。最新に追いついていれば、その Issue を閉じる
- `GIT_USER_NAME` / `GIT_USER_EMAIL` が空 (既定) のまま `--repo` で起動すると、VM の中でコミットが「Please tell me who you are」で失敗していた (VM では git が作成者を推測できない)。空のときは、GitHub トークンの持ち主のログイン名と noreply のアドレス (`<id>+<login>@users.noreply.github.com`) を使う。トークンから読めないときは `mvm peek` の `bootstrap` に設定を促す警告を出す。イメージの作り直し (`mvm image build`) が必要
- `mvm wait` が `suspended` で返ったとき、作業が終わったのか途中で止まったのかが分からなかった (ドキュメントは「作業中の VM はサスペンドしない」としていたが、手動の `mvm suspend` やキープアライブの無い最小構成では作業の途中でもサスペンドする)。VM は起こさないまま、`mvm peek <name>` で再開して確かめる手順を表示し、`--json` の `hint` にも入れる。司令塔の skill も、`suspended` なら `peek` で確かめて待ち直すようにした (`mvm orchestrator install` で入れ直すと反映)
- Windows Terminal や Claude のデスクトップアプリの端末から `mvm ssh` / `mvm view` でアタッチすると、シェルのプロンプトに `61;4;6;7;...c0;10;1c` のような文字列が入ることがあった (Enter を押すとコマンドとして実行される)。これらの端末 (ConPTY) は、代替画面に切り替えた直後の問い合わせに応答を 2 回返すことがあり、tmux が 2 回目をキー入力として扱っていた。tmux が手元の端末で代替画面を使わないようにした。そのため、Ctrl-b → d で抜けた後は、元の画面に戻らず tmux の最後の画面が残る (`mvm image build` で作り直すと直る)
- `mvm stop` / `stop-all` / `renew` と毎晩の自動停止で、終了のたびにワークスペースを 2 回保存していた (保存の数秒後に、VM の終了時のフックがもう一度保存していた)。保存の履歴と終了にかかる時間が倍になっていた。保存できたら終了時のフックでは保存しない。保存に失敗したときは、これまでどおり終了時のフックでもう一度試みる。古いイメージの VM では 2 回保存のまま (`mvm image build` で作り直すと直る。毎晩の自動停止は `mvm infra deploy` も必要)
- `mvm stop --no-save` / `stop-all --no-save` でも、VM の終了時のフックがワークスペースを保存し、`workspaces/<名前>/latest.tar.gz` を上書きしていた (`--restore latest` で前の保存に戻れなかった)。終了の前に VM に伝え、終了時のフックでも保存しない。古いイメージの VM では伝えられないので、注意を表示する (`mvm image build` で作り直すと直る)
- Claude Code が認証の失敗や利用上限などの API のエラーで止まっても、`mvm wait` が `waiting`、`mvm result` が「Claude is waiting for your input」と報告し、司令塔が失敗に気づけなかった。Claude Code の `StopFailure` フックで新しい状態 `error` を記録し、`mvm wait` / `mvm result` がエラーの種類と文章、次にやることを表示する (`mvm result` は終了コード 1)。通知 (`--notify`) も送る
- `mvm secret set`: 端末の折り返しごとコピーしたトークンを登録すると、途中に空白や改行が入って認証が 401 で失敗していた。トークンの種類 (JSON・URL 以外) は途中の空白や改行を取り除いて登録し、VM の起動時にも取り除く (登録済みの値も、イメージを作り直せば使える)。`claude-oauth-token` が `sk-ant-oat01-` で始まらないときは注意を表示する
- `mvm follow`: Codex がリポジトリの `AGENTS.md` を読み込んだとき、その中身を利用者の指示として表示していた
- Windows で `mvm ssh` / `view` / `forward` / `login` を使うと、カレントフォルダに `nul` という名前のファイル (VM のホスト鍵) ができていた。ホスト鍵は `.state/known_hosts/<name>` に書き、VM を起動するたびに消す。`mvm ssh-config --install` で書いた設定も同じなので、入れ直す。前の版で作られた `nul` は消してよい (Git Bash なら `rm nul`)
- `mvm ssh <name> --no-reconnect` (名前の後ろにオプションを書く、ドキュメントの書き方) で、`--no-reconnect` が ssh にコマンドとして渡り、接続できなかった
- `mvm doctor`: `mvm infra deploy` の直後でも「AWS の構成が、手元のテンプレートと違います」と出ていた。CloudFormation がテンプレートの日本語を `?` に置き換えて保存するため。比べる前に手元の側もそろえるようにした
- `printf '...' | mvm shell <name>` のように標準入力をパイプやファイルにすると、コマンドは VM で動いても結果が表示されずに終わっていた。読み終えたら Ctrl-D でシェルを終わらせ、VM 側が閉じるまで出力を表示するようにした (`exit` を書かなくても終わる)
- `mvm renew`: SSH 公開鍵なしで起動した後に鍵を作っても、鍵付きにならなかった (`mvm ssh` は「鍵を用意して `mvm renew`」と案内していた)。renew のときに `SSH_PUBLIC_KEY_FILE` を読み直す
- 失敗が後になってから分かる箇所に、早めの確認を足した。`mvm up` / `mvm fleet` は、起動の前にシークレットの登録を確かめる (無いまま起動すると、課金が始まってから bootstrap が失敗していた)。`mvm image build` の完成待ちは、AWS の呼び出しが 3 回続けて失敗したら (SSO の期限切れなど) ドットを出し続けずに理由を出して止まる。`mvm doctor` は、クォータで 1 台も起動できないときを `NG` にし、AWS 認証の失敗の理由 (期限切れ・プロファイル・ネットワーク) を出し、`NIGHTLY_STOP_TIME` が空か `BUDGET_MONTHLY_USD` が 0 のときに安全網が無効だと `!!` で知らせる

### 変更

- `mvm watch` を、全台を 1 台 1 行で見渡せる一覧ボードに作り直した。状態、経過時間、メモリ (使用/全体と、動かしている間の最大)、最後の出来事 (会話の記録の最後の 1 件) を表示し、許可・入力待ち、エラー、`UNKNOWN`、メモリ不足 (OOM) などの要対応の台は表の下に理由とやることを出す。`--once`、`--json` (台ごとの状態の JSON)、`--log <CSV>` (台ごと・更新ごとの状態とメモリを追記。2GB の VM で足りるかの測定に使える) を追加し、端末の幅に合わせて日本語の桁をそろえ、端末なら色を付ける (`NO_COLOR` で無効)。通信を減らすため、AWS CLI の `get-microvm` は 1 台につき 60 秒に 1 回までにし、`SUSPENDED` の VM には `/status` も `/events` も呼ばない
- `mvm watch` の既定の間隔を 60 秒から 10 秒に変更 (呼び出しを軽くしたので負荷はほぼ同じ)。画面の末尾は出さなくなったので、画面を見たいときは `mvm peek` / `mvm view` を使う
- `mvm memory pull|push|sync` の「config.env に MEMORY_SYNC=true を書いてください」の案内を、そのリポジトリで初めて同期したときだけ出す (毎回出ていた)。あわせて `mvm memory install-hooks` を案内する
- ドキュメントを利用者向けに整理した。README を短くし、`docs/setup.md` は構築と管理 (AWS の構成、操作用の IAM ユーザー、イメージの更新、片付け) に絞った。場面別の使い方 (`.pmva/setup.sh`、通知、VS Code、よく使うコマンド) は `docs/usage.md` に、症状ごとの確認方法は新しい `docs/troubleshooting.md` にまとめた。インストールスクリプトの最後の案内は `docs/quickstart.md` を指す
- AWS から VM の状態を取れなかったとき (`UNKNOWN`)、`mvm ls` / `watch` / `wait` / `top` / `stop` / `renew` / `infra delete` に、理由 (認証切れ、認証情報が無い、権限が無い、接続できない など) とやるべきこと (`aws sso login`、`AWS_PROFILE` の確認、`mvm doctor`) を表示する。`ls --json` は `stateError`、`wait --json` は `error` に入れる。`infra delete` は、状態を取れないセッションを「起動中」と言わずに理由を出して止める
- Claude Code の hooks と Codex の `notify` を、通知 (`--notify`) を使わないときも常に設定するようにした (状態の記録のため)。`~/.claude/settings.json` に利用者が書いたフックは残す。新しいイメージ (`mvm image build`) が必要
- 同時に動かす台数の目標を 10 台から「AWS アカウントのクォータの範囲内」に変更。`MAX_AGENTS` の既定を `0` (クォータで決まる台数まで) にし、`mvm doctor` はクォータに対して何台動かせるかを表示する。コストの試算 (`mvm cost`) の既定も 2 台にした
- `mvm cost` (試算) の単価を、東京リージョンの 2026 年 11 月の実際の請求に合わせた。low / high の 2 行をやめて 1 行 (`tokyo`) にし、スナップショットの読み込みと書き込みを別の単価 (`--snapshot-read-gb` / `--snapshot-write-gb`) で計算する。日本の消費税 10% を含めた税込の額も表示する (`--tax-rate 0` で除外)。`docs/cost.md` に実費との比較を追加
- MicroVM イメージの Claude Code を 2.1.312 から 2.1.330 に、Codex を 0.165.0 から 0.171.0 に更新。新しいイメージ (`mvm image build`) が必要で、起動中の VM は `mvm renew` で移れる

## [0.1.0] - 2026-10-29

最初のリリースです。

### 追加

- **手元 CLI `mvm`** (Windows / macOS / Linux。Python 標準ライブラリのみ)
  - 起動: `up`、`fleet` (タスクファイルで台ごとに別の指示)
  - 確認・指示: `ls`、`peek`、`send`、`watch`、`logs`
  - 接続: `ssh` (SSH over WebSocket、切れたら自動で再接続)、`ssh-config` (VS Code Remote-SSH 用)、`shell`、`token`
  - 保存・停止: `save`、`suspend` / `resume`、`stop`、`stop-all`、`renew` (8 時間の寿命を越えて続ける)
  - 準備: `doctor` (前提ツール、クォータ、管理者権限の認証情報の警告)、`infra`、`secret`、`image`
  - 費用: `cost` (試算)、`cost --actual` (Cost Explorer の実績)
- **認証方式**
  - Claude Code: サブスクリプション (`claude setup-token`)、API キー、Amazon Bedrock、LLM ゲートウェイ、VM 内でのログイン
  - Codex: API キー、ChatGPT アカウント (`auth.json`)、デバイスコードでのログイン
- **MicroVM イメージ** (ARM64 / Amazon Linux 2023): Claude Code 2.1.312 と Codex 0.165.0 を固定。`mvm image versions` / `update-versions` で更新
- **AWS の構成** (CloudFormation)
  - S3 (ワークスペースの保存)、エージェントごとに読める認証情報を分けた実行ロール、操作用ポリシー `pmva-operator`
  - キープアライブ: 作業中の VM だけを起こし続け、PC を閉じても作業を続ける
  - 毎晩の自動停止 (任意)、予算アラート (任意)
- **通知**: Claude Code / Codex の応答の完了・入力待ちを Slack などの Webhook に送る
- **セットアップ**: `scripts/install.sh` (macOS / Linux)、`scripts/install.ps1` (Windows)
- **ドキュメント**: 最短手順、使い方ガイド、コマンド・設定リファレンス、認証、構築と運用、FAQ、費用、設計、検証結果、開発者向けガイド
- **CI**: Windows / macOS (Python 3.9・3.13)、Linux (3.13) のテストと lint。Claude Code / Codex の新しい版を毎週確認して Issue で知らせるワークフロー

[0.1.0]: https://github.com/yucn6201g/parallel-microvms-agents/releases/tag/v0.1.0
