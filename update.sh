#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ $# -gt 1 || ( $# -eq 1 && $1 != --check ) ]]; then
  echo 'Usage: ./update.sh [--check]' >&2; exit 2
fi
if [[ $(git branch --show-current) != main ]]; then
  echo 'Switch to main before updating.' >&2; exit 1
fi
if [[ -n $(git status --porcelain --untracked-files=no) ]]; then
  echo 'Tracked files have local changes. Commit or restore them first.' >&2; exit 1
fi
git fetch origin main
local_commit=$(git rev-parse HEAD)
remote_commit=$(git rev-parse FETCH_HEAD)
if ! git merge-base --is-ancestor "$local_commit" "$remote_commit"; then
  echo 'Local main is ahead or diverged; refusing to overwrite it.' >&2; exit 1
fi
if [[ ${1:-} == --check ]]; then
  if [[ $local_commit == "$remote_commit" ]]; then
    echo "Source is current: $local_commit"
  else
    git log --oneline "$local_commit..$remote_commit"
  fi
  exit 0
fi
[[ -f .env ]] || { echo 'Create .env from .env.example first.' >&2; exit 1; }
docker compose version >/dev/null
git merge --ff-only "$remote_commit"
# Build before replacing the running container. Retry this script if the build fails.
docker compose build bot
docker compose up -d --no-build bot
docker compose ps bot
printf 'Source revision: %s\n' "$(git rev-parse --short HEAD)"
