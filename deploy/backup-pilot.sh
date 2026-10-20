#!/usr/bin/env bash
# Cloud-only data maintenance; does not execute ERP business jobs.
set -Eeuo pipefail
umask 077
base=/opt/backups/erp-desktop-pilot
container=erp-desktop-pilot-database-1
install -d -m 700 "$base"
exec 9>"$base/.lock"
flock -n 9 || exit 0
# Refuse to consume the final 2 GiB of the server disk.
test "$(df -Pk "$base" | awk 'NR==2 {print $4}')" -gt 2097152
stamp=$(date -u +%Y%m%dT%H%M%SZ)
run=$(mktemp -d "$base/$stamp-XXXXXX")
restore_db="erp_backup_check_$(date +%s)_$$"
created=0
cleanup() {
    if [ "$created" = 1 ]; then
        docker exec "$container" dropdb -U erp "$restore_db"
    fi
}
trap cleanup EXIT
docker exec "$container" pg_dump -U erp -d erp -Fc > "$run/database.dump.partial"
test -s "$run/database.dump.partial"
docker exec -i "$container" pg_restore --list < "$run/database.dump.partial" > "$run/contents.txt"
mv "$run/database.dump.partial" "$run/database.dump"
# Save deployment settings privately, never to the publicly readable image bucket.
tar -czf "$run/deployment-config.tar.gz" -C /opt/erp-desktop-pilot compose.yaml .env
if [ -d /opt/erp-desktop-pilot/catalog-source-files ]; then
    tar -czf "$run/catalog-source-files.tar.gz" -C /opt/erp-desktop-pilot catalog-source-files
    tar -tzf "$run/catalog-source-files.tar.gz" > "$run/source-files-contents.txt"
fi
if docker exec erp-desktop-pilot-api-1 test -d /var/lib/erp/ozon_categories; then
    docker exec erp-desktop-pilot-api-1 tar -czf - -C /var/lib/erp ozon_categories > "$run/catalog-categories.tar.gz"
    tar -tzf "$run/catalog-categories.tar.gz" > "$run/category-contents.txt"
fi
  docker inspect --format '{{.Config.Image}}' erp-desktop-pilot-api-1 > "$run/image.txt"
  if docker exec erp-desktop-pilot-api-1 test -d /var/lib/erp/pod/ai-cloud-inputs; then
    docker exec erp-desktop-pilot-api-1 tar -czf - -C /var/lib/erp pod/ai-cloud-inputs > "$run/ai-inputs.tar.gz"
    tar -tzf "$run/ai-inputs.tar.gz" > "$run/ai-inputs-contents.txt"
  fi
if docker exec erp-desktop-pilot-api-1 test -d /var/lib/erp/cloud-thumbnails; then
    docker exec erp-desktop-pilot-api-1 tar -czf - -C /var/lib/erp cloud-thumbnails > "$run/cloud-thumbnails.tar.gz"
fi
if docker exec erp-desktop-pilot-api-1 test -d /var/lib/erp/pod/wb; then
    docker exec erp-desktop-pilot-api-1 tar -czf - -C /var/lib/erp pod/wb > "$run/wb-private-files.tar.gz"
    tar -tzf "$run/wb-private-files.tar.gz" > "$run/wb-private-files-contents.txt"
fi
# SQLite online backup includes committed WAL pages while the API stays online.
# Never archive a live database file directly: its WAL may contain newer orders.
if docker exec erp-desktop-pilot-api-1 test -f /var/lib/erp/orders/pod/orders.db; then
    docker exec -i erp-desktop-pilot-api-1 python - > "$run/orders.db.partial" <<'PY'
import sqlite3, tempfile, pathlib, sys, shutil
from contextlib import closing
with tempfile.TemporaryDirectory(prefix='erp-order-backup-') as directory:
    target = pathlib.Path(directory) / 'orders.db'
    with closing(sqlite3.connect('file:/var/lib/erp/orders/pod/orders.db?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(target)) as backup:
            source.backup(backup)
    with closing(sqlite3.connect(target.as_uri() + '?mode=ro', uri=True)) as restored:
        if restored.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('Order backup integrity check failed')
        for table in ('ozon_orders', 'batch_order_groups'):
            restored.execute('SELECT count(*) FROM ' + table).fetchone()
    with target.open('rb') as stream:
        shutil.copyfileobj(stream, sys.stdout.buffer)
PY
    test -s "$run/orders.db.partial"
    mv "$run/orders.db.partial" "$run/orders.db"
fi
docker exec "$container" createdb -U erp -T template0 "$restore_db"
created=1
docker exec -i "$container" pg_restore -U erp --no-owner --exit-on-error -d "$restore_db" < "$run/database.dump"
docker exec "$container" psql -X -v ON_ERROR_STOP=1 -U erp -d "$restore_db" -Atc "SELECT 'schema='||version_num FROM alembic_version; SELECT 'products='||count(*) FROM catalog_products; SELECT 'variants='||count(*) FROM catalog_variants; SELECT 'assets='||count(*) FROM assets; SELECT 'product_assets='||count(*) FROM catalog_product_assets; SELECT 'linked_assets_without_oss='||count(*) FROM assets WHERE id IN (SELECT asset_id FROM catalog_product_assets) AND coalesce(oss_url,'')='';" > "$run/restored-counts.txt"
(cd "$run" && sha256sum database.dump deployment-config.tar.gz > SHA256SUMS)
if [ -f "$run/catalog-source-files.tar.gz" ]; then
    (cd "$run" && sha256sum catalog-source-files.tar.gz >> SHA256SUMS)
fi
if [ -f "$run/catalog-categories.tar.gz" ]; then
    (cd "$run" && sha256sum catalog-categories.tar.gz >> SHA256SUMS)
fi
cleanup
created=0
if [ -f "$run/cloud-thumbnails.tar.gz" ]; then
    (cd "$run" && sha256sum cloud-thumbnails.tar.gz >> SHA256SUMS)
fi
  date -u +%FT%TZ > "$run/RESTORE_VERIFIED"
  if [ -f "$run/ai-inputs.tar.gz" ]; then (cd "$run" && sha256sum ai-inputs.tar.gz >> SHA256SUMS); fi
if [ -f "$run/wb-private-files.tar.gz" ]; then (cd "$run" && sha256sum wb-private-files.tar.gz >> SHA256SUMS); fi
if [ -f "$run/orders.db" ]; then (cd "$run" && sha256sum orders.db >> SHA256SUMS); fi
printf 'Backup restored successfully: %s\n' "$run"
cat "$run/restored-counts.txt"
# No automatic removal: only administrator-reviewed backups may be pruned.
