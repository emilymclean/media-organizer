# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HOME=/config

# --- Install MEGAcmd from MEGA's official apt repo ------------------------
# (python:3.11-slim-bookworm is Debian 12, hence the Debian_12 repo path)
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates \
    && mkdir -p /etc/apt/keyrings \
    && wget -qO- https://mega.nz/keys/MEGA_signing.key \
        | gpg --dearmor -o /etc/apt/keyrings/mega.nz.gpg \
    && echo "deb [arch=amd64,arm64 signed-by=/etc/apt/keyrings/mega.nz.gpg] https://mega.nz/linux/repo/Debian_12/ ./" \
        > /etc/apt/sources.list.d/mega.nz.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends megacmd \
    && apt-get purge -y wget gnupg \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python dependencies ---------------------------------------------------
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- App code ----------------------------------------------------------
COPY media_organizer.py .
COPY bootstrap.sh /usr/local/bin/bootstrap.sh
RUN chmod +x /usr/local/bin/bootstrap.sh

# /library : organised media output (mount a host directory here)
# /config  : MEGAcmd's $HOME/.megaCmd session/config lives here (mount to
#            persist login sessions across container runs)
RUN mkdir -p /library /config
VOLUME ["/library", "/config"]

ENTRYPOINT ["/usr/local/bin/bootstrap.sh"]
CMD ["--help"]