# Grok Bot: the X lane

Grok Bot runs Magpie's X lane as **Magpie Scout** on its own cloud computer, with no personal computer
involved. It sweeps the roster every 3 hours, checks each post for Community Notes and credible pushback,
keeps the roster current by adding new players and retiring stale or noisy ones, and pushes its files to
`inbox/`. Every Magpie run (cloud and home) reads sweep files from the last 30 hours.

## One-time setup (owner)

1. In the Grok Bot desktop app, create a Bot named **Magpie Scout** and connect the **X** plugin.
2. Create a fine-grained GitHub token: repository access `cameronpsutcliff/magpie` only, permission
   **Contents: read and write**, nothing else. Paste it into the Bot's masked credential field as
   `MAGPIE_GITHUB_TOKEN`. Never paste it into chat.
3. Paste [`magpie-scout-prompt.md`](magpie-scout-prompt.md) into the Bot as its first message. It sets
   itself up, saves two skills (**Magpie X sweep** and **Magpie roster review**), runs once, and either
   schedules its routines or replies with the exact settings to enter: the sweep every 3 hours from 05:00
   to 23:00 Central, the roster review daily at 04:30 Central.

## What the Bot writes

Only files under `inbox/`:

- `inbox/x/<timestamp>.json`: one file per sweep. Each item carries its primary links (so it clusters
  with the same repo, model or paper from other sources), an evidence type, and a `doubt` record:
  Community Notes and credible pushback, rated 0 to 3.
- `inbox/x-roster.json`: players the Bot adds (`trial`, then `active`) or retires, with evidence and
  reasons, plus `source_leads` for newsletters, blogs and channels Magpie does not watch yet.
- `inbox/x-status.json`: the last run, roster counts and any errors.

## How Magpie uses it

- `magpie/collectors/inbox.py` turns sweep items into `social` items. Re-checks of the same post replace
  the earlier version. Doubt lowers trust; at level 2 or higher the note is added to the summary the
  editor reads, and `build.py` marks the event down by 6 points per level and attaches the flag.
- `roster()` merges the curated `config/x-accounts.json` with the Bot's trial and active additions, minus
  its retirements. The home tier's Grok CLI sweeps and the page's "X accounts" count both use it.
- If the Bot has delivered before but nothing arrives for 12 hours, the source shows as down in the
  edition's source health instead of passing as a quiet day.
- The Grok CLI sweeps stay as a fallback and can be switched off with `MAGPIE_SKIP_X=1` once the Bot is
  reliable.
