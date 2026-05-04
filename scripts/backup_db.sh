#!/bin/bash
# Ежедневный бэкап БД MaxMover.
# Пишет в /opt/maxmover/backups/, хранит 14 последних, остальные удаляет.
# При ошибке exit != 0 — systemd timer увидит FAILED и journalctl сохранит.

set -euo pipefail

BACKUP_DIR="/opt/maxmover/backups"
PROJECT_DIR="/opt/maxmover"
DATE=$(date -u +%Y-%m-%d_%H-%M-%S)
FILE="${BACKUP_DIR}/maxmover_${DATE}.sql.gz"
KEEP=14

mkdir -p "$BACKUP_DIR"

cd "$PROJECT_DIR"
docker compose exec -T db pg_dump -U maxmover -d maxmover --no-owner --no-acl \
    | gzip -9 > "$FILE"

# Проверяем что файл не пустой (gzip даже пустого ввода даёт ~20 байт)
SIZE=$(stat -c%s "$FILE")
if [ "$SIZE" -lt 1000 ]; then
    echo "Backup file too small: ${SIZE} bytes — probably failed" >&2
    rm -f "$FILE"
    exit 1
fi

# Ротация — оставляем KEEP последних
ls -1t "${BACKUP_DIR}"/maxmover_*.sql.gz 2>/dev/null | tail -n +$((KEEP+1)) | xargs -r rm -f

echo "Backup OK: ${FILE} (${SIZE} bytes)"
