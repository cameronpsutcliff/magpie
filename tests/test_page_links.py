"""page_url beyond the day files: index.json leads, the ideas index, rollups, element pages and the feed, and the
published files carry it. Every link is built by pages.py from an id and a date already in the file."""
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from magpie import publish, views as V
from magpie import elements as E
from magpie.util import ROOT, short_hash, write_json

SITE = "https://www.cameronsutcliff.com/magpie"
DATA = ROOT / "data/v1"
K1, K2 = short_hash("gh:acme/kit", 12), short_hash("hf:acme/model", 12)


def event(i, key, headline):
    return {"id": i, "key": key, "kind": "repo", "title": key, "summary": "", "url": f"https://example.com/{i}", "maker": "Acme",
            "vendors": ["Acme"], "topics": [], "evidence": "reported", "signal": 60.0, "impact": 4, "rank_score": 70.0, "metrics": {},
            "sources": [{"name": "HN", "title": headline, "url": f"https://news.example/{i}"}], "mentions": [], "source_groups": 1,
            "brief": {"headline": headline, "newly_possible": f"{headline}, now.", "impact": 4}}


def edition(date, ideas=()):
    return {"schema": "magpie.day/1", "date": date, "generated_at": f"{date}T11:00:00Z", "run": {"where": "home", "enriched": 2},
            "edition": {"headline": "A day", "dek": "Dek."}, "coverage": {"events": 2, "sources_total": 1}, "top": [K1, K2],
            "events": [event(K1, "gh:acme/kit", "Kit runs agents"), event(K2, "hf:acme/model", "Model gets faster")],
            "facets": {"vendors": [["Acme", 2]], "topics": [], "kinds": []}, "possibilities": {"items": list(ideas)}}


class Links(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        idea = {"id": "2099-01-05-teams-run-kits", "title": "Teams run kits", "connection": "Because.", "rank": 9.0, "effort": "a week",
                "ingredients": [K1], "ingredient_refs": [{"id": K1, "title": "Kit", "date": "2099-01-05", "url": "https://example.com/k"},
                                                         {"id": K2, "title": "Model", "date": "2099-01-04", "url": "https://example.com/m"}],
                "builds_on": {"id": "idea-2099-01-04-1", "date": "2099-01-04", "title": "Labs run models"}}
        self.days = {"2099-01-04": edition("2099-01-04", [{"title": "Labs run models", "connection": "Since.", "rank": 5.0, "effort": "a day",
                                                            "ingredients": [K2], "ingredient_refs": [{"id": K2, "title": "Model", "date": "2099-01-04",
                                                                                                      "url": "https://example.com/m"}]}]),
                     "2099-01-05": edition("2099-01-05", [idea])}
        for d, day in self.days.items():
            write_json(self.tmp / "days" / f"{d}.json", day, compact=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_index_json_leads(self):
        entry = publish.index_entry(self.days["2099-01-05"])
        self.assertEqual([t["page_url"] for t in entry["top"]], [f"{SITE}/c/2099-01-05-{K1}", f"{SITE}/c/2099-01-05-{K2}"])

    def test_the_ideas_index(self):
        index = V.ideas_index(V._days(self.tmp))
        x = next(i for i in index["ideas"] if i["id"] == "2099-01-05-teams-run-kits")
        self.assertEqual(x["page_url"], f"{SITE}/i/2099-01-05-teams-run-kits")
        # Each ingredient links its own day's page, a library one included.
        self.assertEqual([i["page_url"] for i in x["ingredients"]], [f"{SITE}/c/2099-01-05-{K1}", f"{SITE}/c/2099-01-04-{K2}"])
        self.assertEqual(x["builds_on"]["page_url"], f"{SITE}/i/2099-01-04-labs-run-models")

    def test_rollups(self):
        days = V._days(self.tmp)
        week = V.rollup("2099-01-05", days, V.ideas_index(days), {})["views"]["week"]
        self.assertTrue(week["top"])
        for c in week["top"]:
            self.assertEqual(c["page_url"], f"{SITE}/c/{c['date']}-{c['event_id']}")
        self.assertEqual({x["page_url"] for x in week["ideas"]}, {f"{SITE}/i/{x['id']}" for x in week["ideas"]})

    def test_element_pages(self):
        a = E.appearance(event(K1, "gh:acme/kit", "Kit runs agents"), self.days["2099-01-05"], [], {})
        self.assertEqual(a["page_url"], f"{SITE}/c/2099-01-05-{K1}")
        found = E.contributions(self.days["2099-01-05"], lambda d: self.days.get(d))
        self.assertEqual(found["gh:acme/kit"]["appearance"]["page_url"], f"{SITE}/c/2099-01-05-{K1}")
        self.assertEqual(found["gh:acme/kit"]["possibilities"][0]["page_url"], f"{SITE}/i/2099-01-05-teams-run-kits")

    def test_the_feed_links_each_lead_to_its_page_after_its_source(self):
        with mock.patch.object(publish, "DATA", self.tmp):
            publish.write_feed()
        feed = json.loads((self.tmp / "feed.json").read_text())
        html = feed["items"][0]["content_html"]
        self.assertIn(f'<li><a href="https://example.com/{K1}">Kit runs agents</a> (<a href="{SITE}/c/2099-01-05-{K1}">on Magpie</a>): '
                      "Kit runs agents, now.</li>", html)
        self.assertLess(html.index(f"https://example.com/{K2}"), html.index(f"{SITE}/c/2099-01-05-{K2}"))  # the source stays first


class Published(unittest.TestCase):
    """The files on disk, after the regeneration."""

    def test_every_item_reference_links_its_page(self):
        index = json.loads((DATA / "index.json").read_text())
        for d in index["days"]:
            for t in d.get("top", []):
                self.assertEqual(t.get("page_url"), f"{SITE}/c/{d['date']}-{t['id']}")
        ideas = json.loads((DATA / "ideas/index.json").read_text())
        for x in ideas["ideas"]:
            self.assertEqual(x.get("page_url"), f"{SITE}/i/{x['id']}")
            for i in x.get("ingredients", []):
                if i.get("id") and i.get("date"):
                    self.assertEqual(i.get("page_url"), f"{SITE}/c/{i['date']}-{i['id']}")
        for path in sorted((DATA / "rollups").glob("*.json")):
            for w in json.loads(path.read_text())["views"].values():
                for c in w["top"]:
                    self.assertEqual(c.get("page_url"), f"{SITE}/c/{c['date']}-{c['event_id']}", path.name)
        for path in sorted((DATA / "elements").glob("*.json")):
            if path.name == "index.json":
                continue
            page = json.loads(path.read_text())
            for a in page.get("appearances", []) + page.get("timeline", []):
                self.assertEqual(a.get("page_url"), f"{SITE}/c/{a['date']}-{a['id']}", path.name)
        feed = json.loads((DATA / "feed.json").read_text())
        self.assertIn('">on Magpie</a>)', feed["items"][0]["content_html"])

    def test_every_published_file_passes_its_schema(self):
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema = lambda n: Draft202012Validator(json.loads((DATA / "schema" / n).read_text()), format_checker=FormatChecker())  # noqa: E731
        day, index, rollup, element, ideas = (schema(n) for n in ("day.schema.json", "index.schema.json", "rollup.schema.json",
                                                                   "element.schema.json", "ideas.schema.json"))
        checks = [(day, p) for p in sorted((DATA / "days").glob("*.json"))] + [(day, DATA / "latest.json"), (index, DATA / "index.json"),
                                                                                (ideas, DATA / "ideas/index.json")]
        checks += [(rollup, p) for p in sorted((DATA / "rollups").glob("*.json"))]
        # Element pages are slow to validate (about 0.3 s each): a fixed spread of them, every one is checked for page_url above.
        checks += [(element, p) for p in [p for p in sorted((DATA / "elements").glob("*.json")) if p.name != "index.json"][::40]]
        for v, path in checks:
            with self.subTest(path.name):
                self.assertEqual([f"{'/'.join(map(str, e.absolute_path))}: {e.message[:80]}" for e in v.iter_errors(json.loads(path.read_text()))], [])


if __name__ == "__main__":
    unittest.main()
