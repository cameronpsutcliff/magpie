import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from magpie import review
from magpie.collectors import github


def ev(key, kind="repo", evidence="reported", hype=False, sources=("GitHub trending (day)",), url=None, mentions=()):
    owner_repo = key.split(":", 1)[1]
    return {"id": key, "key": key, "kind": kind, "title": owner_repo, "evidence": evidence, "impact": 3, "signal": 50.0,
            "maker": "Open source", "url": url or f"https://example.com/{owner_repo}",
            "sources": [{"name": n, "url": url or "https://example.com"} for n in sources],
            "mentions": [{"name": "Newsletter", "url": u} for u in mentions],
            "brief": {"headline": f"Headline {owner_repo}", "hype": hype}}


def day(date, events, top=None, enriched=26):
    return {"date": date, "run": {"enriched": enriched}, "edition": {"headline": f"Edition {date}"},
            "events": events, "top": top if top is not None else [e["id"] for e in events[:5]]}


class ReviewTests(unittest.TestCase):
    def days(self):
        lead = ev("gh:acme/agent", url="https://github.com/Acme/agent")
        paper = ev("arxiv:2610.00001", kind="paper", mentions=["https://github.com/lab/code"])
        claim = ev("url:x.com/lab/status/1", kind="social", hype=True, sources=("X: @lab",))
        news = ev("url:news.example/launch", kind="news", evidence="reproduced")
        flagged = ev("gh:hype/swarm", hype=True)
        return {
            # ISO week 2026-W43 runs Monday 2026-10-19 to Sunday 2026-10-25.
            "2026-10-18": day("2026-10-18", [ev("gh:old/thing", evidence="reported")]),
            "2026-10-19": day("2026-10-19", [lead, paper, claim, flagged], top=[lead["id"], paper["id"], claim["id"]]),
            "2026-10-20": day("2026-10-20", [news, dict(lead, evidence="reproduced"), flagged], top=[news["id"]]),
            # The next week: the lead story picks up a third source.
            "2026-10-27": day("2026-10-27", [dict(lead, evidence="corroborated")], top=[]),
        }

    def test_parse_week(self):
        self.assertEqual(review.parse_week(None, "2026-10-20"), (2026, 43))
        self.assertEqual(review.parse_week("2026-W42", "2026-10-20"), (2026, 42))
        self.assertEqual(review.parse_week("2026-10-25", "2026-10-20"), (2026, 43))
        self.assertEqual(review.week_dates(2026, 43)[0], "2026-10-19")
        with self.assertRaises(SystemExit):
            review.parse_week("last week", "2026-10-20")

    def test_artifact(self):
        self.assertEqual(review.artifact(ev("gh:acme/agent", url="https://github.com/Acme/agent")), "https://github.com/Acme/agent")
        self.assertEqual(review.artifact(ev("hf:org/model", kind="social", url="https://x.com/org/status/9")),
                         "https://huggingface.co/org/model")
        self.assertEqual(review.artifact(ev("arxiv:2610.00001", kind="paper", mentions=["https://github.com/lab/code"])),
                         "https://github.com/lab/code")
        self.assertIsNone(review.artifact(ev("url:news.example/launch", kind="news")))

    def test_week_report(self):
        text, card = review.review(self.days(), 2026, 43)
        self.assertEqual((card["editions"], card["leads"], card["leads_runnable"]), (2, 2, 1))
        self.assertEqual((card["slots"], card["slots_runnable"]), (4, 2))
        self.assertEqual((card["hype_events"], card["hype_in_top"]), (2, 1))
        self.assertEqual((card["corroborated_later"], card["not_corroborated"]), (1, 3))
        self.assertIn("Headline acme/agent** led 2026-10-19 as Reported; corroborated on 2026-10-27", text)
        self.assertIn("Made a top five: YES", text)
        self.assertIn("Partial week", text)
        self.assertNotIn("old/thing", text)
        self.assertNotIn(chr(0x2014), text)

    def test_empty_week(self):
        text, card = review.review(self.days(), 2026, 30)
        self.assertEqual(card["editions"], 0)
        self.assertIn("No editions were published", text)

    def test_run_writes_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "data/v1/days").mkdir(parents=True)
            for d, obj in self.days().items():
                (root / f"data/v1/days/{d}.json").write_text(json.dumps(obj))
            path, card = review.run(root, "2026-W43")
            self.assertEqual(path, root / "reports/review/2026-W43.md")
            self.assertTrue(path.read_text().startswith("# Magpie weekly review, 2026-W43"))
            # Without a week, the report covers the week of the latest edition.
            self.assertEqual(review.run(root)[1]["week"], "2026-W44")


class WatchedOrgsTests(unittest.TestCase):
    def test_watched_orgs_queries_owners_in_chunks(self):
        repo = {"full_name": "acme/agent", "html_url": "https://github.com/acme/agent", "description": "An LLM agent",
                "created_at": "2099-01-01T00:00:00Z", "stargazers_count": 40, "topics": []}
        with mock.patch.object(github, "fetch_json", return_value={"items": [repo]}) as fetch:
            items = github.watched_orgs([f"org{i}" for i in range(8)], chunk=6)
        self.assertEqual(fetch.call_count, 2)
        self.assertIn("user%3Aorg0", fetch.call_args_list[0].args[0])
        self.assertEqual(items[0].keys, ["gh:acme/agent"])
        self.assertTrue(items[0].extra["ai"])


if __name__ == "__main__":
    unittest.main()
