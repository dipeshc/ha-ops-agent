# The base image is pinned here, by digest per architecture, and not taken from BUILD_FROM: Supervisor
# 2026.10 deprecates build.yaml and can pass `base:latest` instead of its pinned `build_from`. Both
# digests are the per-platform manifests of the ghcr.io/home-assistant/base:3.24 index
# sha256:58422480a299b16b6ea376c2167463206d430619ed02b40cd687c8bd6875519e. The Supervisor passes
# BUILD_ARCH; only that stage is used. No default: a build without it fails instead of silently taking
# another architecture's image.
ARG BUILD_ARCH
FROM ghcr.io/home-assistant/base:3.24@sha256:2eed1dd7bd4fa0de08b31739dc1690d148f56d2aa5b0e933765c434b55d75fa3 AS base-aarch64
FROM ghcr.io/home-assistant/base:3.24@sha256:ed340acf0a3a77a761b4d3ee2977ab5ddc44038304597e66b0ccfd859b8d8a1f AS base-amd64
# hadolint ignore=DL3006
FROM base-${BUILD_ARCH}

# Claude Code on musl needs libgcc, libstdc++ and the system ripgrep. py3-yaml (PyYAML's
# SafeLoader) checks that proposed config changes parse.
# Claude Code comes from Anthropic's signed apk repository (stable channel),
# so the binary is fixed per image build and never self-updates.
# Pinned for reproducible builds. Bump deliberately; see the stable indexes at
# https://downloads.claude.ai/claude-code/apk/stable/{aarch64,x86_64}/APKINDEX.tar.gz
ARG CLAUDE_CODE_VERSION=2.1.311-r1
ARG CLAUDE_APK_KEY_SHA256=395759c1f7449ef4cdef305a42e820f3c766d6090d142634ebdb049f113168b6
# Alpine packages are pinned to the exact v3.24 versions too (from the v3.24 main/community
# aarch64 and x86_64 indexes, which carry the same versions). Alpine keeps only the newest build
# of each package in a branch, so a security update there makes this build fail until these
# are bumped: deliberate, never silent. krb5-libs and zlib come with the base image and are pinned
# here too, because the base image can lag the branch's security builds.
RUN apk add --no-cache \
        bash=5.3.9-r1 \
        ca-certificates=20261020-r0 \
        git=2.54.0-r0 \
        krb5-libs=1.22.3-r0 \
        libgcc=15.2.0-r5 \
        libstdc++=15.2.0-r5 \
        openssh-client-default=10.3_p1-r2 \
        openssh-keygen=10.3_p1-r2 \
        python3=3.14.10-r1 \
        py3-yaml=6.0.3-r2 \
        ripgrep=15.1.0-r0 \
        setpriv=2.42.3-r1 \
        tzdata=2026f-r0 \
        zlib=1.4-r0 \
    && wget -q -O /etc/apk/keys/claude-code.rsa.pub \
        https://downloads.claude.ai/keys/claude-code.rsa.pub \
    && echo "${CLAUDE_APK_KEY_SHA256}  /etc/apk/keys/claude-code.rsa.pub" | sha256sum -c - \
    && echo "https://downloads.claude.ai/claude-code/apk/stable" >> /etc/apk/repositories \
    && apk add --no-cache "claude-code=${CLAUDE_CODE_VERSION}" \
    && claude --version

# The AI runs as this unprivileged account (no sudo, no shell).
RUN addgroup -S -g 1001 ops \
    && adduser -S -D -H -u 1001 -G ops -s /sbin/nologin -h /data/claude-ops/home ops

COPY rootfs /

# No setuid/setgid binaries: nothing the AI starts can regain privileges.
RUN find / -xdev -type f -perm /6000 -exec chmod a-s {} +

RUN chmod 0755 /opt/ops/bin/* /etc/s6-overlay/scripts/*.sh \
        /etc/s6-overlay/s6-rc.d/opsd/run /etc/s6-overlay/s6-rc.d/opsd/finish \
    && chmod 0644 /etc/claude-code/managed-settings.json /opt/ops/ssh/known_hosts \
    && python3 -m compileall -q /opt/ops/opsd

ENV PYTHONUNBUFFERED=1 \
    PATH=/opt/ops/bin:${PATH}
