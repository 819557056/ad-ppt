#!/usr/bin/env bash
# Restore only into an empty PostgreSQL database and empty named data volumes.
set -Eeuo pipefail

if [[ $# -ne 1 || ${SCENE_RESTORE_CONFIRM:-} != restore-into-empty-stack ]]; then
  echo 'Usage: SCENE_RESTORE_CONFIRM=restore-into-empty-stack scripts/scene-restore.sh BACKUP_DIRECTORY' >&2
  echo 'Start only the PostgreSQL service; API, worker, renderer and frontend must remain stopped.' >&2
  exit 2
fi
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
backup=$(realpath -e -- "$1")
if [[ ! -f "$backup/COMPLETE" ]]; then
  echo 'Backup has no COMPLETE marker.' >&2
  exit 2
fi
(cd -- "$backup" && sha256sum -c SHA256SUMS)
cmp -- "$root/backend/fonts/manifest.json" "$backup/font-manifest.json" || {
  echo 'Font manifest differs from backup; restore refused.' >&2
  exit 2
}
dc=(docker compose --project-directory "$root" -f "$root/docker-compose.scene.yml")
"${dc[@]}" version >/dev/null
mapfile -t running < <("${dc[@]}" ps --status running --services)
for service in "${running[@]}"; do
  if [[ "$service" != postgres ]]; then
    echo "Service $service is still running; restore refused." >&2
    exit 2
  fi
done
if [[ ! " ${running[*]} " == *' postgres '* ]]; then
  echo 'PostgreSQL service must be running.' >&2
  exit 2
fi
# Older backups without engine manifests need a separately reviewed migration;
# there is deliberately no ignore-runtime switch.
source "$root/scripts/scene-runtime-common.sh"
runtime_tmp=$(mktemp -d)
trap 'rm -rf -- "$runtime_tmp"' EXIT
scene_capture_runtime "$runtime_tmp"
scene_compare_runtime "$backup/runtime" "$runtime_tmp"
clients=$("${dc[@]}" exec -T postgres psql -U banana_scene -d banana_scene -Atq -c \
  "SELECT count(*) FROM pg_stat_activity WHERE datname='banana_scene' AND backend_type='client backend' AND pid<>pg_backend_pid()")
if [[ "$clients" != 0 ]]; then
  echo "External database clients remain active ($clients); restore refused." >&2
  exit 2
fi
tables=$("${dc[@]}" exec -T postgres psql -U banana_scene -d banana_scene -Atq -c \
  "SELECT count(*) FROM pg_catalog.pg_tables WHERE schemaname='public'")
if [[ "$tables" != 0 ]]; then
  echo 'Target database is not empty; restore refused.' >&2
  exit 2
fi
"${dc[@]}" run --rm --no-deps -T --entrypoint /bin/sh worker -c \
  'cd /app/backend && /app/.venv/bin/python -m ops.scene_volume_archive check-empty --target /app/scene_assets'
"${dc[@]}" run --rm --no-deps -T --entrypoint /bin/sh backend -c \
  'cd /app/backend && /app/.venv/bin/python -m ops.scene_volume_archive check-empty --target /app/uploads'

cat -- "$backup/database.dump" | "${dc[@]}" exec -T postgres pg_restore \
  -U banana_scene -d banana_scene --exit-on-error --no-owner --no-acl
"${dc[@]}" run --rm --no-deps -T -v "$backup:/backup:ro" --entrypoint /bin/sh worker -c \
  'cd /app/backend && /app/.venv/bin/python -m ops.scene_volume_archive restore --archive /backup/scene-assets.tar --target /app/scene_assets'
"${dc[@]}" run --rm --no-deps -T -v "$backup:/backup:ro" --entrypoint /bin/sh backend -c \
  'cd /app/backend && /app/.venv/bin/python -m ops.scene_volume_archive restore --archive /backup/legacy-uploads.tar --target /app/uploads'
audit_tmp="$runtime_tmp/restored-audit.json"
"${dc[@]}" run --rm --no-deps -T --entrypoint /bin/sh worker -c \
  'cd /app/backend && /app/.venv/bin/python -m ops.scene_asset_audit --check-credentials' > "$audit_tmp"
cmp -- "$backup/scene-audit.json" "$audit_tmp" || {
  echo 'Restored database/assets differ from backup audit; services remain stopped.' >&2
  exit 1
}
"${dc[@]}" exec -T postgres psql -U banana_scene -d banana_scene -v ON_ERROR_STOP=1 -c \
  "UPDATE scene_task_items SET lease_expires_at = now() - interval '1 second' WHERE state='running'"
echo 'Restore verified. Keep the original CREDENTIAL_ENCRYPTION_KEY; start services manually.'
