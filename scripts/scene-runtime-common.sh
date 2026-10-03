#!/usr/bin/env bash
# Sourced by backup/restore after maintenance checks. No docker inspect environment dumps.
scene_capture_runtime() {
  local output=$1 service filename container containers expected actual
  mkdir -m 700 -p -- "$output"
  for service in backend worker renderer frontend; do
    filename=runtime-manifest.json
    [[ "$service" == frontend ]] && filename=runtime-manifest.txt
    "${dc[@]}" run --rm --no-deps -T --entrypoint /bin/cat "$service" "/opt/scene/$filename" > "$output/$service.runtime"
    # Maintenance run must use the same engine as every existing service container,
    # not a newly built tag that silently replaced an older running image.
    containers=$("${dc[@]}" ps -a -q "$service") || return 1
    while IFS= read -r container; do
      [[ -z "$container" ]] && continue
      [[ "$container" =~ ^[a-f0-9]{12,64}$ ]] || { echo 'Unexpected container identity' >&2; return 1; }
      docker cp "$container:/opt/scene/$filename" "$output/container-runtime.tmp"
      cmp -- "$output/container-runtime.tmp" "$output/$service.runtime" || {
        echo "Existing $service runtime differs from the maintenance image; refused." >&2
        return 1
      }
      rm -f -- "$output/container-runtime.tmp"
    done <<< "$containers"
  done
  "${dc[@]}" run --rm --no-deps -T --entrypoint /bin/cat backend /app/deployment/scene/runtime.lock.json > "$output/runtime.lock.json"
  cmp -- "$root/deployment/scene/runtime.lock.json" "$output/runtime.lock.json" || {
    echo 'Checkout and built runtime locks differ; rebuild/recreate the stack first.' >&2
    return 1
  }
  container=$("${dc[@]}" ps -q postgres)
  [[ "$container" =~ ^[a-f0-9]{12,64}$ ]] || { echo 'Exactly one PostgreSQL container is required' >&2; return 1; }
  expected=$(sed -n 's/^    image: //p' "$root/docker-compose.scene.yml")
  actual=$(docker inspect --format '{{.Config.Image}}' "$container")
  [[ "$actual" == "$expected" && "$actual" == *@sha256:* ]] || {
    echo 'PostgreSQL container does not use the pinned Compose image; refused.' >&2
    return 1
  }
  docker inspect --format '{{.Image}}' "$container" > "$output/postgres.image"
  "${dc[@]}" exec -T postgres postgres --version > "$output/postgres.version"
}

scene_compare_runtime() {
  local expected=$1 actual=$2 name
  for name in backend.runtime worker.runtime renderer.runtime frontend.runtime runtime.lock.json postgres.image postgres.version; do
    [[ -s "$expected/$name" && -s "$actual/$name" ]] && cmp -- "$expected/$name" "$actual/$name" || {
      echo "Missing/different backup runtime ($name); restore refused before database writes." >&2
      return 1
    }
  done
}
