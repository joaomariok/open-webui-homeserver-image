#!/bin/sh
# Backs up Open WebUI's data: a consistent copy of the SQLite database (safe
# while Open WebUI is running, unlike a plain file copy) plus the uploads folder.
# Run as root (the data folder is owned by the container's root user):
#   sudo sh scripts/backup.sh
# Nightly at 03:30 (sudo crontab -e):
#   30 3 * * * sh /home/joaom/homeserver-ai/scripts/backup.sh >> /var/log/openwebui-backup.log 2>&1
# Needs: sudo apt install sqlite3
set -eu

cd "$(dirname "$0")/.."
DATA=./data/open-webui
DEST=${BACKUP_DIR:-./backups}
KEEP=${BACKUP_KEEP:-14}
STAMP=$(date +%Y%m%d-%H%M%S)

command -v sqlite3 >/dev/null || { echo "sqlite3 not found: sudo apt install sqlite3" >&2; exit 1; }
[ -f "$DATA/webui.db" ] || { echo "no database at $DATA/webui.db" >&2; exit 1; }

mkdir -p "$DEST"
sqlite3 "$DATA/webui.db" ".backup '$DEST/webui-$STAMP.db'"
if [ -d "$DATA/uploads" ]; then
  tar -czf "$DEST/uploads-$STAMP.tar.gz" -C "$DATA" uploads
fi

# Keep only the newest $KEEP of each kind.
for pattern in 'webui-*.db' 'uploads-*.tar.gz'; do
  # shellcheck disable=SC2012
  ls -1t "$DEST"/$pattern 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do rm -f -- "$old"; done
done

echo "$(date -Is) backup ok: $DEST/webui-$STAMP.db"
