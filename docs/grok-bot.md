# Grok Bot: the X lane

Grok Bot runs Magpie's X sweeps on its own cloud computer, with no personal computer involved. Each run
searches the roster in `config/x-accounts.json`, writes one JSON file to `inbox/x/`, and pushes it. Every
Magpie run (cloud and home) reads files from the last 30 hours.

## One-time setup (owner)

1. In the Grok Bot desktop app, create a Bot named **Magpie Scout** and connect the **X** plugin.
2. Create a fine-grained GitHub token: repository access `cameronpsutcliff/magpie` only, permission
   **Contents: read and write**, nothing else. Paste it into the Bot's masked credential field as
   `MAGPIE_GITHUB_TOKEN`. Never paste it into chat.
3. Save the skill below as **Magpie X sweep**.
4. Add a routine to Magpie Scout: run the skill every 3 hours from 05:00 to 23:00 Central.

## Skill: Magpie X sweep

```
Goal: find what became newly possible in AI on X since the last run, for Magpie's daily brief.

1. In /workspace, clone https://github.com/cameronpsutcliff/magpie (or pull if it exists), using the
   MAGPIE_GITHUB_TOKEN credential for https.
2. Read config/x-accounts.json. Group accounts by "group" (labs, devtools, opensource, research,
   builders, applied). Skip accounts with signal_to_noise below 3.
3. For each group, use the X plugin to search posts from that group's handles in the last 4 hours, plus
   posts they quote or repost. Keep only NEW capabilities: model or weight releases, repos, agent and
   coding-tool features, techniques, measured results, evals. Skip opinion, fundraising, hiring,
   politics, memes and engagement bait. Merge posts about the same thing.
4. Write inbox/x/<UTC timestamp YYYY-MM-DDTHH-MM>.json with exactly this shape:
   {"ran_at": "<ISO 8601 UTC>", "bot": "magpie-scout", "items": [
     {"title": "short neutral headline", "url": "https://x.com/<handle>/status/<id>",
      "handle": "@...", "group": "<group>", "summary": "one or two plain sentences on what is newly possible",
      "links": ["repo, model, paper or product URLs in the post"], "vendor": "company or project",
      "posted_at": "<ISO 8601>"}]}
   Up to 40 items. No em-dash characters. If nothing qualifies, write an empty items list.
5. git add the file, commit with the message "x sweep <timestamp>", pull --rebase, and push.
   Never change any other file.
6. Reply with the item count and the three strongest titles.
```

## How Magpie uses it

`magpie/collectors/inbox.py` turns each item into a `social` item; posts that link a repo, model or paper
cluster with that capability. The home tier's Grok CLI sweeps stay as a fallback and can be switched off
with `MAGPIE_SKIP_X=1` once the Bot is reliable.
