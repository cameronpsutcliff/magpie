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
        self.assertEqual([r["id"] for r in rows], ["2099-01-03-small-clinics-can-transcribe-visits-on"])  # trading shares no word; later and untitled are out
        r = rows[0]
        self.assertEqual((r["kind"], r["today"], r["date"]), ("idea", False, "2099-01-03"))
        self.assertEqual(r["recipe"], ["Whisper runs locally", "Flint codes on a laptop"])
        self.assertEqual(r["_url"], "https://www.cameronsutcliff.com/magpie/i/2099-01-03-small-clinics-can-transcribe-visits-on")  # the idea's own page
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
        base = {"connection": "On 2099-01-03 Magpie suggested local transcription; Fast Whisper at 10x speed makes it practical.",
                "chain": ["a", "b", "c"], "effort": "a week", "scores": {"value": 4, "feasibility": 4, "evidence": 3, "novelty": 3}}
        answer = [
            {**base, "title": "Clinics transcribe and review a visit in minutes", "ingredients": ["2099-01-03-small-clinics-can-transcribe-visits-on", "s1"]},
            {**base, "title": "Only an idea and nothing new", "ingredients": ["2099-01-03-small-clinics-can-transcribe-visits-on"]},
            {**base, "title": "Two ideas, one kept", "ingredients": ["2099-01-03-small-clinics-can-transcribe-visits-on", "2099-01-01-quant-desks-can-backtest-with-agents", "s2", "s1"]},
            {**base, "title": "Ungrounded", "connection": "It saves 90 minutes.", "ingredients": ["2099-01-03-small-clinics-can-transcribe-visits-on", "s1"]},
            {**base, "title": "The idea's own number", "connection": "Review takes 15 minutes now.", "ingredients": ["2099-01-03-small-clinics-can-transcribe-visits-on", "s1"]},
        ]
        out, prompt = self.run_generate("1", answer)
        self.assertIn('"kind": "idea"', prompt)
        self.assertIn("builds on it", prompt)
        self.assertEqual(out["pool"]["ideas"], 2)  # clinic for Fast Whisper, trading for the backtest agent
        kept = {p["title"]: p for p in out["items"]}
        self.assertEqual(sorted(kept), ["Clinics transcribe and review a visit in minutes", "Two ideas, one kept"])
        p = kept["Clinics transcribe and review a visit in minutes"]
        self.assertEqual(p["builds_on"], {"id": "2099-01-03-small-clinics-can-transcribe-visits-on", "date": "2099-01-03",
                                          "title": "Small clinics can transcribe visits on one laptop",
                                          "url": "https://www.cameronsutcliff.com/magpie/i/2099-01-03-small-clinics-can-transcribe-visits-on"})
        # The idea is what it builds on, not an ingredient: the page never shows it as a capability.
        self.assertEqual(p["ingredients"], ["s1"])
        self.assertEqual([r["id"] for r in p["ingredient_refs"]], ["s1"])
        self.assertEqual(p["mix"], "today_x_today")
        self.assertEqual(kept["Two ideas, one kept"]["ingredients"], ["s2", "s1"])
        self.assertEqual(kept["Two ideas, one kept"]["builds_on"]["id"], "2099-01-03-small-clinics-can-transcribe-visits-on")  # the first idea named
        whys = {d["title"]: d["why"] for d in out["dropped"]}
        self.assertIn("one from today", whys["Only an idea and nothing new"])
        self.assertIn("90", whys["Ungrounded"])
        self.assertIn("15", whys["The idea's own number"])  # an idea's words never vouch for a number

    def test_code_sets_the_id_and_builds_on_never_the_model(self):
        base = {"connection": "Fast Whisper at 10x speed helps.", "chain": ["a", "b", "c"], "effort": "a week",
                "scores": {"value": 4, "feasibility": 4, "evidence": 3, "novelty": 3}}
        answer = [{**base, "title": "Clinics transcribe on laptops", "ingredients": ["s1", "s2"], "id": "evil",
                   "builds_on": {"id": "made-up", "date": "2099-01-01", "title": "Never said", "url": "https://evil.example"}}]
        out, _ = self.run_generate("1", answer)
        p = out["items"][0]
        self.assertEqual(p["id"], "2099-01-05-clinics-transcribe-on-laptops")
        self.assertNotIn("builds_on", p)  # no idea among its ingredients: nothing it builds on

    def test_malformed_earlier_editions_are_skipped(self):
        bad = [{"date": "2099-01-02", "possibilities": ["not", "a", "dict"]},
               {"date": "2099-01-02", "possibilities": {"items": {"title": "a dict, not a list"}}},
               {"possibilities": {"items": [CLINIC]}},  # no date
               {"date": "2099-01-04", "possibilities": {"items": [{"title": "Clinic speech notes", "connection": 7, "chain": "a string",
                                                                    "ingredient_refs": "a string", "disconfirm": ["x"]}, "junk", None]}}]
        for i, d in enumerate(bad):
            write_json(self.data / "days" / f"2099-01-0{2 if i < 3 else 4}.json", d, compact=True)
            rows = P.earlier_ideas("2099-01-05", self.data, [self.speech])
            self.assertTrue(all(r["id"].startswith("2099-01-0") for r in rows))
        self.assertEqual(rows[0], {**rows[0], "id": "2099-01-04-clinic-speech-notes", "what": "", "recipe": [], "limits": "", "_summary": ""})
        days = self.data / "days"
        (days / "2099-01-02.json").write_text('{"date": "2099-01-02", "possibilities": ')  # cut off mid-file
        (days / "2099-01-01.json").write_bytes(b"\xff\xfe not utf-8")
        (days / "2098-12-31.json").write_text("[1, 2, 3]")  # JSON, but not an edition
        write_json(days / "2098-12-30.json", {"date": "2098-12-30", "possibilities": {"items": [CLINIC]}}, compact=True)
        got = P.earlier_ideas("2099-01-05", self.data, [self.speech], editions=3)
        # The three files that cannot be read as editions do not count: the three read are 01-04, 01-03 and 12-30.
        self.assertEqual(sorted(r["id"] for r in got), ["2098-12-30-small-clinics-can-transcribe-visits-on", "2099-01-03-small-clinics-can-transcribe-visits-on", "2099-01-04-clinic-speech-notes"])

    def test_only_the_last_seven_editions_and_six_ideas_newest_first(self):
        shutil.rmtree(self.data)
        for day in range(1, 10):  # nine earlier editions with the same related idea
            write_json(self.data / "days" / f"2099-02-0{day}.json", {"date": f"2099-02-0{day}", "possibilities": {"items": [CLINIC]}}, compact=True)
        rows = P.earlier_ideas("2099-02-10", self.data, [self.speech])
        self.assertEqual([r["date"] for r in rows], [f"2099-02-0{d}" for d in range(9, 3, -1)])  # six, newest first
        rows = P.earlier_ideas("2099-02-10", self.data, [self.speech], limit=20)
        self.assertEqual(len(rows), 7)  # seven editions back, no more

    def test_the_flag_off_prompt_is_the_old_template(self):
        _, prompt = self.run_generate("", [])
        with mock.patch.object(P, "PROMPT", P.PROMPT.replace("{ideas}", "")):
            _, old = self.run_generate("", [])
        self.assertEqual(prompt, old)

if __name__ == "__main__":
    unittest.main()
