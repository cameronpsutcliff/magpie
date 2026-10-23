import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from magpie import enrich as N
from magpie import followups as F
from magpie import publish as PUB
from magpie import read as R
from magpie.util import write_json


def ev(i, key, headline=None, **kw):
    e = {"id": i, "key": key, "kind": "model", "title": key.split(":", 1)[1], "signal": 50.0, "evidence": "reported",
         "summary": f"{key} summary.", "vendors": ["Acme"], "maker": "Acme", "topics": ["agents"], "metrics": {},
         "url": "https://example.com", "created_at": None, "license": None, "sources": [{"name": "HF", "url": "https://example.com", "title": f"{key} source"}],
         "mentions": []}
    if headline:
        e["brief"] = {"headline": headline, "newly_possible": f"{headline}.", "impact": 3}
    e.update(kw)
    return e


FLINT = "hf:recursivesi/flint"
MINIMAX = "hf:minimaxai/minimax-m3.1-flash"


class FollowUpTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "v1"
        days = {"2099-01-01": [ev("a1", FLINT, "Recursive releases Flint"), ev("a2", "gh:acme/listed")],
                "2099-01-03": [ev("b1", FLINT, "Recursive releases Flint, a 40B coding model"), ev("b2", "gh:acme/listed")],
                "2099-01-04": [ev("c1", "gh:acme/later", "Later thing")]}
        for d, events in days.items():
            write_json(self.data / "days" / f"{d}.json", {"date": d, "events": events, "top": [events[0]["id"]]}, compact=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_reported_is_the_latest_earlier_edition_that_wrote_it_up(self):
        got = F.reported("2099-01-04", self.data)
        self.assertEqual(got, {FLINT: "2099-01-03"})  # listed only does not count; the same day does not count
        events = [ev("x", FLINT), ev("y", "gh:acme/listed"), ev("z", "gh:acme/new", reported="2099-01-02")]
        self.assertEqual(F.mark(events, "2099-01-04", self.data), 1)
        self.assertEqual([e.get("reported") for e in events], ["2099-01-03", None, None])  # a stale stamp is cleared
        self.assertEqual(F.reported("2099-01-02", self.data), {FLINT: "2099-01-01"})

    def test_names_are_whole_repo_or_model_names(self):
        flint, sage = ev("f", FLINT), ev("s", "hf:internscience/sage-a1.5")
        self.assertEqual(F.names(flint), {"flint"})
        self.assertEqual(F.named({"headline": "Verify code, run Flint locally, and more", "dek": ""}, [flint]), [FLINT])
        self.assertEqual(F.named({"headline": "Serve MiniMax fast", "dek": "Flintstone tools landed."}, [flint]), [])
        self.assertEqual(F.named({"headline": "", "dek": "Sage-A1.5 tops a benchmark."}, [sage]), [sage["key"]])
        self.assertEqual(F.names(ev("u", "url:example.com/post")), set())  # a page has no name to check
        self.assertEqual(F.names(ev("g", "gh:acme/agents")), set())  # too generic to mean one capability

    def test_the_editor_sees_the_date_and_the_rule(self):
        rows = json.loads(N._payload([ev("f", FLINT, reported="2099-01-03"), ev("m", MINIMAX)]))
        self.assertEqual([r.get("reported") for r in rows], ["2099-01-03", None])
        self.assertIn('never name or describe a follow-up', N.PROMPT)
        self.assertIn('"reported"', R.PROMPT)
        self.assertEqual(R._row(ev("f", FLINT, "Flint at 216 tokens per second", reported="2099-01-03"))["reported"], "2099-01-03")
        self.assertNotIn("reported", R._row(ev("m", MINIMAX, "MiniMax M3.1 Flash")))

    def run_editor(self, replies):
        calls = []

        def lane(prompt, timeout):
            calls.append(prompt)
            return replies[len(calls) - 1]
        events = [ev("f", FLINT, reported="2099-01-03"), ev("m", MINIMAX)]
        with mock.patch.dict(N.LANES, {"fake": lane}), mock.patch.dict(os.environ, {"MAGPIE_LANES": "fake"}):
            edition, receipt = N.enrich(events, "2099-01-04")
        return edition, receipt, calls

    BRIEFS = [{"id": "f", "headline": "Flint runs at 216 tokens per second", "newly_possible": "x", "impact": 3},
              {"id": "m", "headline": "MiniMax M3.1 Flash serves code fast", "newly_possible": "y", "impact": 4}]

    def test_a_headline_that_names_a_follow_up_is_written_again(self):
        first = json.dumps({"edition": {"headline": "Run Flint locally, serve MiniMax", "dek": "Flint arrived.", "quiet": False}, "events": self.BRIEFS})
        again = json.dumps({"edition": {"headline": "Serve MiniMax M3.1 Flash fast", "dek": "One open coding model led.", "quiet": True}})
        edition, receipt, calls = self.run_editor([first, again])
        self.assertEqual(len(calls), 2)
        self.assertEqual(edition["headline"], "Serve MiniMax M3.1 Flash fast")
        self.assertEqual(receipt["follow_ups_named"], [FLINT])
        self.assertTrue(receipt["edition_rewritten"])
        self.assertIn("ALREADY REPORTED", calls[1])
        self.assertIn('not its stories: "Run Flint locally, serve MiniMax"', calls[1])
        self.assertNotIn("Flint runs at 216", calls[1].split("ALREADY REPORTED:")[0])  # the follow-up is offered only as told

    def test_a_clean_headline_costs_no_second_call_and_a_failed_rewrite_keeps_the_words(self):
        clean = json.dumps({"edition": {"headline": "Serve MiniMax fast", "dek": "A day.", "quiet": False}, "events": self.BRIEFS})
        edition, receipt, calls = self.run_editor([clean])
        self.assertEqual((len(calls), edition["headline"]), (1, "Serve MiniMax fast"))
        self.assertNotIn("edition_rewritten", receipt)
        named = json.dumps({"edition": {"headline": "Run Flint locally", "dek": "A day.", "quiet": False}, "events": self.BRIEFS})
        still = json.dumps({"edition": {"headline": "Run Flint at last", "dek": "Again.", "quiet": False}})
        edition, receipt, calls = self.run_editor([named, still])
        self.assertEqual(edition["headline"], "Run Flint locally")
        self.assertFalse(receipt["edition_rewritten"])

    def test_rewrite_keeps_only_grounded_words(self):
        new, old = [ev("m", MINIMAX, "MiniMax M3.1 Flash serves code fast")], [ev("f", FLINT, "Flint runs at 216 tokens per second", reported="2099-01-03")]
        answers = iter([
            "not json",
            json.dumps({"edition": {"headline": "Serve MiniMax 40x faster", "dek": "A day."}}),  # 40 is in no story
            json.dumps({"edition": {"headline": "Serve MiniMax fast", "dek": "One model led.", "quiet": "yes"},
                        "follow_ups": [{"id": "f", "headline": "Flint hits 216 tokens per second", "newly_possible": "A community run measured it."},
                                       {"id": "f", "headline": "dup", "newly_possible": "dup"}, {"id": "zzz", "headline": "x", "newly_possible": "y"}]}),
        ])
        with mock.patch.dict(N.LANES, {"fake": lambda p, t: next(answers)}):
            got = F.rewrite("2099-01-04", new, old, follow_ups=True, lanes=["fake", "fake", "fake"])
        self.assertEqual(got["edition"], {"headline": "Serve MiniMax fast", "dek": "One model led.", "quiet": False})
        self.assertEqual(got["follow_ups"], {"f": {"headline": "Flint hits 216 tokens per second", "newly_possible": "A community run measured it."}})
        bad = json.dumps({"edition": {"headline": "Serve MiniMax", "dek": "Fine."},
                          "follow_ups": [{"id": "f", "headline": "Flint hits 900 tokens per second", "newly_possible": "x"}]})
        with mock.patch.dict(N.LANES, {"fake": lambda p, t: bad}):
            self.assertEqual(F.rewrite("2099-01-04", new, old, follow_ups=True, lanes=["fake"])["follow_ups"], {})
        with mock.patch.dict(N.LANES, {"fake": lambda p, t: "[]"}):
            self.assertIsNone(F.rewrite("2099-01-04", new, old, lanes=["fake"]))

    def published(self):
        day = {"date": "2099-01-04", "top": ["m", "f"],
               "edition": {"headline": "Serve MiniMax, run Flint locally", "dek": "Flint arrived.", "quiet": False, "read_minutes": 3},
               "events": [ev("m", MINIMAX, "MiniMax M3.1 Flash serves code fast"),
                          ev("f", FLINT, "Recursive releases Flint, a fast coding model", summary="Flint runs at 216 tokens per second on one GPU."),
                          ev("o", "gh:acme/other", "Other tool")]}
        return day

    def test_a_published_day_gets_a_new_headline_dek_and_follow_up_words(self):
        day = self.published()
        reply = json.dumps({"edition": {"headline": "Serve MiniMax M3.1 Flash fast, and more", "dek": "One open coding model led.", "quiet": False},
                            "follow_ups": [{"id": "f", "headline": "Flint measured at 216 tokens per second", "newly_possible": "A run on one GPU measured it."}]})
        calls = []
        with mock.patch.dict(N.LANES, {"fake": lambda p, t: calls.append(p) or reply}):
            receipt = F.edit_day(day, self.data, lanes=["fake"])
        self.assertTrue(receipt["changed"])
        self.assertEqual(receipt["before"]["headline"], "Serve MiniMax, run Flint locally")
        self.assertEqual(day["edition"]["headline"], "Serve MiniMax M3.1 Flash fast, and more")
        self.assertEqual(day["edition"]["read_minutes"], 3)  # the rest of the edition is untouched
        flint = next(e for e in day["events"] if e["id"] == "f")
        self.assertEqual((flint["reported"], flint["brief"]["headline"]), ("2099-01-03", "Flint measured at 216 tokens per second"))
        self.assertIn('"headline_then": "Recursive releases Flint, a 40B coding model"', calls[0])
        self.assertIn('imperative verbs in sentence case, but not its stories: "Serve MiniMax, run Flint locally"', calls[0])
        self.assertEqual(next(e for e in day["events"] if e["id"] == "o")["brief"]["headline"], "Other tool")

    def test_a_day_without_follow_ups_up_top_is_left_alone(self):
        day = self.published()
        day["top"], day["edition"]["headline"], day["edition"]["dek"] = ["m", "o"], "Serve MiniMax", "A day."
        with mock.patch.dict(N.LANES, {"fake": mock.Mock(side_effect=AssertionError("no call"))}):
            self.assertFalse(F.edit_day(day, self.data, lanes=["fake"])["changed"])

    def test_a_follow_up_the_headline_names_outside_the_top_is_rewritten_too(self):
        day = self.published()
        day["top"] = ["m", "o"]
        reply = json.dumps({"edition": {"headline": "Serve MiniMax M3.1 Flash fast", "dek": "One open coding model led.", "quiet": False},
                            "follow_ups": [{"id": "f", "headline": "Flint measured at 216 tokens per second", "newly_possible": "A run on one GPU measured it."}]})
        calls = []
        with mock.patch.dict(N.LANES, {"fake": lambda p, t: calls.append(p) or reply}):
            receipt = F.edit_day(day, self.data, lanes=["fake"])
        self.assertTrue(receipt["changed"])
        self.assertIn('"headline_then": "Recursive releases Flint, a 40B coding model"', calls[0].split("ALREADY REPORTED:")[1])
        flint_says_flint = json.dumps({"edition": {"headline": "Run Flint again", "dek": "x"}})
        day = self.published()
        day["top"] = ["m", "o"]
        with mock.patch.dict(N.LANES, {"fake": lambda p, t: flint_says_flint}):
            self.assertFalse(F.edit_day(day, self.data, lanes=["fake"])["changed"])  # still naming it: nothing applied
        self.assertEqual(day["edition"]["headline"], "Serve MiniMax, run Flint locally")

    def test_run_for_rewrites_the_day_latest_and_index_only(self):
        day = self.published()
        write_json(self.data / "days" / "2099-01-04.json", day, compact=True)
        write_json(self.data / "latest.json", day, compact=True)
        entry = {"date": "2099-01-04", "headline": "Serve MiniMax, run Flint locally", "top": []}
        write_json(self.data / "index.json", {"latest": "2099-01-04", "days": [entry, {"date": "2099-01-03", "headline": "Old"}]})
        reply = json.dumps({"edition": {"headline": "Serve MiniMax M3.1 Flash fast", "dek": "One open coding model led.", "quiet": False},
                            "follow_ups": [{"id": "f", "headline": "Flint measured at 216 tokens per second", "newly_possible": "A run on one GPU measured it."}]})
        read = {"items": [{"lead": "MiniMax serves code fast.", "body": "", "ids": ["m"]}], "lane": "fake"}
        with mock.patch.object(PUB, "DATA", self.data), mock.patch.dict(N.LANES, {"fake": lambda p, t: reply}), \
                mock.patch.dict(os.environ, {"MAGPIE_LANES": "fake"}), mock.patch.object(R, "generate", return_value=read), \
                mock.patch.object(PUB, "refresh_elements"), mock.patch.object(PUB, "write_feed"), \
                mock.patch.object(PUB, "index_entry", side_effect=lambda d: {"date": d["date"], "headline": d["edition"]["headline"]}):
            F.run_for("2099-01-04")
        for name in ("days/2099-01-04.json", "latest.json"):
            saved = json.loads((self.data / name).read_text())
            self.assertEqual(saved["edition"]["headline"], "Serve MiniMax M3.1 Flash fast")
            self.assertEqual(saved["read"], read)
        index = json.loads((self.data / "index.json").read_text())
        self.assertEqual([d["headline"] for d in index["days"]], ["Serve MiniMax M3.1 Flash fast", "Old"])


if __name__ == "__main__":
    unittest.main()
