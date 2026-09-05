# syntax=docker/dockerfile:1
FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    HOME=/config

# --- Install MEGAcmd from MEGA's official apt repo ------------------------
# (python:3.11-slim-bookworm is Debian 12, hence the Debian_12 repo path)
RUN apt-get update && apt-get install -y --no-install-recommends \
        wget gnupg ca-certificates 
RUN mkdir -p /etc/apt/keyrings 
RUN wget -qO- https://mega.nz/keys/MEGA_signing.key \
        | gpg --dearmor -o /etc/apt/keyrings/mega.nz.gpg 
RUN echo "deb [arch=amd64,arm64 signed-by=/etc/apt/keyrings/mega.nz.gpg] https://mega.nz/linux/repo/Debian_12/ ./" \
        > /etc/apt/sources.list.d/mega.nz.list 
RUN apt-get update 
RUN apt-get install -y --no-install-recommends megacmd 
RUN rm -rf /var/lib/apt/lists/*

# --- Media Organizer ---------------------------------------------------
WORKDIR /app
COPY ./media_organizer ./media_organizer
RUN pip install --no-cache-dir -r media_organizer/requirements.txt

# --- Python dependencies ---------------------------------------------------
COPY ./server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install gunicorn

# --- App code ----------------------------------------------------------
COPY ./server/app.py .
COPY ./server/bootstrap.sh /usr/local/bin/bootstrap.sh
COPY ./server/templates .
RUN chmod +x /usr/local/bin/bootstrap.sh

# /library : organised media output (mount a host directory here)
# /config  : MEGAcmd's $HOME/.megaCmd session/config lives here (mount to
#            persist login sessions across container runs)
RUN mkdir -p /library /config
VOLUME ["/library", "/config"]
EXPOSE 5000

ENTRYPOINT ["/usr/local/bin/bootstrap.sh"]