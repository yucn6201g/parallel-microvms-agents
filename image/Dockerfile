# Lambda MicroVMs 用エージェント実行イメージ (ARM64 / Amazon Linux 2023)
#
# このディレクトリ全体が zip 化されて S3 にアップロードされ、
# `mvm image build` (CreateMicrovmImage / UpdateMicrovmImage) でビルドされる。
# ビルド完了時点のディスクとメモリがスナップショットされ、全 MicroVM で共有されるため、
# シークレットやホスト鍵などの「VM ごとに一意な状態」はここでは作らない (bootstrap.sh で /run 後に作る)。
FROM public.ecr.aws/lambda/microvms:al2023-minimal

ARG NODE_VERSION=22.20.0
ARG WEBSOCAT_VERSION=1.13.0
ARG UV_VERSION=0.13.6
# エージェントの版は固定する (全 VM で同じ版を使い、更新はビルドし直したときだけ)。
# 最新版の確認と書き換えは mvm image versions / mvm image update-versions で行う
ARG CLAUDE_CODE_VERSION=2.1.312
ARG CODEX_VERSION=0.165.0
ARG INSTALL_GH=true
ARG GH_VERSION=2.103.3
# ブラウザ (Playwright / Puppeteer の Chromium) を動かすためのライブラリと日本語フォント。
# mvm image build が config.env の IMAGE_BROWSER_DEPS / IMAGE_EXTRA_PACKAGES でこの 2 行を書き換える
ARG INSTALL_BROWSER_DEPS=true
ARG EXTRA_PACKAGES=
# CPU の種類。Lambda MicroVMs は ARM64 でビルドする。CI では amd64 でもビルドして確かめる (BuildKit が自動で渡す)
ARG TARGETARCH

USER root

# 基本ツール (al2023-minimal は dnf が無い場合 microdnf を使う)
RUN PM="$(command -v dnf || command -v microdnf)" \
 && "$PM" install -y \
      git tmux jq python3 tar gzip xz unzip zip \
      procps-ng findutils which less vim-minimal diffutils patch \
      openssh-server openssh-clients shadow-utils util-linux awscli-2 \
      make gcc gcc-c++ \
 && "$PM" clean all \
 && rm -rf /var/cache/dnf /var/cache/yum

# ブラウザの実行に必要なライブラリ (Playwright が Ubuntu で headless の Chromium 用に入れるものの Amazon Linux 2023 での名前。
# GTK は画面を表示するときだけ要るので入れない) と、
# スクリーンショットで日本語が豆腐にならないためのフォント。ブラウザ本体は版がプロジェクトごとに違うので入れず、
# リポジトリの .pmva/setup.sh で `npx playwright install chromium` する (docs/setup.md の「リポジトリごとの準備」)
RUN if [ "$INSTALL_BROWSER_DEPS" = "true" ]; then \
      PM="$(command -v dnf || command -v microdnf)" \
      && "$PM" install -y \
           nss nspr atk at-spi2-atk at-spi2-core cups-libs libdrm dbus-libs libxcb libxkbcommon \
           libX11 libXcomposite libXdamage libXext libXfixes libXrandr \
           mesa-libgbm pango cairo alsa-lib fontconfig \
           google-noto-sans-cjk-ttc-fonts dejavu-sans-fonts liberation-fonts \
      && "$PM" clean all \
      && rm -rf /var/cache/dnf /var/cache/yum; \
    fi

# 利用者が追加するパッケージ (例: postgresql16-server valkey sqlite)。MicroVM の中では root になれないので、ここで入れる
RUN if [ -n "$EXTRA_PACKAGES" ]; then \
      PM="$(command -v dnf || command -v microdnf)" \
      && "$PM" install -y $EXTRA_PACKAGES \
      && "$PM" clean all \
      && rm -rf /var/cache/dnf /var/cache/yum; \
    fi

# GitHub CLI (任意)。公式 rpm リポジトリは AL2023 の dnf で署名検証に失敗するため、リリースの tar.gz を checksum 付きで入れる
RUN if [ "$INSTALL_GH" = "true" ]; then \
      arch="$([ "${TARGETARCH:-arm64}" = amd64 ] && echo amd64 || echo arm64)" \
      && cd /tmp \
      && curl -fsSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_${arch}.tar.gz" \
      && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt" \
           | grep " gh_${GH_VERSION}_linux_${arch}.tar.gz\$" | sha256sum -c - \
      && tar -xzf "gh_${GH_VERSION}_linux_${arch}.tar.gz" \
      && install -m 0755 "gh_${GH_VERSION}_linux_${arch}/bin/gh" /usr/local/bin/gh \
      && rm -rf gh_* \
      && gh --version; \
    fi

# Node.js (公式バイナリ。SHASUMS256 で整合性確認)。別の版が要るリポジトリは pmva-node で入れる
RUN arch="$([ "${TARGETARCH:-arm64}" = amd64 ] && echo x64 || echo arm64)" \
 && cd /tmp \
 && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz" \
 && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" \
      | grep " node-v${NODE_VERSION}-linux-${arch}.tar.xz\$" | sha256sum -c - \
 && tar -xJf "node-v${NODE_VERSION}-linux-${arch}.tar.xz" -C /usr/local --strip-components=1 --no-same-owner \
 && rm -f "node-v${NODE_VERSION}-linux-${arch}.tar.xz"

# uv (Python のパッケージと版の管理。`uv python install 3.12`、`uv sync`、`uv run pytest`)。公開されている sha256 で確認
RUN arch="$([ "${TARGETARCH:-arm64}" = amd64 ] && echo x86_64 || echo aarch64)" \
 && cd /tmp \
 && file="uv-${arch}-unknown-linux-gnu.tar.gz" \
 && curl -fsSLO "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${file}" \
 && curl -fsSL "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${file}.sha256" | sha256sum -c - \
 && tar -xzf "$file" \
 && install -m 0755 "uv-${arch}-unknown-linux-gnu/uv" "uv-${arch}-unknown-linux-gnu/uvx" /usr/local/bin/ \
 && rm -rf uv-* \
 && uv --version

# Claude Code / Codex CLI
# 自動アップデートは無効化しているため、更新したい場合はイメージを再ビルドする。
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force \
 && claude --version && codex --version

# websocat (SSH over WebSocket ブリッジ用)
RUN arch="$([ "${TARGETARCH:-arm64}" = amd64 ] && echo x86_64 || echo aarch64)" \
 && curl -fsSL -o /usr/local/bin/websocat \
      "https://github.com/vi/websocat/releases/download/v${WEBSOCAT_VERSION}/websocat.${arch}-unknown-linux-musl" \
 && chmod 0755 /usr/local/bin/websocat \
 && websocat --version

# 実行ユーザー (Claude Code の --dangerously-skip-permissions は root では使えないため非 root)
# MicroVM のコンテナには no_new_privs が付いているため sudo は使えない (PoC で確認)。
# root が必要な作業は mvm shell (root で入る) から行うか、必要なパッケージをこのイメージに追加する。
RUN useradd -m -u 1000 -s /bin/bash agent

COPY supervisor.py bootstrap.sh snapshot.sh notify.sh /opt/pmva/
COPY agent-attach pmva-node /usr/local/bin/
COPY tmux.conf /home/agent/.tmux.conf
COPY bashrc.pmva /home/agent/.bashrc.pmva
RUN chmod 0755 /opt/pmva/supervisor.py /opt/pmva/bootstrap.sh /opt/pmva/snapshot.sh /opt/pmva/notify.sh \
      /usr/local/bin/agent-attach /usr/local/bin/pmva-node \
 && echo '[ -f ~/.bashrc.pmva ] && . ~/.bashrc.pmva' >> /home/agent/.bashrc \
 && mkdir -p /home/agent/work /home/agent/.pmva \
 && chown -R agent:agent /home/agent

ENV DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    LANG=C.UTF-8 \
    TERM=xterm-256color

USER agent
WORKDIR /home/agent

# 8080: 状態/操作 API (peek / send / snapshot)
# 9000: ライフサイクルフック
# 7681: SSH over WebSocket ブリッジ
EXPOSE 8080 9000 7681
CMD ["python3", "/opt/pmva/supervisor.py"]
