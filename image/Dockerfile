# Lambda MicroVMs 用エージェント実行イメージ (ARM64 / Amazon Linux 2023)
#
# このディレクトリ全体が zip 化されて S3 にアップロードされ、
# `mvm image build` (CreateMicrovmImage / UpdateMicrovmImage) でビルドされる。
# ビルド完了時点のディスクとメモリがスナップショットされ、全 MicroVM で共有されるため、
# シークレットやホスト鍵などの「VM ごとに一意な状態」はここでは作らない (bootstrap.sh で /run 後に作る)。
FROM public.ecr.aws/lambda/microvms:al2023-minimal

ARG NODE_VERSION=22.20.0
ARG WEBSOCAT_VERSION=1.13.0
ARG CLAUDE_CODE_VERSION=latest
ARG CODEX_VERSION=latest
ARG INSTALL_GH=true
ARG GH_VERSION=2.103.3

USER root

# 基本ツール (al2023-minimal は dnf が無い場合 microdnf を使う)
RUN PM="$(command -v dnf || command -v microdnf)" \
 && "$PM" install -y \
      git tmux jq python3 tar gzip xz unzip zip \
      procps-ng findutils which less vim-minimal diffutils patch \
      openssh-server openssh-clients shadow-utils sudo awscli-2 \
      make gcc gcc-c++ \
 && "$PM" clean all \
 && rm -rf /var/cache/dnf /var/cache/yum

# GitHub CLI (任意)。公式 rpm リポジトリは AL2023 の dnf で署名検証に失敗するため、リリースの tar.gz を checksum 付きで入れる
RUN if [ "$INSTALL_GH" = "true" ]; then \
      cd /tmp \
      && curl -fsSLO "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_linux_arm64.tar.gz" \
      && curl -fsSL "https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_checksums.txt" \
           | grep " gh_${GH_VERSION}_linux_arm64.tar.gz\$" | sha256sum -c - \
      && tar -xzf "gh_${GH_VERSION}_linux_arm64.tar.gz" \
      && install -m 0755 "gh_${GH_VERSION}_linux_arm64/bin/gh" /usr/local/bin/gh \
      && rm -rf gh_* \
      && gh --version; \
    fi

# Node.js (ARM64 公式バイナリ。SHASUMS256 で整合性確認)
RUN cd /tmp \
 && curl -fsSLO "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-arm64.tar.xz" \
 && curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" \
      | grep " node-v${NODE_VERSION}-linux-arm64.tar.xz\$" | sha256sum -c - \
 && tar -xJf "node-v${NODE_VERSION}-linux-arm64.tar.xz" -C /usr/local --strip-components=1 --no-same-owner \
 && rm -f "node-v${NODE_VERSION}-linux-arm64.tar.xz"

# Claude Code / Codex CLI
# 自動アップデートは無効化しているため、更新したい場合はイメージを再ビルドする。
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "@openai/codex@${CODEX_VERSION}" \
 && npm cache clean --force \
 && claude --version && codex --version

# websocat (SSH over WebSocket ブリッジ用)
RUN curl -fsSL -o /usr/local/bin/websocat \
      "https://github.com/vi/websocat/releases/download/v${WEBSOCAT_VERSION}/websocat.aarch64-unknown-linux-musl" \
 && chmod 0755 /usr/local/bin/websocat \
 && websocat --version

# 実行ユーザー (Claude Code の --dangerously-skip-permissions は root では使えないため非 root)
# MicroVM 自体が隔離環境なので、エージェントがパッケージを追加できるよう sudo を許可する。
RUN useradd -m -u 1000 -s /bin/bash agent \
 && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
 && chmod 0440 /etc/sudoers.d/agent

COPY supervisor.py bootstrap.sh snapshot.sh /opt/pmva/
COPY agent-attach /usr/local/bin/agent-attach
COPY tmux.conf /home/agent/.tmux.conf
COPY bashrc.pmva /home/agent/.bashrc.pmva
RUN chmod 0755 /opt/pmva/supervisor.py /opt/pmva/bootstrap.sh /opt/pmva/snapshot.sh /usr/local/bin/agent-attach \
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
