import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from magpie import views as V
from magpie.util import write_json


def cap(i, key, headline, impact, signal=50.0, sources=None, **kw):
    e = {"id": i, "key": key, "title": key, "url": f"https://example.com/{i}", "maker": "Acme", "signal": signal,
         "brief": {"headline": headline, "newly_possible": f"{headline}.", "impact": impact},
         "sources": sources if sources is not None else [{"name": "HN", "title": f"{headline} on HN", "url": f"https://news.example/{i}"}]}
    e.update(kw)
    return e


def idea(title, rank, refs=(), **kw):
    return {"title": title, "connection": f"{title} because.", "rank": rank, "effort": "a week",
            "ingredient_refs": [{"id": r, "title": f"Thing {r}", "date": "2099-01-01", "url": f"https://example.com/{r}"} for r in refs], **kw}


def edition(date, events, top=(), ideas=(), threats=(), constraints=(), n=100):
    return {"date": date, "events": events, "top": list(top), "coverage": {"events": n},
            "possibilities": {"items": list(ideas)}, "threats": list(threats), "constraints": {"items": list(constraints)}}


class IdTests(unittest.TestCase):
    def test_an_id_is_the_date_and_the_first_words_of_the_title(self):
        self.assertEqual(V.idea_id("2026-10-23", "SaaS vendors can hand auditors machine-checked proofs, not just findings"),
                         "2026-10-23-saas-vendors-can-hand-auditors-machine")
        self.assertEqual(V.idea_id("2026-10-23", "Café teams run Ollama"), "2026-10-23-cafe-teams-run-ollama")
        self.assertEqual(V.idea_id("2026-10-23", "!!!"), "2026-10-23-idea")
        self.assertEqual(V.idea_id("2026-10-23", "Same words here", {"2026-10-23-same-words-here"}), "2026-10-23-same-words-here-2")

    def test_ids_keep_their_own_and_never_repeat_in_a_day(self):
        items = [{"title": "Run it locally", "id": "2099-01-01-run-it-locally"}, {"title": "Run it locally"}, "junk", {"title": " "},
                 {"title": "Another one"}]
        self.assertEqual(V.ids_for("2099-01-01", items),
                         ["2099-01-01-run-it-locally", "2099-01-01-run-it-locally-2", None, None, "2099-01-01-another-one"])
        day = {"date": "2099-01-01", "possibilities": {"items": items}}
        self.assertEqual(V.assign_ids(day), 2)
        self.assertEqual(items[1]["id"], "2099-01-01-run-it-locally-2")
        self.assertEqual(V.assign_ids({"date": "2099-01-01", "possibilities": None}), 0)


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "v1"
        flint = "hf:recursivesi/flint"
        self.days = {
            "2099-01-01": edition("2099-01-01", [cap("a", flint, "Recursive releases Flint", 5, 80), cap("b", "gh:acme/kit", "Kit ships", 3)],
                                  top=["a"], ideas=[idea("Clinics run speech locally", 3.0, ["a"])],
                                  threats=[{"id": "T1", "title": "Old flaw", "first_seen": "2098-12-01", "severity": "high"}]),
            "2099-01-05": edition("2099-01-05", [cap("c", flint, "Flint measured at 216 tokens per second", 5, 60, reported="2099-01-01"),
                                                 cap("d", "gh:acme/new", "New tool lands", 5, 90, sources=[{"name": "x", "url": "javascript:alert(1)"}]),
                                                 cap("e", "gh:acme/kit", "Kit adds plugins", 4)],
                                  top=["c", "d"],
                                  ideas=[idea("Desks backtest overnight", 9.5, ["d"]),
                                         idea("Clinics review visits in minutes", 4.0, ["c"],
                                              builds_on={"id": "idea-2099-01-01-1", "date": "2099-01-01", "title": "Clinics run speech locally"})],
                                  threats=[{"id": "T2", "title": "New flaw", "first_seen": "2099-01-05", "severity": "critical", "reach": 3},
                                           {"id": "T3", "title": "Part of an incident", "first_seen": "2099-01-05", "incident": "T2"}],
                                  constraints=[{"id": "C1", "headline": "A rule", "first_seen": "2099-01-04", "signal": 40,
                                                "doc_url": "https://gov.example/rule", "status": "proposed"}]),
            "2098-12-10": edition("2098-12-10", [cap("z", "gh:acme/old", "Old thing", 5)], top=["z"]),
        }
        for d, day in self.days.items():
            write_json(self.data / "days" / f"{d}.json", day, compact=True)
        (self.data / "days" / "2099-01-03.json").write_text("{not json")
        write_json(self.data / "elements" / "index.json", {"elements": [{"key": flint, "slug": "hf-recursivesi--flint"}]})

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_the_index_links_ideas_both_ways(self):
        index = V.ideas_index(V._days(self.data))
        ids = [x["id"] for x in index["ideas"]]
        self.assertEqual(ids, ["2099-01-05-desks-backtest-overnight", "2099-01-05-clinics-review-visits-in-minutes",
                               "2099-01-01-clinics-run-speech-locally"])  # newest edition first, then its order
        later, earlier = index["ideas"][1], index["ideas"][2]
        self.assertEqual(later["builds_on"]["id"], "2099-01-01-clinics-run-speech-locally")  # the pool id names its page
        self.assertEqual(earlier["built_on_by"], ["2099-01-05-clinics-review-visits-in-minutes"])
        self.assertEqual(earlier["ingredients"][0]["url"], "https://example.com/a")

    def test_the_week_ranks_by_impact_then_days_on_top_and_keeps_sources(self):
        days = V._days(self.data)
        r = V.rollup("2099-01-05", days, V.ideas_index(days), {"hf:recursivesi/flint": "hf-recursivesi--flint"})
        week, month = r["views"]["week"], r["views"]["month"]
        self.assertEqual((week["from"], week["editions"], week["capabilities"]), ("2098-12-30", 2, 200))
        self.assertEqual(month["editions"], 3)  # 2098-12-10 is outside the week, inside the month
        self.assertEqual([c["key"] for c in week["top"]], ["hf:recursivesi/flint", "gh:acme/new", "gh:acme/kit"])
        flint = week["top"][0]
        # Impact 5 on both days and on top twice; its best appearance is the higher signal one.
        self.assertEqual((flint["days"], flint["top_days"], flint["title"], flint["slug"]), (2, 2, "Recursive releases Flint", "hf-recursivesi--flint"))
        self.assertEqual(flint["sources"], [{"name": "HN", "title": "Recursive releases Flint on HN", "url": "https://news.example/a"}])
        self.assertEqual(week["top"][1]["sources"], [])  # only http links are kept for click-outs
        self.assertEqual([x["id"] for x in week["ideas"]], ["2099-01-05-desks-backtest-overnight", "2099-01-05-clinics-review-visits-in-minutes",
                                                            "2099-01-01-clinics-run-speech-locally"])
        self.assertEqual([t["id"] for t in week["threats"]], ["T2"])  # first seen in the window, not part of an incident
        self.assertEqual(week["constraints"][0]["doc"], "https://gov.example/rule")
        self.assertEqual(len(month["top"]), 4)

    def test_refresh_writes_the_index_and_the_rollup_and_never_raises(self):
        got = V.refresh(self.data)
        self.assertEqual(got, {"ideas": 3, "rollups": 1})
        self.assertTrue((self.data / "ideas" / "index.json").exists())
        self.assertEqual(json.loads((self.data / "rollups" / "2099-01-05.json").read_text())["date"], "2099-01-05")
        from magpie import publish
        with mock.patch.object(publish, "scrub", side_effect=SystemExit("redaction gate blocked")):
            self.assertEqual(V.refresh(self.data), {"error": "SystemExit"})
        with mock.patch.object(V, "rollup", side_effect=RuntimeError("boom")):
            self.assertEqual(V.refresh(self.data), {"error": "RuntimeError"})

    def test_run_gives_ids_to_editions_that_lack_them(self):
        from magpie import publish
        write_json(self.data / "index.json", {"latest": "2099-01-05", "days": []})
        with mock.patch.object(publish, "DATA", self.data):
            V.run(all_dates=True)
        day = json.loads((self.data / "days" / "2099-01-05.json").read_text())
        self.assertEqual([p["id"] for p in day["possibilities"]["items"]],
                         ["2099-01-05-desks-backtest-overnight", "2099-01-05-clinics-review-visits-in-minutes"])
        self.assertEqual(json.loads((self.data / "latest.json").read_text())["date"], "2099-01-05")
        self.assertEqual(sorted(p.name for p in (self.data / "rollups").glob("*.json")),
                         ["2098-12-10.json", "2099-01-01.json", "2099-01-05.json"])


if __name__ == "__main__":
    unittest.main()
