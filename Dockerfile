# vcsim-lab — vcsim plus the lab in one container.
#
# Base is python:3.12-slim (NOT vmware/vcsim — that image is too minimal
# to add Python to). The vcsim binary is downloaded from the govmomi
# releases the same way tools/start-vcsim-binary.sh does.
#
# Build:  docker build -t vcsim-lab .
# Run:    docker run -p 8989:8989 vcsim-lab
#         (SDK endpoint https://localhost:8989/sdk, user/pass by default)
# The entrypoint starts vcsim, builds the 12-VM lab, then keeps vcsim
# in the foreground. Render sets $PORT; locally it defaults to 8989.
FROM python:3.12-slim

ARG VCSIM_VERSION=v0.56.2

# curl (to fetch vcsim) + CA certs (to verify the download over HTTPS).
# Removed apt lists keep the image lean.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ARCH="$(uname -m)" \
    && case "$ARCH" in x86_64) ARCH="x86_64" ;; aarch64|arm64) ARCH="arm64" ;; esac \
    && curl -fsSL -o /tmp/vcsim.tar.gz \
         "https://github.com/vmware/govmomi/releases/download/${VCSIM_VERSION}/vcsim_Linux_${ARCH}.tar.gz" \
    && tar -C /usr/local/bin -xzf /tmp/vcsim.tar.gz vcsim \
    && rm /tmp/vcsim.tar.gz \
    && chmod +x /usr/local/bin/vcsim \
    && test -x /usr/local/bin/vcsim

WORKDIR /app

# Dependencies first so they cache across code-only rebuilds.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# The entrypoint runs vcsim + the lab build (see docker-entrypoint.sh).
RUN chmod +x /app/docker-entrypoint.sh

# Documentary only: Render ignores EXPOSE and injects $PORT, which the
# entrypoint honours (default 8989 when $PORT is unset).
ENV PORT=8989
EXPOSE 8989

ENTRYPOINT ["/app/docker-entrypoint.sh"]
