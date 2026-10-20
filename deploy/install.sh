#!/bin/sh
set -eu

cd /opt/russia-crossborder-erp

if [ ! -f deploy/.env ]; then
    umask 077
    postgres_password="$(openssl rand -hex 32)"
    app_secret_key="$(python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())')"
    worker_token="$(openssl rand -hex 32)"
    operator_token="$(openssl rand -hex 32)"
    {
        printf 'POSTGRES_DB=erp\n'
        printf 'POSTGRES_USER=erp\n'
        printf 'POSTGRES_PASSWORD=%s\n' "$postgres_password"
        printf 'ERP_SECRET_KEY=%s\n' "$app_secret_key"
        printf 'ERP_WORKER_TOKEN=%s\n' "$worker_token"
        printf 'ERP_ACCESS_TOKEN=%s\n' "$operator_token"
    } > deploy/.env
fi

docker compose -f deploy/compose.yaml --env-file deploy/.env build
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d database
docker compose -f deploy/compose.yaml --env-file deploy/.env run --rm api python -m alembic upgrade head
docker compose -f deploy/compose.yaml --env-file deploy/.env up -d api
