# Magpie

Every shiny new thing in AI, collected each morning and ranked by what it makes possible.

Read it at **[cameronsutcliff.com/magpie](https://cameronsutcliff.com/magpie)**. This repository is the
pipeline and the open feed behind that page.

## What it does

Every morning Magpie reads about 35 public sources: GitHub trending for the day, week and month,
brand-new repos, Hacker News, Hugging Face models, demos and daily papers, lab blogs, newsletters,
product changelogs (Claude Code, Codex, Cursor, Copilot, Ollama, vLLM and more), a YouTube watch
list, and a roster of 72 vetted X accounts read through Grok's live search.

1. **Collect.** Each source is an isolated collector with its own timeout. One broken source never stops the edition.
2. **Cluster.** Mentions of the same repo, model, demo or paper merge into one capability event. Links
   inside newsletters and posts count as corroboration, not as new events.
3. **Score.** Code computes a 0 to 100 signal from novelty, whether you can run it today, momentum
   (stars gained, not totals), independent pickup, source trust and spread across topics.
4. **Edit.** A model writes the words for the strongest candidates (what became possible, why it
   matters, what to try, what is not proven) and rates **impact** from 1 to 5: how far a capability
   ripples into other technology. It cannot change a score or add a fact.
5. **Publish.** A redaction gate checks the output, then the edition lands in `data/v1/`.

## The feed

| File | What it holds |
|---|---|
| `data/v1/latest.json` | Today's edition |
| `data/v1/days/YYYY-MM-DD.json` | Every past edition |
| `data/v1/index.json` | The archive: date, headline, lead stories, counts |
| `data/v1/elements/<slug>.json` | One element across every edition: what it is, where to find it, what it works with, its history |
| `data/v1/elements/index.json` | Every element page, with its canonical key, first and last sighting and counts |

Schemas `magpie.day/1`, `magpie.element/1` and `magpie.elements/1` live in `data/v1/schema/`. Anything downstream
can pull these files; nothing needs this code.

An **element** is one thing Magpie keeps meeting: a repo, model, paper, tool or official action, keyed the way the
editions cluster it (`gh:owner/repo`, `hf:org/model`, `arxiv:id`, `url:...`, `policy:...`). It gets a page when it
appears on two or more days, feeds a new possibility, is an official action, or an official action rests on a document
it carried, cites it, or names its maker. Pages start on a date (`since` in the index): a seed reaches back seven
editions at most, and after that they grow from each day's edition; nothing earlier is read. Pages are computed from the published editions and the constraints
register, with no model call: every appearance, where to find it, which sources spotted it, the knock-on areas the
editor named, the possibilities it fed, what it works with (combined in a possibility, or covered by the same post
on the same day), and the actions that touch it. Each publish folds that day in and replaces any earlier copy of
the same day; `python3 -m magpie elements --rebuild` regenerates them all from the day files and matches a run of
daily updates byte for byte. When an action is added, corrected or removed, every element it could touch is
recomputed from the editions. Pages are bounded: the newest 60 appearances and 60 possibilities in full, two years of
one-line timeline rows, and a folded summary of the rest (counts, extremes, and the actions older sources matched). Slugs are a pure function of the key (`gh-owner--repo`, `arxiv-<id>`,
`policy-fr-<number>`, else a readable stem plus a hash), so a page's URL never changes.

## Where it runs

- **Cloud tier** (`.github/workflows/edition.yml`): GitHub Actions, no personal computer involved. With no
  secrets it publishes a plain edition from every keyless source. Add `CLAUDE_CODE_OAUTH_TOKEN` (from
  `claude setup-token`) or `ANTHROPIC_API_KEY` as a repository secret and the cloud run edits too.
- **Home tier** (`scripts/home-run.sh`): an always-on machine adds the YouTube watch list, the Grok X
  pulse and the editor on existing subscriptions. Its edited edition wins for the day; the cloud tier
  never overwrites it.

## Run it

```bash
python3 -m magpie run --no-llm --cloud   # keyless sources, no editor
python3 -m magpie run                    # everything available on this machine
python3 -m magpie elements --rebuild     # regenerate every element page from the day files
python3 -m unittest discover -s tests
```

Python 3.12, standard library only.

## Every week

```bash
python3 -m magpie discover                    # grow and prune the watch list, write a change log
python3 -m magpie discover --veto feed:<id>   # undo one change and remember the decision
python3 -m magpie review --week               # quality report for the week of the latest edition
```

`scripts/home-run.sh` runs both on Sundays before the edition: review for the last full week, then discover
for the seven days of runs that end on Saturday. It commits what they write (`config/sources.json`,
`config/youtube.json`, `config/candidates.json`, `reports/`) and pushes with the edition. A failure in
either is logged and never blocks the edition.

**Discover** runs on the home tier, where `state/runs/` keeps each day's items. It mines the last seven
days for newsletters and blogs, YouTube channels and GitHub owners that other sources linked or mentioned,
plus the `source_leads` Magpie Scout writes to `inbox/x-roster.json`, then applies two rules:

| Rule | When |
|---|---|
| Promote | Two or more trusted, independent sources cited it within the week. |
| Demote | Zero kept items in its last 21 live editions, or three hype flags in 21 days. Backfilled editions never count toward zero kept. Official lab and vendor feeds (a feed with a `vendor`) are pinned: hype still marks their items down, but they are never benched. |

Trusted means owner-curated: a registry source with trust 0.65 or more, an account in
`config/x-accounts.json` (the pulse and Grok Bot count as one), or a high-priority YouTube channel.
Discovered sources never vouch for others, and neither do Magpie Scout's own roster additions or an X post
with doubt of level 2 or more. A Scout lead counts as cited by the accounts it was seen via.

X accounts are not the loop's: Magpie Scout keeps `inbox/x-roster.json` and `config/x-accounts.json` stays
owner-curated, so discover never promotes or benches an X account. Newsletters, blogs and changelogs become
feeds (only after their feed parses), GitHub owners feed the `github-watched-orgs` collector, and YouTube
channels join `config/youtube.json` at medium priority when a link gave their channel id (a bare handle is
held for you to add by hand). Demoted feeds move to a `benched` list; YouTube channels are pruned by hand
through the `dropped` list. Candidates and their evidence land in `config/candidates.json` and the change
log in `reports/discovery/`; read both and veto what you don't want (vetoes live in `config/vetoes.json`).

**Review** reads the published editions and writes `reports/review/YYYY-Www.md`: each day's top five,
every hype flag, how many leads had a runnable artifact, and which top stories later reached
Corroborated. Pass `--week 2026-W43` or any date in the week for an earlier one.

## Evidence labels

| Label | Rule |
|---|---|
| Reported | One source so far. Treat it as a claim. |
| Runnable | A public repo, model or demo exists, and a second independent source picked it up. |
| Corroborated | Three or more independent sources point at the same thing. |

## License

Code: Apache 2.0. Editorial text in the feed: CC BY 4.0. Titles, links and metrics belong to their sources.
