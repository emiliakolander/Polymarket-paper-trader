#!/usr/bin/env bash
# Persist paperbot state (SQLite DB, reports, dashboard) between independent CI runs
# on a dedicated git branch, rewritten as a single commit each time.
#
#   scripts/paperbot_state.sh restore <branch> <dir>   # fetch branch into <dir> (or start empty)
#   scripts/paperbot_state.sh save    <branch> <dir>   # commit <dir> and force-push to <branch>
#   scripts/paperbot_state.sh reset   <branch>         # delete the branch (fresh start)
set -euo pipefail
cmd="${1:?restore|save|reset}"; branch="${2:?branch}"; dir="${3:-state}"

case "$cmd" in
  restore)
    if git ls-remote --exit-code --heads origin "$branch" >/dev/null; then
      git fetch --depth=1 origin "$branch"
      git worktree add --detach "$dir" FETCH_HEAD
      echo "restored state from branch $branch"
    else
      git worktree add --detach "$dir" HEAD
      git -C "$dir" rm -rf --quiet .
      echo "no state branch $branch yet; starting fresh"
    fi
    mkdir -p "$dir"
    ;;
  save)
    cd "$dir"
    printf '*-wal\n*-shm\n' > .gitignore
    git config user.name "paperbot"
    git config user.email "paperbot@users.noreply.github.com"
    git checkout --quiet --orphan "state-tmp-$$"
    git add -A
    git commit --quiet -m "paperbot state $(date -u +%Y-%m-%dT%H:%MZ)" || { echo "nothing to save"; exit 0; }
    git push --quiet --force origin HEAD:"$branch"
    echo "saved state to branch $branch"
    ;;
  reset)
    git push --quiet origin --delete "$branch" 2>/dev/null || true
    echo "state branch $branch reset"
    ;;
  *) echo "unknown command $cmd" >&2; exit 2 ;;
esac
