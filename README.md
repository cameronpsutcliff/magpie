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

Schema `magpie.day/1`. Anything downstream can pull these files; nothing needs this code.

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
python3 -m unittest discover -s tests
```

Python 3.12, standard library only.

## Evidence labels

| Label | Rule |
|---|---|
| Reported | One source so far. Treat it as a claim. |
| Runnable | A public repo, model or demo exists, and a second independent source picked it up. |
| Corroborated | Three or more independent sources point at the same thing. |

## License

Code: Apache 2.0. Editorial text in the feed: CC BY 4.0. Titles, links and metrics belong to their sources.
