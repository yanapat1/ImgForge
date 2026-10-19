#!/bin/bash
# THIS SERVER ONLY: run after the server resets (everything outside /home/jovyan/work is wiped).
# Restores the Docker daemon config from the persistent copy, restarts Docker and starts image-tools.
# Docker itself is installed by /home/jovyan/work/launch.sh. Other servers just use `docker compose up -d`.
set -e
cd "$(dirname "$0")"

PERSIST=/home/jovyan/work/docker-config
if [ -f "$PERSIST/daemon.json" ] && ! cmp -s "$PERSIST/daemon.json" /etc/docker/daemon.json 2>/dev/null; then
    echo "--> Restoring /etc/docker/daemon.json (data-root, nvidia runtime)"
    sudo mkdir -p /etc/docker && sudo cp "$PERSIST/daemon.json" /etc/docker/daemon.json
    sudo systemctl restart docker 2>/dev/null || sudo service docker restart
fi

[ -f .env ] || cp .env.example .env    # then set COMPOSE_FILE in .env for this server
docker compose up -d
