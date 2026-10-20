#!/bin/bash
# The home tier: runs on an always-on machine with the owner's subscriptions.
# Adds the YouTube watch list, the Grok X pulse and the model editor to the cloud sources,
# then publishes. Its edited edition wins for the day.
set -uo pipefail

cd "$(dirname "$0")/.."
# Machine-specific settings (for example MAGPIE_YT_HOME) live in an untracked file.
[ -f .env.local ] && set -a && . ./.env.local && set +a
export PATH="/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin"
# Pick the GitHub account per call; never switch the machine's active login.
export GH_ACCOUNT="${MAGPIE_GH_ACCOUNT:-cameronpsutcliff}"
export GIT_CONFIG_COUNT=1
export GIT_CONFIG_KEY_0=credential.https://github.com.username
export GIT_CONFIG_VALUE_0="$GH_ACCOUNT"
if command -v gh >/dev/null 2>&1; then
  GITHUB_TOKEN="$(gh auth token --user "$GH_ACCOUNT" 2>/dev/null || true)"
  export GITHUB_TOKEN
fi

log="state/home-run.log"
mkdir -p state
echo "=== $(date '+%F %T %Z') home run" >>"$log"

git pull --rebase -X theirs --quiet >>"$log" 2>&1 || echo "pull failed; continuing on local main" >>"$log"

# Sundays, before the edition: review the last full week, then run discovery over the seven days of runs
# that end yesterday. Neither may block the edition. Only a step that succeeded is committed; a failed one
# has everything it may have written put back.
weekly_undo() {
  git checkout -- "$@" >>"$log" 2>&1
  git clean -fdq -- "$@" >>"$log" 2>&1
}
if [ "$(date +%u)" = 7 ]; then
  echo "--- weekly review and discovery" >>"$log"
  last_week="$(date -v-7d +%F 2>/dev/null || date -d '7 days ago' +%F)"
  yesterday="$(date -v-1d +%F 2>/dev/null || date -d yesterday +%F)"
  review_out="reports/review"
  discover_out="config/sources.json config/youtube.json config/candidates.json reports/discovery"
  if python3 -m magpie review --week "$last_week" >>"$log" 2>&1; then
    git add $review_out
  else
    echo "review failed; the edition goes ahead" >>"$log"
    weekly_undo $review_out
  fi
  if python3 -m magpie discover --date "$yesterday" >>"$log" 2>&1; then
    git add $discover_out
  else
    echo "discover failed; the edition goes ahead" >>"$log"
    weekly_undo $discover_out
  fi
  git diff --cached --quiet || git commit -qm "weekly review and discovery $(date +%F)"
fi

if ! python3 -m magpie run >>"$log" 2>&1; then
  echo "run failed" >>"$log"
  exit 1
fi
git add data state/history.json
if git diff --cached --quiet; then
  echo "nothing new to publish" >>"$log"
  # A Sunday's weekly commit still goes out on its own.
  [ -n "$(git log --oneline '@{u}..HEAD' 2>/dev/null)" ] || exit 0
else
  git commit -qm "edition $(date +%F) (home, edited)"
fi
for i in 1 2 3; do
  if git push -q >>"$log" 2>&1; then
    echo "published" >>"$log"
    exit 0
  fi
  git pull --rebase -X theirs -q >>"$log" 2>&1
done
echo "push failed" >>"$log"
exit 1
