# runtests -- standalone test runner.
#
# Single job: materialize a jj revision into an isolated workspace and run
# pytest against it. No dependency on any other project. Movable to its own
# repository together with ../scripts/venv-cache.sh.
FROM debian:stable-slim

# Pin toolchain versions explicitly; do not track "latest".
ARG JJ_VERSION=0.45.1
# Poetry 2.x: reads PEP 621 [project] metadata (1.x only reads [tool.poetry]).
ARG POETRY_VERSION=2.5.1

# System + build deps. build-essential / pkg-config / default-libmysqlclient-dev
# cover the C-extension deps (mysqlclient etc.) that target repos commonly pull.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-venv \
        python3-dev \
        python3-pip \
        build-essential \
        pkg-config \
        default-libmysqlclient-dev \
        default-mysql-client \
        libnss-wrapper \
        openssh-client \
    && rm -rf /var/lib/apt/lists/*

# jj: static musl binary works on any glibc/musl base.
RUN curl -fsSL "https://github.com/jj-vcs/jj/releases/download/v${JJ_VERSION}/jj-v${JJ_VERSION}-x86_64-unknown-linux-musl.tar.gz" \
        -o /tmp/jj.tar.gz && \
    tar -xzf /tmp/jj.tar.gz -C /usr/local/bin ./jj && \
    rm -f /tmp/jj.tar.gz && \
    /usr/local/bin/jj --version

# The broker refuses an image whose jj is older than 0.38 (broker/cld-broker.py:
# require_jj_image): older jj reads the repo's own config files.
LABEL org.cld.jj-version="${JJ_VERSION}"

# Poetry, pinned.
RUN curl -sSL https://install.python-poetry.org | \
        POETRY_HOME=/opt/poetry python3 - --version "${POETRY_VERSION}" && \
    ln -s /opt/poetry/bin/poetry /usr/local/bin/poetry && \
    /usr/local/bin/poetry --version

# Fixed venv path shared with scripts/venv-cache.sh: a venv only works at the
# absolute path it was built at, so cached venvs are copied back here. World-
# writable because the broker's --user uid is unknown at build time. VIRTUAL_ENV
# makes poetry use this venv instead of creating its own. /cache is where the
# broker mounts the host-side cache; unmounted, it is just a scratch dir.
RUN mkdir -p /opt/venv /cache && chmod 1777 /opt/venv /cache
ENV VIRTUAL_ENV=/opt/venv \
    POETRY_CACHE_DIR=/cache/poetry
# From the `scripts` build context (../scripts, passed by build.sh).
COPY --from=scripts venv-cache.sh /usr/local/bin/venv-cache.sh
# `git+ssh://` dependencies: git and poetry's dulwich run `ssh` from PATH, and
# this wrapper (ahead of /usr/bin/ssh) makes it work as the broker's --user uid.
COPY --from=scripts ssh-wrapper.sh /usr/local/bin/ssh

# Bake internal CA certs into the system trust store so python/requests/poetry
# and TLS to the databases trust them without any host mount.
COPY ca-certs/seznam/ /tmp/seznam-ca/
RUN mkdir -p /usr/local/share/ca-certificates/seznam && \
    for f in /tmp/seznam-ca/*.pem; do \
        cp "$f" "/usr/local/share/ca-certificates/seznam/$(basename "${f%.pem}").crt"; \
    done && \
    rm -rf /tmp/seznam-ca && \
    update-ca-certificates

ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
