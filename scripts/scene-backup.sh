#!/usr/bin/env bash
# Consistent Scene/legacy backup on a Linux Docker host. Never includes the master key.
set -Eeuo pipefail

if [[ $# -ne 1 || ${SCENE_MAINTENANCE_CONFIRMED:-} != yes ]]; then
  echo 'Usage: SCENE_MAINTENANCE_CONFIRMED=yes scripts/scene-backup.sh NEW_OUTPUT_DIRECTORY' >&2
  echo 'Confirm external database writers are stopped before running.' >&2
  exit 2
fi
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
output=$(realpath -m -- "$1")
if [[ -e "$output" ]]; then
  echo 'Backup output must not already exist.' >&2
  exit 2
fi
dc=(docker compose --project-directory "$root" -f "$root/docker-compose.scene.yml")
"${dc[@]}" version >/dev/null
mapfile -t running < <("${dc[@]}" ps --status running --services)
contains() { local wanted=$1 item; for item in "${running[@]}"; do [[ "$item" == "$wanted" ]] && return 0; done; return 1; }
if ! contains postgres; then
  echo 'PostgreSQL service must be running.' >&2
  exit 2
fi
to_stop=()
for service in frontend backend worker renderer; do
  contains "$service" && to_stop+=("$service")
done
restart() {
  local status=$?
  trap - EXIT
  if ((${#to_stop[@]})); then
    "${dc[@]}" start "${to_stop[@]}" || status=1
  fi
  exit "$status"
}
trap restart EXIT
if ((${#to_stop[@]})); then
  "${dc[@]}" stop "${to_stop[@]}"
fi
clients=$("${dc[@]}" exec -T postgres psql -U banana_scene -d banana_scene -Atq -c \
  "SELECT count(*) FROM pg_stat_activity WHERE datname='banana_scene' AND backend_type='client backend' AND pid<>pg_backend_pid()")
if [[ "$clients" != 0 ]]; then
  echo "External database clients remain active ($clients); backup refused." >&2
  exit 1
fi

mkdir -m 700 -p -- "$output"
source "$root/scripts/scene-runtime-common.sh"
scene_capture_runtime "$output/runtime"
"${dc[@]}" exec -T postgres pg_dump -U banana_scene -d banana_scene -Fc > "$output/database.dump"
"${dc[@]}" run --rm --no-deps -T -v "$output:/backup" --entrypoint /bin/sh worker -c \
  'cd /app/backend && /app/.venv/bin/python -m ops.scene_asset_audit --check-credentials --output /backup/scene-audit.json && /app/.venv/bin/python -m ops.scene_volume_archive backup --source /app/scene_assets --output /backup/scene-assets.tar'
"${dc[@]}" run --rm --no-deps -T -v "$output:/backup" --entrypoint /bin/sh backend -c \
  'cd /app/backend && /app/.venv/bin/python -m ops.scene_volume_archive backup --source /app/uploads --output /backup/legacy-uploads.tar'
cp -- "$root/backend/fonts/manifest.json" "$output/font-manifest.json"
"${dc[@]}" images -q > "$output/images.txt"
(cd -- "$output" && sha256sum database.dump scene-assets.tar legacy-uploads.tar scene-audit.json font-manifest.json images.txt runtime/* > SHA256SUMS && sha256sum -c SHA256SUMS)
printf 'complete\n' > "$output/COMPLETE"
find "$output" -type f -exec chmod 600 {} +
echo "Backup verified at $output. Store CREDENTIAL_ENCRYPTION_KEY separately."
