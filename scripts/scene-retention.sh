#!/usr/bin/env bash
# Explicit, offline Scene retention. Never restart services automatically.
set -Eeuo pipefail
umask 077
usage() {
  echo 'Usage: SCENE_MAINTENANCE_CONFIRMED=yes bash scripts/scene-retention.sh plan NEW_PLAN.json' >&2
  echo '       SCENE_MAINTENANCE_CONFIRMED=yes bash scripts/scene-retention.sh apply PLAN.json SHA256 NEW_RECEIPT.json' >&2
  echo '       SCENE_MAINTENANCE_CONFIRMED=yes bash scripts/scene-retention.sh resume RUN_ID NEW_RECEIPT.json' >&2
  echo 'Stop external writers, finish/cancel queued tasks, and take a verified backup first.' >&2
  exit 2
}
[[ ${SCENE_MAINTENANCE_CONFIRMED:-} == yes && $# -ge 2 ]] || usage
mode=$1
case "$mode" in
  plan)
    [[ $# == 2 ]] || usage
    output=$(realpath -m -- "$2")
    args=(plan --output "/maintenance/$(basename -- "$output")")
    ;;
  apply)
    [[ $# == 4 && $3 =~ ^[0-9a-f]{64}$ ]] || usage
    plan=$(realpath -e -- "$2")
    output=$(realpath -m -- "$4")
    [[ -f "$plan" && $(dirname -- "$plan") == "$(dirname -- "$output")" ]] || {
      echo 'Plan and receipt must be in the same private directory.' >&2; exit 2;
    }
    args=(apply --plan "/maintenance/$(basename -- "$plan")" --confirm-sha256 "$3" --receipt "/maintenance/$(basename -- "$output")")
    ;;
  resume)
    [[ $# == 3 && $2 =~ ^[0-9a-f-]{36}$ ]] || usage
    output=$(realpath -m -- "$3")
    args=(resume --run-id "$2" --receipt "/maintenance/$(basename -- "$output")")
    ;;
  *) usage ;;
esac
[[ -d $(dirname -- "$output") && ! -e "$output" && ! -L "$output" ]] || {
  echo 'Output needs an existing private directory and a new filename.' >&2; exit 2;
}
root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
dc=(docker compose --project-directory "$root" -f "$root/docker-compose.scene.yml")
"${dc[@]}" version >/dev/null
mapfile -t running < <("${dc[@]}" ps --status running --services)
[[ " ${running[*]} " == *' postgres '* ]] || { echo 'PostgreSQL must be running.' >&2; exit 2; }
# No restart trap: failures after the durable journal commit require resume.
"${dc[@]}" stop frontend backend worker renderer
trap 'echo "Services remain stopped. On failure inspect scene_maintenance_runs and resume db_pruned runs before restarting." >&2' EXIT
clients=$("${dc[@]}" exec -T postgres psql -U banana_scene -d banana_scene -Atq -c \
  "SELECT count(*) FROM pg_stat_activity WHERE datname='banana_scene' AND backend_type='client backend' AND pid<>pg_backend_pid()")
[[ "$clients" == 0 ]] || { echo 'External database clients remain; retention refused.' >&2; exit 1; }
"${dc[@]}" run --rm --no-deps -T -w /app/backend \
  -e SCENE_MAINTENANCE_CONFIRMED=yes -v "$(dirname -- "$output"):/maintenance" \
  --entrypoint /app/.venv/bin/python worker -m ops.scene_retention "${args[@]}"
echo "Retention $mode result saved at $output. Review before any next step; restart manually only after success."
