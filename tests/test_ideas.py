import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from magpie import enrich as N
from magpie import possibilities as P
from magpie.util import write_json


def ev(i, key, headline, what="", **kw):
    e = {"id": i, "key": key, "kind": "repo", "title": key.split(":", 1)[1], "summary": what, "maker": "Acme",
         "topics": ["agents"], "evidence": "reported", "url": f"https://example.com/{i}",
         "brief": {"headline": headline, "newly_possible": what}}
    e.update(kw)
    return e


def idea(title, connection, recipe, chain=()):
    return {"title": title, "connection": connection, "chain": list(chain), "disconfirm": "No clinic uses it.",
            "ingredient_refs": [{"id": f"r{n}", "title": t} for n, t in enumerate(recipe)]}


CLINIC = idea("Small clinics can transcribe visits on one laptop", "A local speech model feeds a local coding model.",
              ["Whisper runs locally", "Flint codes on a laptop"], ["Notes stay on the machine", "Review takes 15 minutes"])
TRADING = idea("Quant desks can backtest with agents overnight", "An agent framework drives a backtester.",
               ["Agent kit", "Backtester"])


class IdeaTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "v1"
        days = {"2099-01-01": [TRADING], "2099-01-03": [CLINIC, idea("", "untitled", [])], "2099-01-05": [idea("Later", "x", [])]}
        for d, items in days.items():
            write_json(self.data / "days" / f"{d}.json", {"date": d, "events": [], "top": [], "possibilities": {"items": items}}, compact=True)
        self.speech = ev("s1", "gh:acme/fast-whisper", "Fast Whisper transcribes speech locally at 10x speed",
                         "Transcribe clinic visits and speech on a laptop at 10x speed.")

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_earlier_ideas_are_the_related_possibilities_before_today(self):
        rows = P.earlier_ideas("2099-01-05", self.data, [self.speech])
        self.assertEqual([r["id"] for r in rows], ["idea-2099-01-03-1"])  # trading shares no word; later and untitled are out
        r = rows[0]
        self.assertEqual((r["kind"], r["today"], r["date"]), ("idea", False, "2099-01-03"))
        self.assertEqual(r["recipe"], ["Whisper runs locally", "Flint codes on a laptop"])
        self.assertEqual(r["_url"], "https://www.cameronsutcliff.com/magpie/2099-01-03")
        self.assertEqual(P.earlier_ideas("2099-01-05", self.data, []), [])
        self.assertEqual(P.earlier_ideas("2099-01-05", self.data, [self.speech], limit=0), [])

    def run_generate(self, flag, answer):
        day = {"date": "2099-01-05", "events": [self.speech, ev("s2", "gh:acme/backtest-agent", "Agent kit backtests quant strategies overnight", "Run a backtester from an agent overnight.")]}
        prompts = []

        def lane(prompt, timeout):
            prompts.append(prompt)
            return json.dumps({"possibilities": answer})
        with mock.patch.dict(os.environ, {P.IDEAS_FLAG: flag, "MAGPIE_PAIR_JUDGE": ""}), mock.patch.dict(N.LANES, {"claude": lane}):
            out = P.generate(day, self.data, check=lambda k: True)
        return out, prompts[0]

    def test_with_the_flag_off_nothing_changes(self):
        out, prompt = self.run_generate("", [])
        self.assertNotIn('"kind": "idea"', prompt)
        self.assertNotIn("builds on it", prompt)
        self.assertNotIn("ideas", out["pool"])

    def test_a_possibility_can_build_on_an_earlier_idea(self):
        base = {"connection": "On 2099-01-03 Magpie suggested local transcription; Fast Whisper at 10x speed makes review take 15 minutes.",
                "chain": ["a", "b", "c"], "effort": "a week", "scores": {"value": 4, "feasibility": 4, "evidence": 3, "novelty": 3}}
        answer = [
            {**base, "title": "Clinics transcribe and review a visit in minutes", "ingredients": ["idea-2099-01-03-1", "s1"]},
            {**base, "title": "Only an idea and nothing new", "ingredients": ["idea-2099-01-03-1"]},
            {**base, "title": "Two ideas, one kept", "ingredients": ["idea-2099-01-03-1", "idea-2099-01-01-1", "s2", "s1"]},
            {**base, "title": "Ungrounded", "connection": "It saves 90 minutes.", "ingredients": ["idea-2099-01-03-1", "s1"]},
        ]
        out, prompt = self.run_generate("1", answer)
        self.assertIn('"kind": "idea"', prompt)
        self.assertIn("builds on it", prompt)
        self.assertEqual(out["pool"]["ideas"], 2)  # clinic for Fast Whisper, trading for the backtest agent
        kept = {p["title"]: p for p in out["items"]}
        self.assertEqual(sorted(kept), ["Clinics transcribe and review a visit in minutes", "Two ideas, one kept"])
        p = kept["Clinics transcribe and review a visit in minutes"]
        self.assertEqual(p["mix"], "builds_on_idea")
        self.assertEqual(p["builds_on"], {"id": "idea-2099-01-03-1", "date": "2099-01-03",
                                          "title": "Small clinics can transcribe visits on one laptop",
                                          "url": "https://www.cameronsutcliff.com/magpie/2099-01-03"})
        ref = next(r for r in p["ingredient_refs"] if r["id"] == "idea-2099-01-03-1")
        self.assertEqual((ref["kind"], ref["today"]), ("idea", False))
        self.assertEqual(kept["Two ideas, one kept"]["ingredients"], ["idea-2099-01-03-1", "s2", "s1"])  # the second idea is dropped
        whys = {d["title"]: d["why"] for d in out["dropped"]}
        self.assertIn("one from today", whys["Only an idea and nothing new"])
        self.assertIn("90", whys["Ungrounded"])


if __name__ == "__main__":
    unittest.main()
