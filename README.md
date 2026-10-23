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
   ripples into other technology. It also names the tools, models or platforms a capability works with; code keeps
   a name only if the event's own titles, summaries or sources spell it and every number in it is theirs. It cannot
   change a score or add a fact.
5. **Publish.** A redaction gate checks the output, then the edition lands in `data/v1/`.

## The feed

| File | What it holds |
|---|---|
| `data/v1/latest.json` | Today's edition |
| `data/v1/days/YYYY-MM-DD.json` | Every past edition |
| `data/v1/index.json` | The archive: date, headline, lead stories, counts |
| `data/v1/constraints.json` | Every official action Magpie has recorded that makes AI harder to train, publish, host, reach or buy |
| `data/v1/threats.json` | Every security threat Magpie has recorded that changes what is safe to run, trust or depend on |
| `data/v1/elements/<slug>.json` | One element across every edition: what it is, where to find it, what it works with, its history |
| `data/v1/elements/index.json` | Every element page, with its canonical key, first and last sighting and counts |

Schemas `magpie.day/1`, `magpie.element/1`, `magpie.elements/1`, `magpie.constraints/1` and `magpie.threats/1` live in
`data/v1/schema/`. Anything downstream
can pull these files; nothing needs this code.

An **element** is one thing Magpie keeps meeting: a repo, model, paper, tool or official action, keyed the way the
editions cluster it (`gh:owner/repo`, `hf:org/model`, `arxiv:id`, `url:...`, `policy:...`). It gets a page when it
appears on two or more days, feeds a new possibility, is an official action, or an official action rests on a document
it carried, cites it, or names its maker. A page shows every appearance, where to find it, which sources spotted it,
the knock-on areas the editor named, the possibilities it fed, what it works with (combined in a possibility, covered
by the same post on the same day, or named by the editor and spelled in the sources), and the actions that touch it.

Every publish rebuilds all the pages from scratch, from the editions since the pages started (`since` in the index;
the first build starts seven editions back, and an earlier edition is read only for the appearance a possibility
ingredient from the library was drawn from). No model is called and nothing is patched in
place, so a page always says what the editions and the constraints register say. Slugs are a pure function of the key
(`gh-owner--repo`, `arxiv-<id>`, `policy-fr-<number>`, else a readable stem plus a hash), so a page's URL never changes.

## New possibilities

Each edition combines capabilities into ideas with a causal chain, a smallest test and what would prove them wrong. Two
pools are ranked together: today with today, and today with the library, which is every capability edited in any earlier
edition. For each of today's strongest arrivals, code retrieves the existing capabilities it relates to most (shared rare
words, shared topics, either naming the other as an integration, the same maker), whatever their age, and keeps only
repos, models and spaces that still exist and are not archived. A quiet day with one new repo still pairs it with what it
lifts. Each idea carries its `mix` and an `amplification` term: 1 plus the adoption (0 to 1) of its most established
ingredient, from days seen, peak signal, stars gained and works-with neighbours before that day. The rank multiplies by
it, and at least one idea of each mix is kept when both were proposed. Code ranks; the model only proposes.

A named integration is read from works_with lists. `MAGPIE_TEXT_NAMED=1` also reads it from what either side's text states
("deploys to vLLM", "configures ... OpenCode"), matching product names whole; it is off by default.

## Threats

A threat is a security event that changes what is safe to run, trust or depend on when building with or running AI: a
compromised certificate authority or signing key, a malicious or hijacked package, a vulnerability in an AI framework,
serving stack, agent toolkit or SDK, a demonstrated attack on models or agents, a breach an AI provider confirms, or a
CISA KEV entry in the AI or developer stack. Each one rests on a primary document (an advisory, a CVE or GHSA record, an
incident report, a root-program bug or the reporter's disclosure site).

The lane also carries model and agent failures builders should be aware of (`model-or-agent-failure`): an agent that
deleted, changed or leaked data, a model release rolled back or pulled for its behavior, a widely reproduced harmful or
broken behavior, an outage that stopped work at scale. Such an item comes from a post or news item that reports one and
is, or links, an article, post-mortem or status page (never a social post alone). It is reported, not advised: no editor
actions, and the severity is the editor's estimate, never critical.

Sources are keyless and primary: the CISA KEV catalog, OSV.dev for a watch list of AI and developer packages (every
page; an advisory only modified recently can update a recorded threat but never start one), Mozilla's CA incident bugs,
and `inbox/threats.json` for items added by hand (watch lists in `config/security.json`). Posts and news that name a
vulnerability and link its advisory join through that link. One cached model call per batch writes the words; candidates
over the per-run limit wait in a queue and the receipt says so.

Code decides every field that states a fact. Severity comes from the source when it states one; otherwise it is the
editor's estimate, capped at high for trust infrastructure and labelled `severity_basis: "editor"`. `exploited` is true
only for a KEV entry or when the primary document says so (the sentence is kept in `exploited_quote`), else null.
`status` comes from the documents (an explicit dispute, a stated fixed version, a stated revocation, else active). Every
package and version pair in `affects` and `fixed_in` is structured data or appears together in one line, table row or
sentence of a source document; `actions` are built from those fixed versions, plus at most two editor actions that each
rest on a verbatim quote. Sightings of the same document merge transitively (union-find over CVE, GHSA and OSV ids, bug
numbers and URLs), and package advisories that belong to an umbrella incident carry its id in `incident` and follow it
in the day's list. The day's list carries what was sighted today, then critical and high threats from the last week;
`data/v1/threats.json` keeps them all.

**Ranked by reach.** Code computes each threat's `reach` from stored fields: each affected package (its full name in its
ecosystem) and each fixed package no affected one names, the ecosystems in `affects`, with those of its nested package
advisories, one for each nested advisory, and 10 more for trust infrastructure. A threat that is trust infrastructure, has a nested advisory or spans two or more
ecosystems is `wide` and ranks with the critical ones whatever its own severity or its basis, so an incident like
eMudhra's (every non-browser TLS client) leads a single-package bug. Every surface shares one order (`rank_key`): that
tier, then reach, then exploited, then a fixed version, then official evidence. The day's list puts new threats first and
breaks remaining ties by its most serious member and newest; `recent.threats` and the site's register fallback break them
by newest. The feed and the index follow the day's list; the lead line reads "Widest reach".

**No blank sections.** When a section's own list is empty (constraints, threats, possibilities, ripples, viewing), the
day's `recent` block carries up to five of the most impactful recent items, each with its date, copied with no model
call from the registers (the last 90 days; an older constraint only while it is in force) and the last 14 editions.
`findings` carries a dated line such as "No new constraint today. Latest: <title> (<date>)" or, for threats, the
widest-reaching item still open; with nothing recent it says when the last one was recorded.

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
python3 -m magpie threats                # add the threats lane and the recent fallbacks to today's edition
python3 -m magpie elements               # rebuild every element page from the editions
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
