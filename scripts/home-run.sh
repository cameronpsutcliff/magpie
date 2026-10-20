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
if ! python3 -m magpie run >>"$log" 2>&1; then
  echo "run failed" >>"$log"
  exit 1
fi
git add data state/history.json
if git diff --cached --quiet; then
  echo "nothing new to publish" >>"$log"
  exit 0
fi
git commit -qm "edition $(date +%F) (home, edited)"
for i in 1 2 3; do
  if git push -q >>"$log" 2>&1; then
    echo "published" >>"$log"
    exit 0
  fi
  git pull --rebase -X theirs -q >>"$log" 2>&1
done
echo "push failed" >>"$log"
exit 1
