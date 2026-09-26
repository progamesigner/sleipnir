ARG COMPOSER_VERSION=latest
ARG UBUNTU_VERSION=latest
ARG UV_VERSION=latest

FROM ubuntu:${UBUNTU_VERSION} AS devcontainers

ARG DEVCONTAINERS_REF=main

RUN apt-get update \
 && apt-get install --no-install-recommends --yes ca-certificates git \
 && rm -rf /var/lib/apt/lists/*

RUN git init --quiet /tmp/devcontainers \
 && git -C /tmp/devcontainers fetch --quiet --depth=1 https://github.com/progamesigner/devcontainers.git "${DEVCONTAINERS_REF}" \
 && git -C /tmp/devcontainers checkout --quiet FETCH_HEAD

FROM ubuntu:${UBUNTU_VERSION} AS fetcher

RUN apt-get update \
 && apt-get install --no-install-recommends --yes ca-certificates curl tar unzip xz-utils \
 && rm -rf /var/lib/apt/lists/*

FROM ubuntu:${UBUNTU_VERSION} AS installer

RUN apt-get update \
 && apt-get install --no-install-recommends --yes ca-certificates curl gnupg jq tar unzip xz-utils \
 && rm -rf /var/lib/apt/lists/*

FROM fetcher AS s6

ARG TARGETARCH

ARG S6_OVERLAY_VERSION=none

RUN case "${TARGETARCH}" in \
        amd64) S6_ARCH=x86_64 ;; \
        arm64) S6_ARCH=aarch64 ;; \
        *) echo "unsupported architecture: ${TARGETARCH}" >&2 ; exit 1 ;; \
    esac \
 && mkdir -p /opt/s6 \
 && curl -fsSL -o /tmp/s6-noarch.tar.xz https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-noarch.tar.xz \
 && curl -fsSL -o /tmp/s6-arch.tar.xz https://github.com/just-containers/s6-overlay/releases/download/v${S6_OVERLAY_VERSION}/s6-overlay-${S6_ARCH}.tar.xz \
 && tar -C /opt/s6 -Jxpf /tmp/s6-noarch.tar.xz \
 && tar -C /opt/s6 -Jxpf /tmp/s6-arch.tar.xz \
 && rm -f /tmp/s6-noarch.tar.xz /tmp/s6-arch.tar.xz

FROM fetcher AS supercronic

ARG TARGETARCH

ARG SUPERCRONIC_VERSION=none

RUN curl -fsSL -o /tmp/supercronic https://github.com/aptible/supercronic/releases/download/v${SUPERCRONIC_VERSION}/supercronic-linux-${TARGETARCH} \
 && install -m 0755 /tmp/supercronic /supercronic \
 && rm -f /tmp/supercronic

FROM fetcher AS tailscale

ARG TARGETARCH

ARG TAILSCALE_VERSION=none

RUN mkdir -p /opt/tailscale/usr/local/bin \
 && curl -fsSL -o /tmp/tailscale.tgz https://pkgs.tailscale.com/stable/tailscale_${TAILSCALE_VERSION}_${TARGETARCH}.tgz \
 && tar -C /tmp -xzf /tmp/tailscale.tgz --strip-components=1 tailscale_${TAILSCALE_VERSION}_${TARGETARCH}/tailscale tailscale_${TAILSCALE_VERSION}_${TARGETARCH}/tailscaled \
 && install -m 0755 /tmp/tailscale /tmp/tailscaled /opt/tailscale/usr/local/bin/ \
 && rm -f /tmp/tailscale.tgz /tmp/tailscale /tmp/tailscaled

FROM fetcher AS vscode

ARG VSCODE_COMMIT=latest
ARG TARGETARCH

RUN case "${TARGETARCH}" in \
        amd64) CODE_ARCH=x64 ;; \
        arm64) CODE_ARCH=arm64 ;; \
    esac \
 && mkdir -p /opt/vscode/usr/local/bin \
 && case "${VSCODE_COMMIT}" in \
        latest) CODE_RELEASE=latest ;; \
        *) CODE_RELEASE=commit:${VSCODE_COMMIT} ;; \
    esac \
 && curl -fsSL -o /tmp/code.tgz https://update.code.visualstudio.com/${CODE_RELEASE}/cli-linux-${CODE_ARCH}/stable \
 && tar -C /opt/vscode/usr/local/bin -xzf /tmp/code.tgz \
 && rm -f /tmp/code.tgz

FROM installer AS devtools

ARG DEVTOOLS_COSIGN_VERSION=latest
ARG DEVTOOLS_CLOUDFLARED_VERSION=none
ARG DEVTOOLS_TAILSCALE_VERSION=none

COPY --from=devcontainers /tmp/devcontainers/features/devtools /tmp/devcontainers/features/devtools

RUN CLOUDFLARED="${DEVTOOLS_CLOUDFLARED_VERSION}" COSIGN="${DEVTOOLS_COSIGN_VERSION}" TAILSCALE="${DEVTOOLS_TAILSCALE_VERSION}" bash /tmp/devcontainers/features/devtools/install.sh

FROM installer AS github-cli

ARG GITHUB_CLI_VERSION=latest

COPY --from=devcontainers /tmp/devcontainers/features/github-cli /tmp/devcontainers/features/github-cli

RUN VERSION="${GITHUB_CLI_VERSION}" bash /tmp/devcontainers/features/github-cli/install.sh

FROM installer AS herdr

ARG HERDR_VERSION=latest

COPY --from=devcontainers /tmp/devcontainers/features/herdr /tmp/devcontainers/features/herdr

RUN VERSION="${HERDR_VERSION}" BRIDGE=false bash /tmp/devcontainers/features/herdr/install.sh

FROM installer AS moshi

ARG MOSHI_VERSION=latest

COPY --from=devcontainers /tmp/devcontainers/features/moshi /tmp/devcontainers/features/moshi

RUN VERSION="${MOSHI_VERSION}" BRIDGE=false CLIPBOARD=false bash /tmp/devcontainers/features/moshi/install.sh

FROM composer/composer:${COMPOSER_VERSION}-bin AS composer

FROM installer AS docker-cli

ARG DOCKER_VERSION=latest
ARG DOCKER_BUILDX_VERSION=latest
ARG DOCKER_COMPOSE_VERSION=latest

ENV _REMOTE_USER=ubuntu

COPY --from=devcontainers /tmp/devcontainers/features/docker /tmp/devcontainers/features/docker

RUN VERSION="${DOCKER_VERSION}" \
    BUILDX="${DOCKER_BUILDX_VERSION}" \
    COMPOSE="${DOCKER_COMPOSE_VERSION}" \
    SOCKETUSER=root \
    bash /tmp/devcontainers/features/docker/install.sh

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM installer AS lang-bun

ARG BUN_VERSION=latest

ENV _REMOTE_USER=ubuntu
ENV _REMOTE_USER_HOME=/home/ubuntu

COPY --from=devcontainers /tmp/devcontainers/features/bun /tmp/devcontainers/features/bun

RUN VERSION="${BUN_VERSION}" bash /tmp/devcontainers/features/bun/install.sh

FROM installer AS lang-deno

ARG DENO_VERSION=latest

ENV _REMOTE_USER=ubuntu
ENV _REMOTE_USER_HOME=/home/ubuntu

COPY --from=devcontainers /tmp/devcontainers/features/deno /tmp/devcontainers/features/deno

RUN VERSION="${DENO_VERSION}" bash /tmp/devcontainers/features/deno/install.sh

FROM installer AS lang-go

ARG GO_VERSION=none

ENV _REMOTE_USER=ubuntu
ENV _REMOTE_USER_HOME=/home/ubuntu

COPY --from=devcontainers /tmp/devcontainers/features/go /tmp/devcontainers/features/go

RUN sed -i \
        -e 's#tar -xz -f /tmp/go.tar.gz -C ${GOROOT} --strip-components=1#tar -xz -f /tmp/go.tar.gz -C ${GOROOT} --strip-components=1 --exclude="go/test/*" --exclude="go/test"#' \
        /tmp/devcontainers/features/go/install.sh \
 && VERSION="${GO_VERSION}" bash /tmp/devcontainers/features/go/install.sh

FROM installer AS lang-node

ARG NODE_VERSION=none

ENV _REMOTE_USER=ubuntu
ENV _REMOTE_USER_HOME=/home/ubuntu

COPY --from=devcontainers /tmp/devcontainers/features/node /tmp/devcontainers/features/node

RUN VERSION="${NODE_VERSION}" bash /tmp/devcontainers/features/node/install.sh

FROM installer AS lang-php

ARG PHP_VERSION=none

ENV _REMOTE_USER=ubuntu
ENV _REMOTE_USER_HOME=/home/ubuntu

COPY --from=devcontainers /tmp/devcontainers/features/php /tmp/devcontainers/features/php

RUN sed -i \
        -e '/--disable-phar/d' \
        -e 's/docker-php-ext-install gd phar/docker-php-ext-install gd/' \
        -e 's/docker-php-ext-enable opcache sodium/docker-php-ext-enable sodium/' \
        /tmp/devcontainers/features/php/install.sh \
 && VERSION="${PHP_VERSION}" bash /tmp/devcontainers/features/php/install.sh

FROM installer AS lang-python

ARG PYTHON_VERSION=none

ENV _REMOTE_USER=ubuntu
ENV _REMOTE_USER_HOME=/home/ubuntu

COPY --from=devcontainers /tmp/devcontainers/features/python /tmp/devcontainers/features/python

RUN sed -i \
        -e '/tar -xJ -f \/tmp\/python.tar.xz -C \/usr\/src\/python --strip-components=1/a\    rm -rf /usr/src/python/Lib/test /usr/src/python/Lib/idlelib/idle_test' \
        /tmp/devcontainers/features/python/install.sh \
 && VERSION="${PYTHON_VERSION}" bash /tmp/devcontainers/features/python/install.sh

FROM installer AS lang-rust

ARG RUST_VERSION=none

ENV _REMOTE_USER=ubuntu
ENV _REMOTE_USER_HOME=/home/ubuntu

COPY --from=devcontainers /tmp/devcontainers/features/rust /tmp/devcontainers/features/rust

RUN sed -i \
        -e 's#RUST_COMPONENTS=\$(cat /tmp/rust/components)#RUST_COMPONENTS=$(grep -vE "^rust-docs(-json-preview)?$|^llvm-tools-preview$" /tmp/rust/components)#' \
        /tmp/devcontainers/features/rust/install.sh \
 && VERSION="${RUST_VERSION}" bash /tmp/devcontainers/features/rust/install.sh

FROM installer AS agent-antigravity-cli

ARG ANTIGRAVITY_CLI_VERSION=latest

COPY --from=devcontainers /tmp/devcontainers/features/antigravity-cli /tmp/devcontainers/features/antigravity-cli

RUN VERSION="${ANTIGRAVITY_CLI_VERSION}" bash /tmp/devcontainers/features/antigravity-cli/install.sh

FROM installer AS agent-copilot

ARG COPILOT_VERSION=latest

COPY --from=devcontainers /tmp/devcontainers/features/copilot-cli /tmp/devcontainers/features/copilot-cli

RUN VERSION="${COPILOT_VERSION}" bash /tmp/devcontainers/features/copilot-cli/install.sh

FROM installer AS agent-opencode

ARG OPENCODE_VERSION=latest

COPY --from=devcontainers /tmp/devcontainers/features/opencode /tmp/devcontainers/features/opencode

RUN VERSION="${OPENCODE_VERSION}" bash /tmp/devcontainers/features/opencode/install.sh

FROM installer AS agent-claude-code

ARG CLAUDE_CODE_VERSION=latest

ENV _REMOTE_USER=ubuntu
ENV _REMOTE_USER_HOME=/home/ubuntu

COPY --from=lang-node /usr/local/ /usr/local/
COPY --from=devcontainers /tmp/devcontainers/features/claude-code /tmp/devcontainers/features/claude-code

RUN VERSION="${CLAUDE_CODE_VERSION}" bash /tmp/devcontainers/features/claude-code/install.sh

FROM installer AS agent-codex

ARG CODEX_VERSION=latest

ENV _REMOTE_USER=ubuntu
ENV _REMOTE_USER_HOME=/home/ubuntu

COPY --from=lang-node /usr/local/ /usr/local/
COPY --from=devcontainers /tmp/devcontainers/features/codex /tmp/devcontainers/features/codex

RUN VERSION="${CODEX_VERSION}" bash /tmp/devcontainers/features/codex/install.sh

FROM devtools

ENV DEBIAN_FRONTEND=noninteractive

SHELL ["/bin/bash", "-o", "pipefail", "-c"]

RUN apt-get update \
 && apt-get install --no-install-recommends --yes \
        bubblewrap \
        build-essential \
        ca-certificates \
        curl \
        git \
        gnupg \
        jq \
        less \
        libargon2-1 \
        libpng16-16t64 \
        libsodium23 \
        libxml2-16 \
        locales \
        mosh \
        openssh-client \
        procps \
        python3 \
        ripgrep \
        sudo \
        tar \
        unzip \
        vim \
        wget \
        xz-utils \
        zip \
        zsh \
 && rm -rf /var/lib/apt/lists/*

COPY --from=s6 /opt/s6/ /
COPY --from=tailscale /opt/tailscale/ /
COPY --from=vscode /opt/vscode/ /
COPY --from=composer /composer /usr/local/bin/composer
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/ /usr/local/libexec/docker/cli-plugins/
COPY --from=github-cli /usr/local/bin/gh /usr/local/bin/gh
COPY --from=herdr /usr/local/bin/herdr /usr/local/bin/herdr
COPY --from=moshi /usr/local/bin/moshi-hook /usr/local/bin/moshi-hook
COPY --from=supercronic /supercronic /usr/local/bin/supercronic
COPY --from=uv /uv /uvx /usr/local/bin/
COPY --from=lang-bun /usr/local/bin/bun /usr/local/bin/bun
COPY --from=lang-deno /usr/local/bin/deno /usr/local/bin/deno
COPY --from=lang-go /usr/local/go /usr/local/go
COPY --from=lang-go /opt/go /opt/go
COPY --from=lang-node /usr/local/ /usr/local/
COPY --from=lang-php /usr/local/ /usr/local/
COPY --from=lang-python /usr/local/ /usr/local/
COPY --from=lang-rust /usr/local/ /usr/local/
COPY --from=agent-antigravity-cli /usr/local/share/antigravity-cli /usr/local/share/antigravity-cli
COPY --from=agent-claude-code /usr/local/share/claude-code /usr/local/share/claude-code
COPY --from=agent-codex /usr/local/share/codex /usr/local/share/codex
COPY --from=agent-copilot /usr/local/share/copilot-cli /usr/local/share/copilot-cli
COPY --from=agent-opencode /usr/local/share/opencode /usr/local/share/opencode

RUN set -eu ; \
    for directory in antigravity-cli claude-code codex copilot-cli opencode ; do \
        chown -R ubuntu:ubuntu /usr/local/share/${directory} ; \
    done \
 && for binary in code herdr moshi-hook tailscale tailscaled uv uvx ; do \
        if [ -e /usr/local/bin/${binary} ] ; then chown ubuntu:ubuntu /usr/local/bin/${binary} ; fi ; \
    done \
 && install -d -o ubuntu -g ubuntu -m 0755 /usr/local/npm /usr/local/npm/bin /usr/local/npm/lib /usr/local/uv /usr/local/uv/bin /usr/local/uv/tools ; \
    export PATH=/usr/local/go/bin:/opt/go/bin:/usr/local/bin:/usr/local/npm/bin:/usr/local/uv/bin:/usr/local/share/antigravity-cli/bin:/usr/local/share/claude-code/bin:/usr/local/share/codex/bin:/usr/local/share/copilot-cli/bin:/usr/local/share/opencode/bin:${PATH} ; \
    missing="" ; \
    for binary in agy bun cc claude code codex composer copilot cargo deno docker gh go herdr make moshi-hook node npm opencode php python3 rustc sudo tailscale tailscaled uv uvx ; do \
        command -v "${binary}" > /dev/null 2>&1 || missing="${missing} ${binary}" ; \
    done ; \
    if [ -n "${missing}" ] ; then echo "missing binaries:${missing}" >&2 ; exit 1 ; fi ; \
    docker buildx version > /dev/null ; \
    docker compose version > /dev/null ; \
    echo "all expected binaries present" \
 && printf 'ubuntu ALL=(ALL) NOPASSWD:ALL\n' > /etc/sudoers.d/ubuntu \
 && chmod 0440 /etc/sudoers.d/ubuntu \
 && visudo --check --quiet --file /etc/sudoers.d/ubuntu \
 && mkdir -p /workspace /var/lib/tailscale /run/tailscale \
 && chown ubuntu:ubuntu /workspace /var/lib/tailscale /run/tailscale \
 && usermod --shell /usr/bin/zsh ubuntu

COPY rootfs/ /

ENV HOME=/home/ubuntu
ENV LANG=C.utf8
ENV LC_ALL=C.utf8
ENV PATH=/home/ubuntu/.local/bin:/usr/local/go/bin:/opt/go/bin:/usr/local/bin:/usr/local/npm/bin:/usr/local/uv/bin:/usr/local/share/antigravity-cli/bin:/usr/local/share/claude-code/bin:/usr/local/share/codex/bin:/usr/local/share/copilot-cli/bin:/usr/local/share/opencode/bin:/command:/usr/bin:/bin:/usr/sbin:/sbin
ENV SHELL=/usr/bin/zsh

ENV CARGO_HOME=/usr/local/cargo
ENV DOCKER_HOST=unix:///run/docker/docker.sock
ENV GOPATH=/opt/go
ENV GOROOT=/usr/local/go

ENV HERDR_STARTUP_CWD=/workspace
ENV NPM_CONFIG_PREFIX=/usr/local/npm
ENV S6_KEEP_ENV=1
ENV TS_STATE_DIR=/var/lib/tailscale
ENV UV_TOOL_BIN_DIR=/usr/local/uv/bin
ENV UV_TOOL_DIR=/usr/local/uv/tools

ENV SLEIPNIR_ENABLE_CODE_TUNNEL=0
ENV SLEIPNIR_ENABLE_DOTFILES=0
ENV SLEIPNIR_ENABLE_HERDR=1
ENV SLEIPNIR_ENABLE_MOSHI=1
ENV SLEIPNIR_ENABLE_RC_AGY=0
ENV SLEIPNIR_ENABLE_RC_CLAUDE=0
ENV SLEIPNIR_ENABLE_RC_CODEX=0
ENV SLEIPNIR_ENABLE_SCHEDULER=0
ENV SLEIPNIR_ENABLE_TAILSCALE=0

ENV SLEIPNIR_SCHEDULER_CRONTAB=/etc/sleipnir/scheduler/crontab
ENV SLEIPNIR_SCHEDULER_WORKDIR=/home/ubuntu

ENV SLEIPNIR_TAILSCALE_TUN=tailscale0

EXPOSE 24544

ENTRYPOINT ["/init"]
