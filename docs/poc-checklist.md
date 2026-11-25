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
- [x] Service Quotas を確認した → 同時に動かせる台数はクォータの範囲内
  - `Max allocated ARM_64 MicroVM memory` (L-CD1C0CC4) が **8GB**。4GB の VM は同時に 2 台まで (2GB なら 4 台)
  - `Rate of RunMicrovm API requests` (L-535CA9B6) が **1 回/秒** (バースト 1)。`mvm fleet` は 1 台ずつ順に起動し、1 台あたり約 4 秒かかるので通常は当たらない
  - `Max Execution Duration of a MicroVM` は 8 時間、イメージ数 100、イメージあたりのバージョン数 50
- [x] `mvm infra deploy` が成功した (操作者は管理者ユーザーだったので `pmva-operator` のアタッチは未確認)

### A の再確認 (2026-11-20、Windows 11 の手元 PC から。同じアカウントの SSO 管理者ロール)

- [x] `mvm doctor`: AWS CLI 2.37.9 が `lambda-microvms` に対応、ベースイメージ `al2023-1` (2026-11-17 更新) が見える
- [x] Service Quotas が引き上げ済み: `Max allocated ARM_64 MicroVM memory` が **80GB** (4GB の VM なら 20 台)、`Rate of RunMicrovm API requests` が 5 回/秒 (バースト 5)。実行時間の上限は 8 時間のまま
- [x] `mvm infra deploy` が成功 (約 2 分 17 秒)。1 回目は、2026-10-29 に削除したスタックの Lambda 関数 `pmva-keepalive` だけが残っていて、事前チェック (修正 19) で止まった。ロールやルールは残っておらず、関数を消してやり直した
- [x] 修正 22: `mvm doctor` が deploy の直後に「AWS の構成が、手元のテンプレートと違います」と出していた (誤検知) → 比べる前に手元のテンプレートの ASCII 以外の文字も `?` にそろえる。実機の doctor で `ok` になることを確認。CloudFormation が、テンプレートの日本語を 1 文字ずつ `?` に置き換えて保存するため。AWS CLI は UTF-8 で正しく送っていることを `--debug` で確認し、日本語だけの最小テンプレートの `validate-template` でも `??????` が返った。日本語はコメントと `Description` にしかなく、埋め込んだ Lambda のコードは保存されたものも構文として正しい (動作には影響しない)
- [x] Bedrock: 推論プロファイルは見えるが、呼ぶと `Access to Anthropic models is not allowed for this account.` (前回と同じ)。今回の PoC は Claude Code の `oauth-token` で進める

## B. イメージビルド

- [x] `mvm image build` が SUCCESSFUL になる (4 回失敗して修正した上で、v5 で成功)
  - ベースイメージ `al2023-minimal` で `dnf` は使えた
  - nodejs.org、npm、github.com からダウンロードできた
  - 修正 1: gh の rpm リポジトリが GPG 検証に失敗 → リリースの tar.gz を checksum 付きで入れる方式に変更
  - 修正 2: validate フックで `tmux --version` (誤り。正しくは `-V`) を待って接続が切れた → 検証を裏で実行し、終わるまで 503 を返す方式に変更
  - 修正 3: `/run` フックの本文は `{"microvmId": "...", "runHookPayload": "<文字列>"}` で届く (ペイロードそのものではない) → 中身を取り出すように修正
- [x] スナップショットのサイズを記録した: メモリ約 713MB、イメージ本体約 2.8GB、追加ディスク約 25MB。Graviton 3 と 4 向けの 2 ビルドが作られる
- [x] 2026-11-20、Windows 11 の手元 PC から現在の `image/` で再ビルド: 1 回で SUCCESSFUL (アップロードから完了まで約 4 分 16 秒)。メモリ約 717MB、イメージ本体約 3.47GB (Chromium のライブラリ、日本語フォント、uv などを足したぶん増えた)、追加ディスク約 35MB。Windows で作った資材 (zip) でも、スクリプトの改行が LF のまま動いた

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
- [x] トークンの有効期限 (60 分) を過ぎても確立済みの SSH 接続が維持される (2026-11-22、Windows 11 の手元 PC から、イメージ 5.0): `mvm ssh rt-test --no-reconnect` で 68 分つなぎ続け、毎分 1 行ずつ出力させた。SSH 用のトークンの期限 (13:28:28 UTC) を 12 分過ぎても切れず、68 分で終了コード 0 で正常に終わった。期限が切れた後の `mvm peek` と新しい `mvm ssh -- <コマンド>` も通り、そのときトークンを取り直した (期限が 1 時間後に更新された)。期限は接続を始めるときにだけ確かめられ、確立済みの接続は切られない
  - 以前の試み: クラウドセッションから 65 分の接続を試したところ、35 分 48 秒で切れた。切れた時刻は、そのクラウドセッションの送信用中継が再起動した時刻 (WebSocket の close コード 1012 `server_draining`) と一致しており、AWS 側の切断ではなかった (G を参照)
- [x] VS Code Remote-SSH (2026-11-22 に Windows の VS Code の画面から接続。C2 を参照)

### C の再確認 (2026-11-20、Windows 11 の手元 PC から。イメージ 1.0)

- [x] `mvm up test-01 --agent none --no-github`: `run-microvm` まで約 3 秒、`bootstrap=ready` まで約 12 秒。VM の中から見えるメモリは 15.7GiB (前回と同じ)
- [x] `mvm ls` / `peek`、`mvm send test-01 --window shell '...'` で送った日本語の指示が tmux に届き、Enter で実行された (`日本語テスト agent aarch64`)
- [x] `mvm shell` に標準入力から送ったコマンドが root で動き、出力が返る (入力の後も標準入力を開けておいた場合)
- [x] 修正 23: `printf 'id; exit\n' | mvm shell test-01` のように、入力を一度に流し込んで標準入力がすぐ閉じると、何も表示されずに終わっていた。入力は VM に届いて実行されていたが、標準入力が閉じた時点で `mvm shell` が接続を閉じ、応答を待たなかった → 標準入力が端末でなければ、読み終えたら Ctrl-D (`\x04`、最後の行に改行がなければ先に改行) を送り、VM 側が閉じるまで出力を受け取る。実機で、`exit` ありとなし、改行なしの入力のどちらも結果が表示されて約 1 秒で終わることを確認
  - VM 側はシェルが終わると接続を閉じる (`exit` の直後に close を受け取ることを、生のメッセージで確認)
- [x] `mvm watch 3`: 3 秒ごとに更新される (出力を取り込んで確認。画面の消去は Windows では `cls`。本物のコンソールでの見た目は 2026-11-21 に C2 で確認)

## C2. 手元 PC の OS ごとの確認

Windows と macOS の両方で使う場合は、それぞれで確認します。

| 確認項目 | Windows (PowerShell / Windows Terminal) | macOS (ターミナル / iTerm2) |
|---|---|---|
| `mvm doctor` が OK | [x] 2026-11-20 (Python 3.14、AWS CLI 2.37.9) | [ ] |
| `mvm up` / `ls` / `peek` / `send` / `watch` | [x] 2026-11-20 (`watch` の見た目は 2026-11-21 に確認) | [ ] |
| `mvm ssh` で tmux にアタッチ、日本語の表示・入力 | [x] 2026-11-21 表示、2026-11-22 日本語の入力と Ctrl-b → d (IME の変換画面そのものは未確認)。修正 28 を参照 | [ ] |
| `mvm shell` (Windows はコンソールの VT モードを使う実装。表示・キー入力・Ctrl-C) | [x] 2026-11-21 表示と Ctrl-C の転送、2026-11-22 矢印・Tab・`sleep` 中の Ctrl-C | [ ] |
| `mvm watch` の描き直し・手元 PC の CPU 使用率・Ctrl-C での終了 | [x] 2026-11-21 | [ ] |
| VS Code Remote-SSH (`mvm ssh-config --install`) | [x] 2026-11-22 (VS Code 1.146、Remote-SSH 0.128) | [ ] |
| 社内プロキシ環境 (`HTTPS_PROXY`) での接続 (該当する場合) | [ ] | [ ] |
| ユーザー名に日本語やスペースを含むパスでの動作 (該当する場合) | [ ] | [ ] |

### C2 の Windows での結果 (2026-11-21、Windows 11、Windows Terminal と Claude アプリの端末)

- [x] `mvm ssh-config --install` の後、Windows 標準の OpenSSH 9.5 で `ssh pmva-<名前>` が通り、`uname -a` が VM (aarch64) の結果を返した (VM には `hostname` コマンドが無い)
- [x] `ssh -L` (ポートフォワード) と `ssh -D` (SOCKS プロキシ) が使えた
- [x] VS Code Remote-SSH の下回り: VM 上で VS Code サーバー (linux-arm64) が起動した。VS Code の画面からの接続は 2026-11-22 に確認 (下記)
- [x] `mvm ssh <名前>` の表示と日本語、`mvm watch` の描き直しと Ctrl-C での終了、`mvm shell` の表示と Ctrl-C の転送を、実際の端末で確認した。`mvm watch` 中の手元 PC の CPU 使用率は低いままだった
- [x] 修正 28: Windows Terminal や Claude アプリの端末 (どちらも ConPTY) から `mvm ssh` でアタッチすると、シェルのプロンプトに `61;4;6;7;...c0;10;1c` のような文字列が入力されることがあった。ConPTY は、代替画面に切り替えた直後の端末の問い合わせ (DA) に応答を 2 回返すことがあり、tmux 3.6a が 2 回目をキー入力としてシェルに渡していた → `image/tmux.conf` で `smcup@:rmcup@` を設定し、tmux が代替画面に切り替えないようにした。動いている VM の tmux に設定を入れて試すと、修正前は 6 回中 5 回漏れ、修正後は 8 回中 0 回 (PR #44)
  - 不採用にした案: `escape-time` を延ばす (原因は応答の分割ではなく重複なので効かない)、`agent-attach` で先に代替画面に切り替えて 0.3 秒待つ (8 回中 3 回漏れた)、Windows のときだけ代替画面をやめる (Windows の ssh も `TERM=xterm-256color` で区別できない)
  - 修正入りのイメージ 4.0 で実機で確認した (2026-11-21、c28): Windows Terminal から `ssh -t pmva-c28 agent-attach` で 8 回、Claude アプリの端末から `mvm ssh c28` で 8 回、アタッチとデタッチを繰り返し、シェルに入った文字列は 0 回。同じ VM の tmux の `terminal-overrides` だけを修正前に戻すと、Windows Terminal で 6 回中 1 回漏れた (確かめ方で漏れを検出できることの確認)

### C2 の Windows での結果の続き (2026-11-22、イメージ 4.0、c29)

キー入力は、Windows の疑似コンソール (ConPTY) の中で `mvm` を動かし、入力パイプにキーの VT シーケンスや文字を書き込んで確かめた。Windows Terminal もキー入力を同じパイプに書き込むので、`mvm` から見た入力の経路は実際の端末と同じ。人が実際にキーボードを打っての確認ではない。

- [x] `mvm shell c29`: 上矢印で直前のコマンドを呼び出して書き足せた (`echo AAA` → `echo AAABBB`)。左矢印で行の途中に挿入できた (`echo XZ` → `XYZ`)。Tab で補完できた (`cat /etc/os-rel` → `/etc/os-release`)。`sleep 30` を 2 秒後の Ctrl-C で止められ、`mvm shell` は切れずに次のコマンドが動いた。`exit` で終了コード 0
- [x] `mvm ssh c29`: tmux のシェルに `echo 日本語テスト、全角ＡＢＣ > /tmp/ja.txt` と入力すると、ファイルの中身が手元で送ったバイト列 (UTF-8) と一致し、画面にも正しく表示された。IME で確定した文字は、この形で端末から届く (IME の変換画面そのものは Windows Terminal の機能で、確認していない)
- [x] `mvm ssh c29` で Ctrl-b → d: `[detached (from session main)]` と出て `mvm ssh` が終了コード 0 で終わり、VM 側の tmux にクライアントが残らなかった
- [x] VS Code の画面からの Remote-SSH 接続: Remote-SSH 拡張 (0.128) を入れ、`code --remote ssh-remote+pmva-c29 /home/agent/work` で開いた。初回は接続先の OS を聞かれるので Linux を選ぶ。ステータスバーに `SSH: pmva-c29` と出て、VM 上に置いたファイルがエクスプローラーに表示され、開くと日本語の中身も正しかった
  - 1 回目は `Connecting with SSH timed out` (接続の待ち時間 17 秒) で失敗し、開き直すと接続できた。原因は特定できていない。同じ形 (`type <台本> | ssh -T -D <ポート> pmva-c29 sh`) を手元で実行すると 1 秒かからずに通った
  - 切り分け (2026-11-22、イメージ 5.0): 接続の前のトークン取得 (`create-microvm-auth-token`) は 0.8 秒で、キャッシュが無いときの `ssh -- true` も 1.6〜1.9 秒 (キャッシュありは 0.9〜1.0 秒)。サスペンドからの自動レジュームも、15 分サスペンドした後で 1.67 秒 (続けての接続は 0.87 秒)、短いサスペンドの後で約 4 秒だった。どれも 17 秒には遠く、タイムアウトの原因はトークンの取得でも VM の再開でもない。VS Code 側の初回だけの処理 (接続先の OS の確認、VS Code サーバーの取得と起動) の可能性が残るが、再現していない。`remote.SSH.connectTimeout` を延ばす案内は、再現するまで入れない
  - VS Code は、PATH で先に見つかった ssh を使う。今回は Git for Windows の `ssh.exe` (OpenSSH 10.3) が使われたが問題なく接続できた。Windows 標準の OpenSSH を使わせたいときは `remote.SSH.path` を設定する

## D. エージェントと認証

今回の PoC では Bedrock と `--agent none` のみ確認しました。

- [x] Claude Code: `bedrock` → VM から Bedrock まで届くことを確認。ただし、このアカウントでは Bedrock の Anthropic モデル利用が有効になっていないため `API Error: 400 Access to Anthropic models is not allowed for this account.` で止まる (手元から直接呼んでも同じエラー)。Bedrock コンソールでモデルの利用申請をすれば使える見込み
  - 推論プロファイルは `jp.anthropic.claude-haiku-5-5` で確認 (Claude Code の表示は「Haiku 5.5 · Amazon Bedrock」)
- [x] Claude Code の `oauth-token` (2026-11-20): `mvm up test-02 --no-github --prompt "一から五までを漢数字で一行ずつ書き、最後の行に「完了」とだけ書いてください。..."` で起動すると、`mvm wait` が `done`、`mvm result` が「一」〜「五」と「完了」を返した (Claude Code v2.1.312、Sonnet 5.5 · Claude API)
  - 修正 24: 端末の折り返しごとコピーしたトークンを `mvm secret set` で登録すると、途中に空白や改行が入り `Not logged in · Please run /login` (401) で止まっていた (2 回続けて発生) → 登録時と VM の起動時に、トークンの途中の空白や改行を取り除く
    - 実機で確認 (2026-11-20、イメージ 2.0): 途中に改行・空白・タブが入った偽の値 (28 文字、取り除くと 24 文字) を `mvm secret set` を通さずに AWS CLI で直接登録して起動。VM のログインシェルの `CLAUDE_CODE_OAUTH_TOKEN` は 24 文字で空白を含まず、期待した値と一致した。エラーも `Not logged in` ではなく `401 OAuth access token is invalid.` (トークンとして読まれている)
  - 修正 25: 認証に失敗しても、bootstrap は `ready`、`mvm wait` は `waiting`、`mvm result` は「Claude is waiting for your input」と報告していた。司令塔が失敗に気づけない → Claude Code の `StopFailure` フックで状態 `error` を記録し、`mvm wait` / `mvm result` がエラーの種類・文章・次にやることを出す
    - 実機で確認 (2026-11-20、イメージ 2.0): 偽の値のトークンを別名のシークレットで登録し、`mvm up test-err --claude-auth oauth-token --claude-secret <そのシークレット> --no-github --prompt "..."` で起動。tmux の対話モードでも `StopFailure` が届き、`mvm wait` が約 19 秒で `error (claude: エラーで止まった)`、`authentication_failed: Please run /login · API Error: 401 OAuth access token is invalid.` と次にやることを表示して終了コード 0 で返った。`mvm result` は同じ内容を表示して終了コード 1
  - イメージ 2.0 (修正 24・25 入り) で、本物のトークンでも従来どおり動くことを確認 (2026-11-20): 同じ指示で起動すると `mvm wait` が `done`、`mvm result` が「一」〜「五」と「完了」(どちらも終了コード 0)。`mvm send` で 2 回目の指示を送っても `done` と「六」〜「八」「完了」が返り、そのまま 90 秒置いても状態は `done` のまま (入力待ちで上書きされない)
- [ ] Claude Code の `api-key` / `gateway` / `interactive` (実機は未確認)
- [ ] Codex の `api-key` / `chatgpt` / `interactive` (実機は未確認)
  - VM 内の初期化 (`bootstrap.sh`) が各方式で正しく設定することは、偽の認証情報を使った自動テスト (`tests/test_supervisor.py`) で確認済み: apiKeyHelper、`CLAUDE_CODE_OAUTH_TOKEN`、ゲートウェイの環境変数、Codex の `auth.json` と `codex login --with-api-key`、GitHub トークン、ファイルの権限 (600)、保存 (スナップショット) に認証情報が入らないこと
  - 修正 15: 登録していないシークレットを `oauth-token` に指定すると、空のトークンのまま「準備完了」になっていた → 失敗として扱う (このテストで発見)
- [x] 起動直後にオンボーディングや「このフォルダを信頼しますか？」の確認で止まらない
- [x] `--claude-args "--dangerously-skip-permissions"` で起動しても、許可を省くことへの同意の確認画面で止まらず、`mvm send` の指示で作業する
  - 2026-11-23 にイメージ 8.0 で、確認画面 (既定は「No, exit」) で止まり、`mvm send` の Enter で Claude Code が終了することを確認 → `bootstrap.sh` が `~/.claude/settings.json` に `skipDangerousModePermissionPrompt` を書くよう修正。固定した版の Claude Code で確認画面が出ないことは自動テスト (`tests/test_agents_e2e.py`) で確認済み
  - 2026-11-24 にイメージ 9.0 (#64 入り、Claude Code 2.1.330) で実機確認。`mvm up j3 --repo ... --memory --claude-args "--dangerously-skip-permissions"` で起動すると、確認画面を出さずに `bypass permissions on` の入力欄で `ready` になり、`mvm send` の 2 回の指示 (メモリを書く、質問に答える) をそのまま実行した
- [x] `mvm up ... --prompt "..."` の初期指示が入力・実行される
- [x] `mvm send` で送った指示 (日本語を含む) がエージェントに届き、Enter で送信される
- [x] `sudo` は**使えない**: コンテナに no_new_privs が付いている (`sudo: The "no new privileges" flag is set`)。イメージから sudo を外し、root が必要な作業は `mvm shell` (root) で行う方針にした
- [x] GitHub への clone / push、`gh pr create` (2026-11-22、テスト用の private リポジトリ、fine-grained personal access token (対象リポジトリだけ、Contents / Pull requests: Read and write))
  - `mvm up gh-poc --repo <URL> --branch pmva-poc/clone-push --prompt "..."` で起動すると、clone と作業ブランチの作成まで済んだ状態でエージェントが始まり、起動時の指示だけでコミット → push → `gh pr create --draft` を終えた (起動から PR の URL が返るまで約 20 秒)。`main` には何も push されず、トークンは `~/.git-credentials` (`credential.helper store`) と `GH_TOKEN` で渡っていた
  - 修正 30: `GIT_USER_NAME` / `GIT_USER_EMAIL` が空 (既定) のままだと、VM の git が作成者を推測できず、エージェントがコミットの手前で止まった (イメージ 5.0) → 空のときは、bootstrap が `gh api user` でトークンの持ち主を読み、ログイン名と `<id>+<login>@users.noreply.github.com` を設定する。読めないときは `bootstrap` の状態に設定を促す警告を出す (PR #53)。イメージ 6.0 で、`GIT_USER_*` を空にしたまま git の設定に触れずにコミット → push → draft PR まで通り、GitHub 上のコミットがトークンの持ち主のアカウントに紐づくことを確認した。fine-grained token でも `GET /user` は読める

## E. ライフサイクル

- [x] 何も接続しない状態で `IDLE_MAX_SECONDS` 後にサスペンドされる (120 秒の設定で、最後の通信から約 132 秒で SUSPENDED)
- [x] `mvm ssh` で接続中はサスペンドされない (120 秒の設定で 3.5 分間接続し続け、ずっと RUNNING)
- [x] サスペンド → 自動レジュームで、tmux・バックグラウンドのプロセス・`/tmp` のファイルが残っていた。`peek` 1 回でレジュームし、応答まで 1.4 秒
  - 長いサスペンド (2026-11-22、イメージ 5.0、手元 PC から): 15 分 `SUSPENDED` のままにした後、`mvm ssh rt-test -- true` が自動レジュームを含めて 1.67 秒で終わった (終了コード 0。続けての接続は 0.87 秒)。サスペンドの長さで再開が遅くなる様子は無かった
  - エージェントが API を呼んでいる最中のサスペンド (2026-11-22、イメージ 5.0、Claude Code 2.1.330、oauth-token): 200 行の応答を書かせ、`claude=working` の 13 秒後に `mvm suspend` で止めた。約 50 秒サスペンドしてから `mvm result` で再開すると、エージェントはそのまま作業を続け、2 秒後に 200 行すべてを書き終えた。会話の記録に API のエラーや再試行は無く、1 回の応答の所要時間 (`turn_duration`) はサスペンド中を含めて 71 秒
- [x] `mvm save` で S3 に保存 (12.8 秒) され、`mvm renew` で別の VM に復元された (保存 → 終了 → 起動 → 復元まで 19 秒。git の履歴も復元)
- [x] terminate フックで保存される (`--no-save` で止めた VM の保存ファイルが `workspace-history/` に残っていた。`--no-save` でも保存していたのは不具合で、修正 26 で直した)
  - 修正 26・27 の後 (2026-11-21、イメージ 3.0): `mvm stop --no-save` では terminate フックが保存せず、`workspaces/<名前>/latest.tar.gz` は直前の `mvm save` のまま (ETag も同じ) で、`workspace-history/` も増えなかった (test-ns)。ふだんの `mvm stop` では保存が 1 回だけになった (test-once。`workspace-history/` は 1 件、VM のログは `terminate snapshot: off` → `snapshot (terminate) skipped`)
- [ ] `--idle off` で 8 時間後に終了する (確認を取りやめた)

## F. 並列化

2026-10-29 に、クォータ (8GB) の範囲内の 2 台で確認しました。

- [x] `mvm fleet 2` で 2 台が 9 秒で起動し、両方 `bootstrap=ready` になった。レート制限 (RunMicrovm 1 回/秒) には当たらなかった
- [x] 3 台目は mvm の事前チェックで止まる。チェックを通さずに AWS に直接 `run-microvm` すると `ServiceQuotaExceededException: The base maximum allocated memory limit has been reached` で拒否された → 上限は**ベースラインメモリの合計**で決まり、事前チェックの前提と一致
- [x] 2 台に同時に `mvm send` / `mvm ssh` できた
  - 修正 8: `mvm send <name> --window shell ...` のように名前の後に書いたオプションが指示文に混ざっていた → 修正
  - 修正 9: `-` で始まる指示を tmux がオプションとして解釈していた → `send-keys -l --` で修正し、AWS 上で確認
  - 修正 10: `stop-all` 直後の `infra delete` で、終了中の VM が terminate フックで S3 に保存したためバケットが空にならず、スタックの削除に失敗した → VM が終了しきるのを待ち、失敗時はもう一度バケットを空にして再試行するように修正
- [x] `mvm watch` で全台を確認できる。手元 PC の CPU 使用率が下がったことを確認する (2026-11-21 に Windows で確認。C2 を参照)
- [ ] 1 日運用した後、Cost Explorer の実費と [cost.md](cost.md) の試算を比べる

## G. 追加機能 (2026-10-29)

- [x] キープアライブ: 定期処理 1 分、作業中とみなす時間 120 秒、アイドル 180 秒の設定で確認
  - 作業中 (画面に 10 秒ごとに出力) の VM には Lambda が毎分 `/healthz` を送り、180 秒を大きく超える 7.5 分間 RUNNING のままだった
  - 作業を止めると、120 秒後から Lambda は何も送らなくなり、その約 3 分後に SUSPENDED になった
  - 修正 11: 作業判定がイメージのスナップショット時点の時刻を引き継いでいて、起動直後の 1 回だけ「作業していない」と判定された → `/run` でリセット (再ビルドしたイメージで、最初の状態が起動時刻になることを確認)
- [x] 完了・入力待ちの通知: Claude Code の hooks と Codex の notify からの送信をローカルのテスト用 Webhook で確認 (自動テスト)。実際のエージェントからの通知は未確認 (Bedrock が使えず、トークンも未登録のため)
  - 修正 12: `~/.codex/config.toml` がまだ無いと初期化が止まる不具合 (テストで発見)
- [x] 予算アラート: テンプレートの検査 (cfn-lint) のみ。メールアドレス未設定のため作成は未確認
- [x] SSH の長時間接続: 出力のない SSH 接続が 41 秒後、4 分後、35 分後に切れることが 3 回あった (同じ条件の 150 秒 × 2 回、120 秒、15 分は切れなかった)。35 分後の切断は `PMVA_DEBUG` の記録で TCP のリセット (`Connection reset by peer`) と分かった。その後の調査で、3 回とも (02:16:19、02:30:40、03:23:26 UTC) このクラウドセッションの送信用中継が再起動した時刻 (close コード 1012 `server_draining`) と一致したため、原因は中継側で、AWS や mvm の問題ではないと判断した
  - 修正 13: WebSocket の読み書きを 1 つのロックで排他 (SSL ソケットはスレッド間で同時に読み書きすると安全でないため。予防)
  - 修正 14: `mvm ssh <name>` (tmux へのアタッチ) は、接続が切れたら自動で再接続する (中継プロセスを強制終了して確認)
  - `PMVA_DEBUG=<ファイル>` で切断理由 (close フレーム、例外) を記録できるようにした
- [x] 操作用ポリシー (`pmva-operator`) だけでの動作 (2026-11-22、Windows 11 の手元 PC、イメージ 6.0): IAM Identity Center に `pmva-operator` だけを付けた権限セットを作って割り当て、そのプロファイルで実行した (`sts get-caller-identity` が `AWSReservedSSO_pmva-operator_...`、ロールに付いたポリシーは `pmva-operator` だけ)。記録の置き場所 (`PMVA_STATE_DIR`) は空のものにし、スタックの出力やイメージの情報もキャッシュを使わずに AWS へ問い合わせさせた
  - 権限不足 (`AccessDenied`) が出ずに通った: `doctor` (「権限の確認は省略しました (iam:List* の権限なし)」と出て、ほかは OK)、`ls`、`image status`、`secret ls`、`infra outputs`、`up --agent none --no-github`、`wait`、`peek`、`ssh -- id`、`send`、`top`、`save`、`suspend`、`stop --no-save`、`cost --actual`、`logs`
  - 試していない操作 (`image build` / `prune`、`secret set` / `rm`) も、ポリシーに要る権限 (S3 への書き込み、`lambda:*Microvm*`、ビルド用ロールの `iam:PassRole`、`secretsmanager:CreateSecret` / `PutSecretValue` / `DeleteSecret`) が入っていることを確かめた
  - 以前は、クラウドセッションでは AWS へのリクエストが管理者の認証情報で署名し直されるため確認できず、IAM のポリシーシミュレーターも `lambda:PassNetworkConnector` を評価できなかった (リソースを `*` にしても拒否と出る)
- [ ] メモリのクォータ引き上げ: Service Quotas の API で申請すると「既定値 (2048) より大きい値を指定してください」と拒否された (現在値 8GB が既定値より低く抑えられているため)。AWS サポートのケースで申請する必要がある (setup.md に手順)
- [x] 実行ロールの分割 (エージェントごとに読めるシークレットを限定): 2026-10-29 にデプロイ (`UPDATE_COMPLETE`) し、IAM のポリシーシミュレーターで 4 つのロールに `secretsmanager:GetSecretValue` を試した。結果は設計どおり

  | ロール | `claude-*` | `openai-*` | `github-*` |
  |---|---|---|---|
  | `ExecutionRole` (両方) | 許可 | 許可 | 許可 |
  | `ExecutionRoleClaude` | 許可 | 拒否 | 許可 |
  | `ExecutionRoleCodex` | 拒否 | 許可 | 許可 |
  | `ExecutionRoleNone` | 拒否 | 拒否 | 許可 |

  VM の中から実際に読む確認は未実施
- [x] 毎晩の自動停止 (2026-11-20、`cron(0 23 * * ? *)`、Asia/Tokyo): 23:00:06 に Lambda `pmva-nightly-stop` が起動し、動いていた 2 台 (test-01、test-02) を保存してから終了した (`{"stopped": [...]}`、`save failed` は出ていない)。S3 の `workspaces/<名前>/latest.tar.gz` と `workspace-history/` に 23:00 の保存物があり、`mvm ls --all` で 2 台とも `TERMINATED`
  - サスペンド中の VM (2026-11-22、rt-test、イメージ 5.0): 23:00 に `SUSPENDED` だった VM も、23:00:08 に保存されてから終了した (`{"stopped": ["microvm-..."]}`、`workspaces/rt-test/latest.tar.gz` が 23:00:11)。夜の測定がこれで途中で止まったので、23 時をまたぐ確認をするときは `NIGHTLY_STOP_TIME` に気をつける
  - 修正 27: 終了のたびに 2 回保存している。Lambda (または `mvm stop`) が保存した約 3 秒後に、VM の terminate フックがもう一度保存する (test-02 は 14:00:07Z と 14:00:10Z)。保存の履歴と終了にかかる時間が倍になる → 保存に成功したら `POST /terminate-save {"save": false}` で terminate フックの保存を止めてから終了する。保存に失敗したときは止めない。`mvm stop` は実機で確認した (E の terminate フックの項)。毎晩の自動停止も 2026-11-21 23:00 に実機で確認した (test-pvm、イメージ 3.0): Lambda は 3.2 秒で `{"stopped": [...]}`、`workspace-history/` は 14:00:07Z の 1 件だけ、VM のログは `snapshot (manual) rc=0` → `terminate snapshot: off` → `snapshot (terminate) skipped`
  - 修正 26: `mvm stop --no-save` でも、VM の terminate フックが保存し、`workspaces/<名前>/latest.tar.gz` を上書きしていた (test-err、test-ws で確認)。docs/commands.md の「保存せずに終了します」と食い違い、`--no-save` の後に `--restore latest` で「最後に保存できた時点に戻る」という mvm の案内も成り立たなかった → 終了の前に `POST /terminate-save {"save": false}` で VM に伝え、terminate フックでも保存しない。イメージ 3.0 で実機で確認した (E の terminate フックの項)
- [x] Claude Code / Codex の版の固定: `image/Dockerfile` で 2.1.312 / 0.165.0 に固定し、`mvm image versions` で npm レジストリの最新版と比べられることを確認。固定した版でのイメージのビルドも 2026-11-20 に実施 (VM の中で Claude Code v2.1.312 を確認)
  - 版の更新 (2026-11-22): `mvm image update-versions` で 2.1.330 / 0.171.0 に書き換え、`mvm image build` でイメージ 5.0 を作った (約 10 分)。`mvm up ver-test --no-github` で起動し、VM の中の `claude --version` が 2.1.330、`codex --version` が 0.171.0。`mvm send` でファイルの作成と `ls` を頼むと、`mvm wait --json` が `done`、`mvm result` が応答を返し、`mvm follow --once` が指示・ツールの実行・結果・応答を表示した (会話の記録の形式は変わっていない)。既定の許可モードは 2.1.312 と同じ auto で、起動画面にその案内が出る。Codex はログインしていないので版の確認だけ
- [x] `mvm cost --actual`: 実際の Cost Explorer から取得できた (2026-10-27〜28 のアカウント全体で $0.01)
- [x] インストールスクリプト (`scripts/install.sh` / `install.ps1`): CI で Windows / macOS / Linux それぞれ 2 回実行し、エイリアスが 1 行だけ登録されることを確認
- [x] コードの見直し (2026-10-29): 次を修正し、自動テストを追加した
  - 修正 16: `mvm renew` で保存に失敗しても VM を終了し、古いスナップショットから作り直していた (作業が失われる) → 保存できなければ終了しない
  - 修正 17: `mvm stop` / `renew` がサスペンド中の VM を保存せずに終了していた → レジュームしてから保存する
  - 修正 18: 権限エラーや通信エラーで VM の状態を取れないと、終了したものとして記録していた (一覧から消え、`infra delete` も起動中の VM を見落とす) → `UNKNOWN` として扱い、終了扱いにしない
  - 修正 19: `infra delete` がリソースを削除できずに止まったとき (権限の不足など)、手作業が必要だった → 消せないリソースだけを残して残りを削除し、残したものと削除コマンドを表示する。`infra deploy` は残った Lambda 関数を先に見つけて知らせる
  - 修正 20: bootstrap の関数内で失敗すると、状態が `running` のまま止まることがあった → `set -E` で必ず `failed` にする
  - `/status?lines=` に数値以外を渡すと応答が返らなかった → 400 を返す
  - 修正 21: WebSocket の送信中はロックを持ったまま待つため、双方向に大量のデータが流れると、こちらの受信が止まり、相手と互いに待ち合って止まることがあった (Windows の CI で発生。`mvm ssh` で大きなファイルを送りながら出力を受けるときにも起こり得た) → ソケットをノンブロッキングにし、ロックを持たずに待つ。送信は専用スレッドで行う。再現テストを追加

## H. 司令塔から操作するための機能 (2026-11-15)

- [x] `mvm wait` / `result` / `--json`: 自動テスト (状態 API の代わりのサーバー、`supervisor.py` と `notify.sh` を手元で動かす結合テスト) のみ
- [x] `result` の会話記録の読み取り: Claude Code 2.1.325 の実際の会話記録 (JSONL) から、最後の応答を取り出せることを確認。Stop フックの入力に `last_assistant_message`、Notification フックの入力に `notification_type` (`idle_prompt` / `permission_prompt` など) があることを、同じ版のパッケージで確認
- [x] 実際のエージェントのフックで状態が変わること: 固定している版 (Claude Code 2.1.312 / Codex 0.165.0) を LLM API の代わりにつないで確認した (`tests/test_agents_e2e.py`、CI の `agents-e2e`)
  - Claude Code: `Stop` で完了と最後の応答 (`last_assistant_message`)。tmux の中の対話モードで、許可を求める画面で `Notification` → 入力待ち、許可するとツールが動いて完了。応答の後に 70 秒入力しなくても完了のまま
  - Codex: `codex exec` で `notify` が `agent-turn-complete` と最後の応答を渡し、完了になる
  - 2.1.312 の既定の許可モードは auto で、`echo` などの安全なコマンドは許可を求めない
- [x] `mvm follow` の会話記録の読み取り: 上の 2 つの版 (と Claude Code 2.1.325) が実際に書いた記録で、指示・思考・ツールの実行と結果・応答を取り出せることを確認 (`tests/fixtures/`)
- [x] `mvm view`: tmux 3.4 で、読み取り専用の接続になること、キー入力がエージェントに届かないこと、ほかの接続の表示中のウィンドウが変わらないこと、抜けるとセッションが消えることを確認
- [x] 司令塔 (`mvm orchestrator install`): 固定した版の Claude Code / Codex が skill を読み込むこと、Claude Code の `--dir` のフックが `npm test` を止めること、確認用の `mvm` コマンドが許可を求めずに実行されること (入れる前は「This command requires approval」) を確認 (`tests/test_agents_e2e.py`)
- [x] 実際の LLM が skill に従って、タスクを分け、起動の了承を求め、`wait` / `result` で進めること: 2026-11-22、Windows 11 の手元の Claude Code 2.1.220 (`claude -p`、`mvm orchestrator install claude --dir`) に「このリポジトリのテストを Linux の MicroVM 2 台で動かして報告して (PR は不要)」と頼んだ
  - `doctor` / `ls` で状況を確かめ、テストファイル 10 件を奇数番目・偶数番目で 2 台に分け、終わりの状態と報告の項目まで書いた指示文を作り、起動の了承を求めて止まった。了承の後に `fleet 2 --no-github` で起動し、`wait` で 2 台の完了を待ち、`result` の報告をまとめ、`stop` は実行せずに判断を求めた
  - 気づいたこと: 了承を求める前に一度 `fleet` を実行しようとし、許可の設定 (確認なしで実行できるのは確認用のコマンドだけ) で止められた。対話モードなら許可を求める画面が出る動きにあたる。追加の確認に `send` も使おうとしたが、同じく許可が要るので止まった
  - VM の作業側 (Claude Code 2.1.312、既定の許可モード) は許可待ちで止まらずに、2 台とも約 1 分でテストを終えた。結果は成功 133 / 失敗 9 / スキップ 24。失敗 9 件は `tests/test_supervisor.py` の `import tomllib` (Python 3.11 以降) が VM の Python 3.9 で使えないためで、テスト側の問題
- [x] Codex の `workspace-write` サンドボックスで `mvm` が動く設定 (`network_access`、`writable_roots`): Codex 0.165.0 で、`mvm orchestrator install codex` が表示する設定を足すと中から `mvm peek` が動き、`writable_roots` が無いと状態の保存先に書けず、`network_access` が無いと接続できないことを確認 (`tests/test_agents_e2e.py`。以前は `/tmp` に書けることをサンドボックスが効かないと見誤っていた)
- [x] イメージ (`image/Dockerfile`): 公開されているベースイメージで、Docker でビルドして動かした (amd64 は開発環境、ARM64 は CI の `image` ワークフロー)。headless の Chromium (Playwright 1.64.1) が日本語フォントで表示できること、uv で Python 3.12、`pmva-node` で Node.js 20、run フックから `.pmva/setup.sh` → エージェントの起動まで通ることを確認
  - Chromium のライブラリは GTK なしで足りる (headless の Chromium は GTK を使わない)。外すとイメージが約 290MB 小さくなった
- [x] Lambda MicroVMs の上でのイメージのビルド (`mvm image build`) と、ブラウザ・`setup.sh` の動作: ビルドは 2026-11-20 に成功 (B を参照)。2026-11-21 (イメージ 3.0、test-pvm): `--restore latest` で戻したワークスペースの `.pmva/setup.sh` (Playwright 1.64.1 と Chromium の導入) が起動時に動き、26 秒で終わってからエージェントが起動した (`bootstrap: ready`)。headless の Chromium 156 で日本語フォント (`document.fonts.check('32px "Noto Sans CJK JP"')` が true) を使ってスクリーンショットを撮れた
- [x] 手元の設定 (`mvm profile push`): 送るものの選び方と除外、VM での置き方 (自動テスト)。固定した版の Claude Code / Codex が `~/.claude/CLAUDE.md` / `~/.codex/AGENTS.md` を読むことを確認 (`tests/test_agents_e2e.py`)
- [x] S3 経由での `mvm profile push` → VM での取得: 2026-11-21、手元の 247 ファイルを送り、`mvm up --profile default` で起動した VM の `~/.claude/CLAUDE.md` が手元と SHA-256 で一致し、skill 15 個もそろった (実行ロールで `profiles/*` を読めた。bootstrap.log は `profile applied`)
- [x] 本物のイメージの中での通しの動作 (2026-11-18、Docker。`tests/test_image.py`、CI の `image` ワークフロー): MicroVM と同じ run フックから、シークレットの取得 (`aws` の代わり)、設定のまとまりの取り込み、`--claude-auth custom` / `--codex-auth custom` で LLM API の代わりにつないだ本物の Claude Code / Codex を tmux の中で起動し、両方が完了するまで
  - 状態 (完了と最後の応答)、`/result`、`/events` (mvm follow)、メモリと多く使っているプロセス (mvm top) が答えること
  - Codex の対話モード (tmux の中) での `notify` で完了になること
  - `/send` (mvm send) で次の指示を送ると、作業中 → 完了 になり、会話の記録に指示が残ること
  - 作業内容の保存に作業と設定が入り、認証情報と `~/.pmva` が入らないこと。別の VM で復元すると作業が戻り、Codex の接続先の設定と pmva のフックが重ならないこと
- [x] `mvm` のコマンドを、本物のイメージを動かす手元の Docker に対して通しで動かした (2026-11-18。`tests/test_local_vm.py`、CI の `image` ワークフロー)。受け口の代わり (トークンとポートを確かめてコンテナにつなぐ) を通して、`up` / `wait` / `result` / `follow` / `peek` / `top` / `send` / `ssh` (SSH over WebSocket) / `forward` / `login` (手元の 1455 番から VM の `codex login` に届く) / `save` / `suspend` / `resume` / `stop`、`--restore latest` での復元
  - 途中で見つけた不具合: `mvm ssh <name> --no-reconnect` (ドキュメントの書き方) で、オプションが ssh にコマンドとして渡っていた。直した
- [ ] Lambda MicroVMs の上での上記: 一部確認。2026-11-22、本物の受け口を通して `fleet` / `wait` / `result` / `stop --no-save` が動いた (上の司令塔の確認)。同じ日に、トークンの期限を過ぎても確立済みの SSH 接続が切れないこと (C)、`pmva-operator` だけでの `up` / `peek` / `ssh` / `send` / `top` / `save` / `suspend` / `stop` (G) も確認
  - サスペンド中の VM へのコマンド (2026-11-22、イメージ 5.0、`--idle 120`): `mvm suspend` の後に打つと、`result` / `follow --once` / `peek` / `send` は約 1 秒、`ssh -- <コマンド>` は約 4 秒で VM が自動で再開し、結果が返った (その後の状態は RUNNING)。`top` と `wait` は VM を起こさずに `SUSPENDED` / `suspended` を返す (作業していない VM を起こさないための仕様)。`send` → `wait` で応答が返り、その後 120 秒のアイドルで再び SUSPENDED になった
  - 修正 29: `mvm wait` は、サスペンド中の VM を「作業していない」とみなして `suspended` ですぐ返す。作業中に `mvm suspend` した上の確認では、作業の途中なのに `suspended` が返り、続けて打った `result` は「まだ応答がありません」だった。手動の `suspend` やキープアライブの無い最小構成では作業中でもサスペンドするのに、docs/commands.md は「作業中の VM はサスペンドしない」としていた → VM は起こさないまま (起こすとアイドルで止まるまで課金される)、`mvm peek` で再開して確かめる手順を表示し、`--json` の `hint` に入れる。司令塔の skill も `suspended` なら `peek` で確かめて待ち直す
- [x] メモリ不足による強制終了の検出 (`/proc/vmstat` の `oom_kill`): 2026-11-21、MicroVM のカーネルでも値が使えた。VM の中でメモリを使い切るプロセスを動かすと、そのプロセスが強制終了され (終了コード 137)、`oom_kill` が 0 → 1 になり、`mvm top` / `mvm peek` に「メモリ不足でプロセスが 1 回強制終了されました」と出た
  - 気づいたこと: `MEMORY_MIB=4096` で起動した VM でも、VM の中からはメモリが約 15.7 GiB (`MemTotal` 16,437,604 kB)、CPU が 8 個に見えた。料金と上限が設定のどちらに従うのかは未確認 ([cost.md](cost.md) の試算は `MEMORY_MIB` に基づく)

## I. AWS の認証方式の追加 (2026-11-16)

- [x] `bootstrap.sh` が方式ごとに書く設定 (環境変数、`~/.codex/config.toml` の接続先、キーのファイルの権限 600、起動し直したときの書き直し、利用者が自分で書いた `model_provider` を残すこと): 自動テスト (`tests/test_supervisor.py`)
- [x] 固定した版の Claude Code 2.1.312 / Codex 0.165.0 が、その設定で AWS の認証を付けて送ること: 接続先を LLM API の代わりに差し替えて確認 (`tests/test_agents_e2e.py`)
  - Claude Code の `mantle`: `bedrock-mantle` の SigV4 署名。Claude Platform on AWS: 署名と `anthropic-workspace-id`、キーがあれば `x-api-key` (署名より優先)。Bedrock の API キー: `Authorization: Bearer` (応答のイベントストリームは偽の API で返せないので、要求だけを確認)
  - Codex の `bedrock`: `bedrock-mantle` の SigV4 署名と、設定したリージョン。`bedrock-api-key`: ファイルのキーを `Authorization: Bearer` で送り、環境変数の Claude Code 用のキーより優先。tmux の中の対話モードでも、ログイン画面を出さずに動く
- [ ] MicroVM の実行ロールの認証情報で、Codex (Rust の AWS SDK) が Bedrock を呼べること: 未確認 (Claude Code は 2026-10-28 に Bedrock まで届くことを確認済み)
- [ ] 実際の Bedrock の Mantle で、OpenAI のモデル (Codex) と Claude (`mantle`) が使えること、必要な IAM の権限 (`bedrock-mantle:CreateInference` など) が足りること: 未確認 (モデルの利用申請が要る)
- [ ] Claude Platform on AWS: 未確認 (AWS Marketplace での購読とワークスペースが要る)
- [ ] Bedrock の API キー: 未確認 (キーの発行が要る)
- [x] `mvm login` の前提: Codex 0.165.0 の `codex login` が `127.0.0.1:1455` で戻りを待ち (戻り先は `http://127.0.0.1:1455/auth/callback`)、ログインの URL を表示することを確認 (`tests/test_agents_e2e.py`)。`mvm login` が 1455 番をつなぎ、表示された URL をブラウザで開くことは自動テスト
- [ ] 実際の ChatGPT / Claude のアカウントで、`mvm login` から VM の中のエージェントにログインできること: 未確認 (MicroVM とアカウントが要る)
- [x] Codex の `gateway`: 固定した版の Codex が、ファイルのキーを `Authorization: Bearer` で、`apiKeyHeader` のときは環境変数のキーを指定のヘッダーで送り、URL のパラメータと追加のヘッダーを付けることを確認 (`tests/test_agents_e2e.py`)
- [ ] Codex の `access-token`: 未確認 (トークンの発行と、ChatGPT への確認の通信が要る)
- [x] `custom`: `bootstrap.sh` が環境変数・ファイル (権限 600)・Claude Code の設定・Codex の接続先を書くこと、`{{files}}` の置き換え、使えない名前を飛ばすことを確認 (`tests/test_supervisor.py`)。中身はエージェントの設定そのものなので、本物のエージェントでは確かめていない

## J. 自動メモリの共有 (2026-11-23)

- [x] `mvm memory` の 3 者比較、`MEMORY.md` の行のマージ、送らないもの (`share: false`、認証情報らしい文字列、1MB 超)、`--dry-run` / `--force`、repo-id の正規化とメモリのフォルダの解決: 自動テスト (`tests/test_memsync.py`、`tests/test_mvm.py`。AWS は `tests/aws_stub.py` の代わり)
- [x] VM で `bootstrap.sh` が `autoMemoryDirectory` を設定して `memsync.py pull` し、起動した Claude Code が取得したメモリを読むこと: 2026-11-23 に実機で確認 (イメージ 7.0、Claude Code の oauth-token)。手元から `mvm memory push` した 10 件と `MEMORY.md` が `~/.claude/pmva-memory/` に置かれ、`~/.claude/settings.json` の `autoMemoryDirectory` がそこを指した。`mvm up --memory --prompt` で「ファイルを調べずにメモリだけで答えて」と聞くと、手元で書いたマージの決まりと Linux のテストの動かし方を索引から答えた
- [ ] 同上のうち、VM の Claude Code の `/memory` に取得したメモリが出ること: 未確認 (画面を開いていない)
- [x] `snapshot.sh` (`mvm save` / `mvm stop`) が `memsync.py push` で VM で書いたメモリを S3 に送り、手元で `mvm memory pull` して読めること: 2026-11-23 に実機で確認。VM の Claude に覚えさせた 1 件が `mvm save` で送られ (索引の 1 行も)、手元の `pull` で取得 1・索引に 1 行追加。中身は UTF-8 / LF のまま
- [ ] 同上のうち、送れなくても保存が続くこと: 未確認 (送信を失敗させる状況を実機で作っていない。自動テストのみ)
- [x] `mvm infra deploy` で実行ロール (`memory/*` の Get / Put、`ListBucket` の `memory/*`) を更新した後に、VM から取得・送信できること: 2026-11-23 に確認。ポリシー `pmva-microvm-runtime` に `SharedAgentMemory` と `memory/*` の一覧が入り、上の取得・送信が通った
- [ ] 同上のうち、更新前は AccessDenied の案内が出て、起動は止まらないこと: 未確認 (確認の前にスタックを更新したため)
- [x] `mvm renew` で復元した VM が、`~/.claude/pmva-memory` と状態ファイルの復元後に、手元の変更だけを取得すること: 2026-11-23 に確認。手元で 1 件に追記して `push` した後に `renew` すると、新しい VM ではその 1 件だけが書き直され (ほかは復元時の更新時刻のまま)、追記が届いた。`renew` の保存時の送信は「S3 のほうが新しい」として VM の古い内容で上書きしなかった。`--restore latest` は `renew` と同じ復元の経路のため個別には試していない
- [x] S3 の ETag が MD5 と一致すること (バケットが SSE-S3 の単一 PUT): 2026-11-23 に確認。`head-object` の ETag と、手元のファイルを LF にそろえた MD5 が一致 (`ServerSideEncryption` は `AES256`)。`push` 直後の `status` もすべて「同期済み」

### J2. フックによる自動の同期 (段階 2)

- [x] フックの処理 (書いたら送る、会話の開始時と指示のたびに取得して会話に渡す、60 秒の間引き、ロック、失敗しても 0 で終わる)、条件付き書き込み (他のマシンの書き込みを上書きしない、`MEMORY.md` の取り直し、古い AWS CLI での退避)、`mvm memory install-hooks` / `uninstall-hooks` / `hook`: 自動テスト (`tests/test_memsync.py`、`tests/test_mvm.py`)。VM の `settings.json` にフックが入ること・外れることは `tests/test_supervisor.py` (Linux)
- [x] 本物の S3 で `put-object --if-match <ETag>` / `--if-none-match "*"` が通り、書き換えられた後は `PreconditionFailed` になること: 2026-11-23 に確認 (手元の aws-cli 2.37.9、バケットは SSE-S3)。`--if-none-match "*"` は新規なら通り、既にあると `PreconditionFailed`。引用符なしの ETag を渡した `--if-match` は通り、書き換えた後に古い ETag を渡すと `PreconditionFailed`。引用符つきの ETag でも通る
- [x] VM の AWS CLI (Amazon Linux 2023 の `awscli-2`) が `--if-match` を知っていること: 2026-11-23 に確認。イメージ 8.0 の VM は aws-cli 2.38.0 で、`put-object` の `--if-match` / `--if-none-match` がある。VM の失敗の記録 (`pmva-memory.state.json.log`) は作られなかった
- [x] VM で Claude Code がメモリを書いた直後に S3 に届くこと (PostToolUse、`async`): 2026-11-23 に確認 (イメージ 8.0、`mvm up --memory`)。`mvm send` でメモリを 1 件書かせると、送ってから 4 秒で S3 に届いた (メタデータ `pmva-origin=j2`)。索引の 1 行も S3 の `MEMORY.md` に入った
- [x] 手元で `install-hooks` した会話に、VM が書いたメモリが次の指示のときに届くこと (UserPromptSubmit の `additionalContext`): 2026-11-23 に確認。手元の `claude -p --input-format stream-json` で会話を開いたまま VM に書かせ、60 秒の間引きを越えてから次の指示で「ツールを使わずに、届いたメモリのファイル名と合言葉を答えて」と聞くと、両方を答えた。手元のメモリのフォルダにもファイルと索引の行が置かれた。対話の画面 (`claude` を端末で開いた会話) では試していない
- [x] 逆向き (手元で書いたら送り、VM の会話に次の指示で届く): 2026-11-23 に確認。手元の `claude -p` に同じメモリへ 1 行追記させると 1 秒ほどで S3 に届き、VM の会話で次の指示を送ると、追記の合言葉を答えた
- [x] Windows の手元で、フックが `"<python.exe>" "<mvm.py>" memory hook ...` を動かせること: 2026-11-23 に確認 (Windows 11、Claude Code 2.1.220、Python 3.14)。SessionStart のフックが終了コード 0 で「取得 0、送信 0、…」を返し、上の UserPromptSubmit・PostToolUse も動いた
- 確認中に見つけた別の不具合: `mvm up --claude-args "--dangerously-skip-permissions"` で起動すると、Claude Code が許可を省くことの確認画面 (既定は「No, exit」) で止まり、`mvm send` の Enter で終了した。#64 で修正済み (実機での確認は C 節の項目)

#### 手動の同期と `snapshot.sh` もフックと同じロックを取る (#68)

- [x] 手動の `pull` / `push` / `rm` / `revert` と `snapshot.sh` の送信がロックを取ること、ロックを待つ・待ちきれずに止まること、`--lock-wait`、長い同期でロックの時刻を新しくすること: 自動テスト (`tests/test_memsync.py` の `CliLockTest`、`ImageWiringTest`)
- [x] 他の同期がロックを持っていると、`snapshot.sh` はメモリの送信を 10 秒であきらめ、ワークスペースの保存は続けること: 2026-11-24 に確認 (イメージ 10.0、`mvm up --memory`)。VM の中で新しいロックを置いてから `snapshot.sh` を動かすと、「最長 10 秒」待ってから警告を出し、ワークスペースを保存して終了コード 0 で終わった (12.4 秒)。送れなかったメモリは `status` で「手元だけ」のまま残り、次の送信で送られた
- [x] フックと `snapshot.sh` がほぼ同時に動いても、状態ファイルが壊れないこと: 2026-11-24 に確認。どちらを先に動かしても、後の方がロックの外れるのを待ってから送った。フックの送信 (6 件、5 秒) 中に `snapshot.sh` を動かすと、`snapshot.sh` は待った後に「送信 0」。`snapshot.sh` の送信 (2 件) 中にフックを動かすと、フックは待った後に自分の 1 件を送った。どちらの後も、状態ファイルは JSON として読めて全件の記録があり、`*.pmva-tmp` もロックも残らず、`status` はすべて「同期済み」
- [x] VM の Claude がメモリを書いた直後の `mvm stop` で、メモリの送信とワークスペースの保存が両方済むこと: 2026-11-24 に確認。`mvm send` で Claude にメモリを 1 件書かせ、ファイルができた直後に `mvm stop` すると、保存はフックの送信を待ってから続き、5 秒で終了した。メモリ本体はフックで、索引の 1 行は保存のときの送信で S3 に届いた。`workspace-history/` は保存 1 回分だけ増え、終了時のフックは保存を飛ばした
- [x] 終了時のフックの保存 (`mvm stop` を通さない終了。50 秒で打ち切り) で、ロックが空かなくてもワークスペースを保存できること: 2026-11-24 に確認。VM の中で新しいロックを置いてから `terminate-microvm` を直接呼ぶと、フックの開始 (13:28:16) から 10 秒後にメモリの送信をあきらめて tar を作り (13:28:26)、13 秒で保存を終えた (`snapshot (terminate) rc=0`)。`workspaces/<名前>/latest.tar.gz` と `workspace-history/` に保存が 1 件ずつできた。ロックのせいで送れなかったメモリは S3 に届かない (ワークスペースの保存には入る)

### J3. 履歴・戻す・消す (段階 3)

- [x] 送るたびの履歴 (`.history/<名前>/<時刻>-<送り元>-<md5 先頭 8 文字>.md` のキー、`pmva-origin` のメタデータ、`MEMORY.md` と送らなかったファイルと `--dry-run` では書かない、書けなくても同期は止まらない): 自動テスト (`tests/test_memsync.py` の `HistoryTest`)
- [x] `mvm memory log` (表、`--origin`、`--since` の相対・日時指定、`--json`、削除の行、ページング、履歴が無いときの案内)、`revert` (既定の 1 つ前、`--to`、未同期の変更で止まる、`--dry-run`、印を外して索引の行を戻す、他のマシンの書き込みとぶつかったとき)、`rm` (印 → S3 のファイル → S3 の索引 → 手元の順、条件付き書き込みのぶつかり、`--dry-run`、AccessDenied の案内)、`mvm memory` の引数の受け渡し: 自動テスト (`tests/test_memsync.py` の `LogTest` / `RevertTest` / `RmTest` / `AdminCliTest`、`tests/test_mvm.py`)
- [x] 削除の印の伝わり方 (`pull` が手元のファイルと索引の行を消す、消された後に書き換えたものは消さない、S3 に送り直された中身を取得しない、`push` が送らない、`status` の表示、フックが会話に伝える文、`rm` した PC と別のマシンの往復): 自動テスト (`tests/test_memsync.py` の `DeletedMarkTest` / `RmTest`)
- [x] 履歴も印も無い (段階 3 より前の) S3 でも、今までどおり同期できること: 自動テスト
- [x] 本物の S3 で、`push` のたびに `.history/` に写しが残り (メタデータ付き)、`mvm memory log` / `revert` で見る・戻せること: 2026-11-24 に確認 (Windows 11、aws-cli 2.37.9、操作用の `pmva-operator`)。試験用のメモリを 2 版 `push` すると `.history/j3-test.md/20261124T125302Z-local-03177d17.md` のようなキーが版ごとにでき、`head-object` のメタデータは `pmva-origin=local`。`log j3-test.md` が 2 行を古い順に出し、`revert` で 1 つ前 (v1) に、続けて `revert` すると v2 に戻った (取り消し)。どちらも手元と S3 の中身がそろい、`status` は「同期済み」
- [x] 本物の S3 で `mvm memory rm` が通り (操作用ポリシーの `s3:DeleteObject`)、VM の実行ロールでは AccessDenied になること: 2026-11-24 に確認。手元の `rm j3-test.md` で `.deleted.json` に印 (md5、時刻、`by: local`、索引の行) が書かれ、S3 のファイル、S3 と手元の `MEMORY.md` の行、手元のファイルが消えた。`log` には「消した」の行が出た。VM の中で `memsync.py rm` を動かすと、印を書いた後の `DeleteObject` が実行ロール `pmva-microvm-execution-claude` の AccessDenied で止まり、「手元の PC で実行してください」の案内と終了コード 1 を返した。その後、手元で同じ名前を `rm` すると続きから消せた
- [x] `rm` したメモリが、VM の会話から消えること (VM の次の指示で「他のマシンで消されたメモリ」が会話に届き、メモリのフォルダと索引から消える): 2026-11-24 に確認 (イメージ 9.0、`mvm up --memory`)。VM の Claude が `j3-test.md` の合言葉を読んだ後に手元で `rm` し、60 秒の間引きを越えてから `mvm send` で聞くと、会話に「他のマシンで消されたメモリ: j3-test.md (手元からも消しました。この記憶には頼らないでください)」が届き、Claude は「もう頼ってはいけない記憶」と答えた。VM の `~/.claude/pmva-memory/` からファイルが、`MEMORY.md` から行が消えていた
- [x] 古いイメージ (8.0 以前) の VM の振る舞い (`rm` したメモリを送り直すこと、新しい版の `pull` / `push` がそれを取り込まないこと): 2026-11-25 に確認。残っていたイメージ 8.0 (`memsync.py` は b3e3fab の版と md5 が一致) で `mvm up --agent none --memory` し、起動時の取得で試験用のメモリが VM に届いた後に、手元で `rm` した。VM の古い `memsync.py` は `status` で「手元だけ」と出し、`push` で S3 に送り直した (`.deleted.json` と `.history/` は一覧で飛ばすので読まない)。その後の手元の新しい版は、`pull` が「mvm memory rm で消されたメモリの内容が S3 に残っています。取得しません」と出して飛ばし (飛ばした 1)、`push` は何も送らず、手元のファイルも戻らなかった。`status` の表示は「S3 だけ」で、消されたものとは区別されない。古い VM は `.history/` に書かないので、送り直しは `mvm memory log` に出ない。`mvm stop` の保存でも送り直すため、片付けは VM の中のファイルを消してから止め、手元で `rm` し直した
- [x] 本物の S3 で、`list-objects-v2 --delimiter /` が `memory/<repo-id>/` 直下のキーだけを返し、`.history/` を取ってこないこと (同期の一覧が履歴の数で遅くならない): 2026-11-24 に確認。`Contents` は直下の 12 件だけで `.history/` 配下を含まず、`.history/` は `CommonPrefixes` に 1 件だけ出た (Git Bash から試すときは `MSYS_NO_PATHCONV=1` を付けないと `/` がパスに書き換わり、区切りが効かないように見える)
- [x] VM が `.history/` に書けること (実行ロールの `s3:PutObject` が `memory/*` に効いていること。AccessDenied のときは警告だけで同期が続くこと): 2026-11-24 に確認。VM の Claude にメモリを 1 件書かせると、フックの送信で `.history/j3-vm.md/20261124T125802Z-j3-e20a9608.md` ができ、手元の `log` に書いたマシン `j3` として出た。AccessDenied のとき: 2026-11-25 に確認 (イメージ 11.0、`mvm up --agent none --memory`)。実行ロール `pmva-microvm-execution-none` に `memory/*/.history/*` の `s3:PutObject` を拒否するインラインポリシーを一時的に付け、VM の中で `memsync.py push` と `hook post-tool` を動かした。どちらもメモリ本体は送り、「履歴を残せませんでした (… AccessDenied …)。同期は続けます」の警告を出して終了コード 0 で終わり、`status` は「同期済み」、手元の会話にも届いた。警告の文は「memory/ の権限がありません。mvm infra deploy でスタックを更新してください」と案内するが、この状況 (本体は書けて履歴だけ拒否) ではスタックの更新では直らない。確認後にインラインポリシーを外した

## K. 少ない RAM での並列と Remote Control の下見 (2026-11-25)

`MEMORY_MIB=2048` でイメージ 11.0 を作り、1 台 (`rc-probe`、`--claude-auth oauth-token`、`--claude-args "--remote-control"`) を約 1 時間動かした。Claude Code は 2.1.330。

- [x] `MEMORY_MIB=2048` のイメージで起動できること: `mvm up` が `2048MiB` で起動し、bootstrap は ready になった。VM の中から見える全体のメモリは約 7.8GiB (4096 のときは約 15.7GiB)。設定値の約 4 倍で、設定値に比例する
- [x] `--claude-args "--remote-control"` だけでは Remote Control が有効にならないこと: 画面に何も出ず、`/remote-control` を打つと「`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` が設定されている間は使えない」と出た。この変数は `image/Dockerfile` の `ENV` で入れている
- [x] setup-token のトークン (`CLAUDE_CODE_OAUTH_TOKEN`) では Remote Control が使えないこと: 上の変数を外した claude で `/remote-control` を打つと「Remote Control requires a full-scope login token. Long-lived tokens (from claude setup-token or CLAUDE_CODE_OAUTH_TOKEN) are limited to inference-only」と出た
- [x] VM の中で `claude auth login --claudeai` (URL を手元のブラウザで開き、表示されたコードを VM に貼る) でログインし直せば Remote Control が有効になること: `CLAUDE_CODE_OAUTH_TOKEN` と `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` を外して `claude --remote-control pmva-rc-probe` で起動すると、「/remote-control is active」と `https://claude.ai/code/session_...` が出た。ログインの認証情報 (`.claude/.credentials.json`) は `snapshot.sh` が保存から外している
- [x] claude.ai/code とスマホの Claude アプリの両方にセッションが出て、claude.ai/code から送った指示が VM で動き、返事が戻ること
- [x] Remote Control の接続があっても、作業が終わればサスペンドされること: VM の最後の作業 (`lastActivity`) は 00:00:38 JST。keepalive が起こし続ける 15 分 (`KEEPALIVE_ACTIVE_SECONDS=900`) と、外からの通信が無い 30 分 (`IDLE_MAX_SECONDS=1800`) を経て、00:43:56 JST に `SUSPENDED` になった。Remote Control の接続は VM から外向きなので、アイドルの判定を妨げない。状態は `mvm ls` (AWS の API だけで VM に通信しない) で 2 分ごとに見た
- [ ] サスペンド中に claude.ai/code やスマホから指示を送ったときの振る舞い (外向きの接続なので VM は起きないはず)、レジューム後に Remote Control がつなぎ直すか: 未確認
- [ ] 2GB × 4 台 × 8 時間で、典型的な作業 (テスト、ブラウザ) が終わるか (`mvm watch --log` の `peakMiB` と `oomKills`)、料金が `MEMORY_MIB` に従うか: 未確認 (本番の測定)

分かったこと: Remote Control を正式に足すには、(1) その VM だけ `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` を外す、(2) Claude Code の認証を完全な権限のログインにする、の 2 つが要る。`oauth-token` のままでは使えない。(2) は `--claude-auth interactive` と `mvm login` で足りる見込みだが、その組み合わせでは試していない。

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
| 1 日あたりの実費 | `mvm cost --actual` で 2026-10-27〜28 のアカウント全体が $0.01 (PoC の VM は短時間のみ。Lambda MicroVMs の行はまだ出ていない)。2 台 × 8 時間の実費は未計測。2026-10-24〜11-21 の実費 (Lambda $2.84、4GB の VM 換算で約 7.1 時間) と試算の比較は [cost.md](cost.md) の「実費との比較」 に記入 (2026-11-22) |
| PoC 中に直した不具合 | 10 件: gh のインストール、validate フック、/run の本文形式、mvm ssh の引数、tmux の日本語、mvm shell の制御メッセージと端末復元、mvm logs (AWS CLI v1 対応)、mvm send のオプション、`-` で始まる指示、infra delete の削除順。ほかに `mvm ls` の時刻表示 |
| 本番前に必要な対応 | `pmva-operator` だけを付けた IAM ユーザーか Identity Center の権限セットの用意 (このポリシーだけで日常の操作が通ることは 2026-11-22 に確認済み)、Bedrock を使うならモデル利用申請 (同時に動かす台数を増やしたい場合だけ、クォータの引き上げ) |
| 片付け | 2026-10-28・29 とも `mvm stop-all`、`mvm infra delete`、`mvm image delete` を実施 |
| 自動テスト (CI) | GitHub Actions で Windows / macOS (Python 3.9・3.13)、Linux (3.13) のテストと lint が成功。端末の表示や VS Code など目で見る項目は C2 に残る |
