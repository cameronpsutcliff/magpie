import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from magpie import library as L
from magpie import pairing as J
from magpie import possibilities as P
from magpie.util import short_hash, write_json

LONG_DASH = chr(0x2014)


def ev(key, headline, *, what="", why="", topics=("agents",), maker="Open source", kind="repo", sources=(), signal=50.0):
    return {"id": short_hash(key, 12), "key": key, "kind": kind, "title": headline, "summary": what, "maker": maker,
            "url": "https://example.com/" + key.split(":", 1)[1], "topics": list(topics), "evidence": "reported",
            "signal": signal, "metrics": {}, "sources": [{"url": u, "name": "post", "title": "a post"} for u in sources],
            "brief": {"headline": headline, "newly_possible": what, "why": why, "limits": "", "works_with": []}}


def day(date, events, possibilities=()):
    return {"schema": "magpie.day/1", "date": date, "events": events, "top": [],
            "possibilities": {"items": [{"ingredients": list(p)} for p in possibilities]}}


# The cases the rule-only scorer got wrong or right in the Oct 2026 previews, as fixtures for the aggregation.
UNSLOTH = ev("gh:unslothai/unsloth", "Unsloth runs and trains models in a local UI", what="Run and fine-tune open models locally.")
OLLAMA = ev("gh:ollama/ollama", "Ollama runs open models on your machine", what="Pull and run open models locally.")
RIVAL = ev("hf:other/rival-9b", "Rival releases a 9B open checkpoint", what="Download a 9B open checkpoint.", kind="model")
ATTICUS = ev("gh:acme/atticus", "Atticus: hold-to-talk Mac assistant on Claude", what="Talk to Claude from any Mac app.")
CRISPR = ev("url:x.com/a/status/1", "Claude reportedly discovers a novel CRISPR-like enzyme", kind="community",
            what="A lab reports Claude proposed a new gene-editing enzyme.")
GUIDE = ev("url:platform.claude.com/prompting", "Official prompting guide for Claude Opus", kind="blog",
           what="Tune prompts for Claude Opus with vendor guidance.")
DEJAVU = ev("gh:x/deja-vu", "deja-vu shares one memory across coding agents", what="Reuse past fixes across coding agents.",
            sources=("https://post/1",))
CODEX_IN_CC = ev("gh:openai/codex-plugin-cc", "Use Codex from inside Claude Code", what="Call Codex from a Claude Code session.")
FILL = [ev(f"gh:f/filler-{i}", f"Filler {i} does a thing", what=f"Unrelated thing {i}.") for i in range(6)]

CKPT = ev("hf:deepseek-ai/DeepSeek-V9-Flash", "DeepSeek V9 Flash weights land on Hugging Face", kind="model",
          what="Download and run an open DeepSeek checkpoint.", maker="DeepSeek")
PROTEIN = ev("url:anthropic.com/research/binders", "Claude designs protein binders against scorpion toxins", kind="blog",
             what="Claude designed working protein binders in a lab study.", maker="Anthropic")
GRAPHIFY = ev("gh:graphify-labs/graphify", "Graphify turns codebases into queryable knowledge graphs",
              what="Query a codebase as a knowledge graph without embeddings.", sources=("https://post/1",))
TODAY = [CKPT, PROTEIN, GRAPHIFY]
LIBRARY = [UNSLOTH, OLLAMA, RIVAL, ATTICUS, CRISPR, GUIDE, DEJAVU, CODEX_IN_CC, *FILL]


def answer(picks: dict, reject: dict | None = None) -> str:
    """A lane's JSON answer: arrival -> [(candidate, relation)] to pick, and the same shape to reject."""
    out = {"arrivals": []}
    for a in TODAY:
        out["arrivals"].append({"id": a["id"],
                                "picks": [{"id": c["id"], "relation": r, "reason": f"{c['title']} {r} it."} for c, r in picks.get(a["id"], [])],
                                "reject": [{"id": c["id"], "relation": r} for c, r in (reject or {}).get(a["id"], [])]})
    return "Here you go:\n" + json.dumps(out)


AGREE = {CKPT["id"]: [(UNSLOTH, "trains"), (OLLAMA, "runs-on")], PROTEIN["id"]: [(CRISPR, "complements")],
         GRAPHIFY["id"]: [(DEJAVU, "complements")]}
REJECT = {PROTEIN["id"]: [(ATTICUS, "same-platform-only")], CKPT["id"]: [(RIVAL, "competes")]}


class PairJudgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "v1"
        self.cache = self.tmp / "cache"
        # Graphify and deja-vu were covered by the same post on an earlier day: an element-page tie.
        days = [day("2099-02-01", LIBRARY[:5]), day("2099-02-02", [*LIBRARY[5:], dict(GRAPHIFY, brief=None)])]
        for d in days:
            write_json(self.data / "days" / f"{d['date']}.json", d, compact=True)
        self.today = day("2099-02-05", TODAY)
        self.lib = L.build("2099-02-05", self.data)
        self.exclude = {e["key"] for e in TODAY}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def runners(self, **answers):
        calls = []

        def make(lane):
            def run(prompt, model, timeout):
                calls.append(lane)
                a = answers[lane]
                if isinstance(a, Exception):
                    raise a
                if callable(a):
                    return a()
                return a
            return run
        return {lane: make(lane) for lane in answers}, calls

    def pick(self, runners, **kw):
        return J.pick(self.today, TODAY, self.lib, self.exclude, check=lambda k: True, data_dir=self.data,
                      cache_dir=self.cache, runners=runners, **kw)

    def test_recall_takes_the_scorer_and_earlier_ties_and_never_today(self):
        near = L.neighbours(TODAY, self.lib, self.exclude)
        cands = J.recall(TODAY, self.lib, self.exclude, "2099-02-05", self.data, near)
        self.assertIn(DEJAVU["key"], cands[GRAPHIFY["id"]])  # tied by an earlier post, whatever its score
        for keys in cands.values():
            self.assertLessEqual(len(keys), J.RECALL)
            self.assertFalse(set(keys) & self.exclude)
            self.assertEqual(len(keys), len(set(keys)))

    def test_prompt_names_every_candidate_and_the_rules(self):
        near = L.neighbours(TODAY, self.lib, self.exclude)
        cands = J.recall(TODAY, self.lib, self.exclude, "2099-02-05", self.data, near)
        text = J.prompt("2099-02-05", TODAY, self.lib, cands)
        for a in TODAY:
            self.assertIn(a["id"], text)
            for k in cands[a["id"]]:
                self.assertIn(self.lib[k]["event"]["id"], text)
        for word in ("same-platform-only", "duplicate", "competes", "runs-on", "trains", "extends", "complements"):
            self.assertIn(word, text)
        self.assertNotIn(LONG_DASH, text)

    def test_parse_keeps_valid_picks_only(self):
        cands = {CKPT["id"]: [UNSLOTH["key"], OLLAMA["key"], RIVAL["key"], ATTICUS["key"]]}
        raw = json.dumps({"arrivals": [{"id": CKPT["id"], "picks": [
            {"id": UNSLOTH["id"], "relation": "trains", "reason": "fine-tunes it"},
            {"id": "not-a-candidate", "relation": "runs-on", "reason": "x"},
            {"id": OLLAMA["id"], "relation": "rhymes-with", "reason": "x"},
            {"id": UNSLOTH["id"], "relation": "runs-on", "reason": "again"},
            {"id": RIVAL["id"], "relation": "complements", "reason": "y"},
            {"id": OLLAMA["id"], "relation": "runs-on", "reason": "z"}],
            "reject": [{"id": ATTICUS["id"], "relation": "same-platform-only"}, {"id": RIVAL["id"], "relation": "competes"},
                       {"id": OLLAMA["id"], "relation": "nonsense"}]},
            {"id": "unknown-arrival", "picks": []}]})
        got = J.parse("prose first " + raw, cands, self.lib)
        self.assertEqual([(k, r) for k, r, _ in got[CKPT["id"]]["picks"]],
                         [(UNSLOTH["key"], "trains"), (RIVAL["key"], "complements"), (OLLAMA["key"], "runs-on")])
        self.assertEqual(got[CKPT["id"]]["reject"], {ATTICUS["key"]: "same-platform-only"})  # a pick is never also a reject
        self.assertNotIn("unknown-arrival", got)
        with self.assertRaises(ValueError):
            J.parse("no json here", cands, self.lib)

    def test_two_families_must_agree_and_rejected_candidates_are_not_filled(self):
        runners, _ = self.runners(claude=answer(AGREE, REJECT), codex=answer(AGREE, REJECT),
                                  agy=answer({CKPT["id"]: [(RIVAL, "complements")]}))
        keys, receipt, notes = self.pick(runners)
        panel = receipt["pair_judge"]
        self.assertIsNone(panel["fallback"])
        kept = {aid: [k["key"] for k in v["kept"]] for aid, v in panel["arrivals"].items()}
        self.assertEqual(kept[CKPT["id"]], [UNSLOTH["key"], OLLAMA["key"]])  # one family alone does not win (Rival)
        self.assertEqual(kept[PROTEIN["id"]], [CRISPR["key"]])
        self.assertEqual(kept[GRAPHIFY["id"]], [DEJAVU["key"]])  # the shared-memory partner the scorer lost
        chosen = {aid: v["chosen"] for aid, v in panel["arrivals"].items()}
        self.assertEqual(chosen[CKPT["id"]][:2], [UNSLOTH["key"], OLLAMA["key"]])
        self.assertEqual(len(chosen[CKPT["id"]]), L.PER_ARRIVAL)  # the gap is filled from the scorer
        self.assertNotIn(RIVAL["key"], chosen[CKPT["id"]])  # a rival: rejected by two families, never filled
        self.assertNotIn(ATTICUS["key"], chosen[PROTEIN["id"]])  # same platform only
        self.assertEqual(set(keys), {k for v in chosen.values() for k in v})
        self.assertEqual(notes[UNSLOTH["key"]], [{"with": CKPT["id"], "relation": "trains",
                                                  "reason": "Unsloth runs and trains models in a local UI trains it."}])

    def test_order_is_families_then_borda_then_the_scorer(self):
        a = answer({CKPT["id"]: [(OLLAMA, "runs-on"), (UNSLOTH, "trains")]})
        b = answer({CKPT["id"]: [(UNSLOTH, "trains"), (OLLAMA, "runs-on")]})
        c = answer({CKPT["id"]: [(UNSLOTH, "trains")]})
        runners, _ = self.runners(claude=a, codex=b, agy=c)
        _, receipt, _ = self.pick(runners)
        kept = receipt["pair_judge"]["arrivals"][CKPT["id"]]["kept"]
        self.assertEqual([k["key"] for k in kept], [UNSLOTH["key"], OLLAMA["key"]])  # three families beat two
        self.assertEqual(kept[0]["votes"], {"claude": 2, "codex": 1, "agy": 1})

    def test_a_gap_is_filled_by_one_familys_pick_before_the_scorer(self):
        near = L.neighbours(TODAY, self.lib, self.exclude)
        cands = J.recall(TODAY, self.lib, self.exclude, "2099-02-05", self.data, near)
        scorer = [k for k, _ in near[CKPT["id"]] if k not in (UNSLOTH["key"], OLLAMA["key"], RIVAL["key"])]
        late = next(self.lib[k]["event"] for k in reversed(cands[CKPT["id"]]) if k in scorer[1:])  # not the scorer's next
        solo = {CKPT["id"]: [(UNSLOTH, "trains"), (OLLAMA, "runs-on"), (late, "complements")]}
        runners, _ = self.runners(claude=answer(solo, REJECT), codex=answer(AGREE, REJECT), agy=answer({}))
        _, receipt, notes = self.pick(runners)
        got = receipt["pair_judge"]["arrivals"][CKPT["id"]]
        self.assertEqual(got["chosen"], [UNSLOTH["key"], OLLAMA["key"], late["key"]])
        self.assertEqual(got["single"], [{"key": late["key"], "votes": {"claude": 3}}])
        self.assertNotIn(late["key"], notes)  # only a winner's reason reaches the editor

    def test_fewer_than_two_lanes_fall_back_to_the_scorer(self):
        runners, _ = self.runners(claude=answer(AGREE), codex=RuntimeError("down"), agy="not json at all")
        keys, receipt, notes = self.pick(runners)
        plain, _ = L.pick(TODAY, self.lib, self.exclude, check=lambda k: True)
        self.assertEqual(keys, plain)
        self.assertEqual(notes, {})
        self.assertIn("fewer than 2", receipt["pair_judge"]["fallback"])
        self.assertEqual(receipt["pair_judge"]["lanes"]["codex"]["status"], "error")
        self.assertEqual(receipt["pair_judge"]["lanes"]["agy"]["status"], "unreadable")

    def test_a_slow_lane_is_cut_at_the_budget(self):
        def slow():
            time.sleep(3)
            return answer(AGREE)
        runners, _ = self.runners(claude=answer(AGREE, REJECT), codex=answer(AGREE, REJECT), agy=slow)
        t = time.time()
        _, receipt, _ = self.pick(runners, budget=1)
        self.assertLess(time.time() - t, 2.5)
        self.assertEqual(receipt["pair_judge"]["lanes"]["agy"]["status"], "timeout")
        self.assertIsNone(receipt["pair_judge"]["fallback"])

    def test_answers_are_cached_per_date_lane_and_prompt(self):
        runners, calls = self.runners(claude=answer(AGREE, REJECT), codex=answer(AGREE, REJECT), agy=answer(AGREE))
        first = self.pick(runners)
        self.assertEqual(sorted(calls), ["agy", "claude", "codex"])
        again = self.pick(runners)
        self.assertEqual(sorted(calls), ["agy", "claude", "codex"])  # no second call
        self.assertEqual(first[0], again[0])
        self.assertEqual(again[1]["pair_judge"]["lanes"]["claude"]["status"], "cached")
        self.assertTrue(list(self.cache.glob("2099-02-05.json")))

    def test_agy_answer_is_read_when_complete_even_if_the_process_lingers(self):
        fake = self.tmp / "agy"
        fake.write_text("#!/bin/sh\nprintf '%s' '{\"status\": \"SUCCESS\", \"response\": \"{\\\"arrivals\\\": []}\"}'\nsleep 30\n")
        fake.chmod(0o755)
        t = time.time()
        with mock.patch.object(J.shutil, "which", return_value=str(fake)):
            self.assertEqual(J._agy("prompt", "Gemini 4 Argon (High)", 20), '{"arrivals": []}')
        self.assertLess(time.time() - t, 5)  # not the 30 s the process would have lingered
        fake.write_text("#!/bin/sh\nprintf '%s' '{\"status\": \"ERROR\", \"error\": \"quota\"}'\nsleep 30\n")
        with mock.patch.object(J.shutil, "which", return_value=str(fake)), self.assertRaises(RuntimeError):
            J._agy("prompt", "Gemini 4 Argon (High)", 20)

    def test_agy_that_never_answers_is_killed_at_its_timeout(self):
        fake = self.tmp / "agy"
        pid_file = self.tmp / "agy.pid"
        fake.write_text(f"#!/bin/sh\necho $$ > '{pid_file}'\nexec sleep 60\n")
        fake.chmod(0o755)
        t = time.time()
        with mock.patch.object(J.shutil, "which", return_value=str(fake)), self.assertRaises(J.subprocess.TimeoutExpired):
            J._agy("prompt", "Gemini 4 Argon (High)", 1)
        self.assertLess(time.time() - t, 4)
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_answers_that_are_not_json_objects_are_unreadable_not_a_crash(self):
        cands = {CKPT["id"]: [UNSLOTH["key"]]}
        for text in ("[]", "null", '"text"', "no JSON here"):
            with self.assertRaises(ValueError):
                J.parse(text, cands, self.lib)
        odd = json.dumps({"arrivals": {"id": CKPT["id"]}})
        self.assertEqual(J.parse(odd, cands, self.lib), {})
        odd = json.dumps({"arrivals": [{"id": CKPT["id"], "picks": "all", "reject": {"id": UNSLOTH["id"]}},
                                       {"id": ["not", "a", "string"]}]})
        self.assertEqual(J.parse(odd, cands, self.lib), {CKPT["id"]: {"picks": [], "reject": {}}})
        for text in (None, 7, ["a"], {"arrivals": []}, '{"arrivals": ' + "[" * 100000 + "]" * 100000 + "}"):
            with self.assertRaises(ValueError):
                J.parse(text, cands, self.lib)
        for odd in ("Sure: []", ["a list"], {"a": "dict"}, None):
            shutil.rmtree(self.cache, ignore_errors=True)
            runners, _ = self.runners(claude=answer(AGREE, REJECT), codex=answer(AGREE, REJECT), agy=lambda odd=odd: odd)
            keys, receipt, _ = self.pick(runners)
            self.assertEqual(receipt["pair_judge"]["lanes"]["agy"]["status"], "unreadable")
            self.assertIsNone(receipt["pair_judge"]["fallback"])
        self.assertEqual(sorted(json.loads((self.cache / "2099-02-05.json").read_text())), ["claude", "codex"])  # not None

    def test_a_cache_of_the_wrong_shape_is_ignored_and_rewritten(self):
        path = self.cache / "2099-02-05.json"
        for bad in ([], "text", {"claude": ["x"], "codex": {"m:d": 3}}):
            write_json(path, bad)
            runners, calls = self.runners(claude=answer(AGREE, REJECT), codex=answer(AGREE, REJECT), agy=answer(AGREE))
            _, receipt, _ = self.pick(runners)
            self.assertEqual(sorted(calls), ["agy", "claude", "codex"])
            self.assertTrue(all(v["status"] == "ok" for v in receipt["pair_judge"]["lanes"].values()))
            self.assertEqual(sorted(json.loads(path.read_text())), ["agy", "claude", "codex"])

    def test_a_cache_written_meanwhile_by_another_run_is_kept(self):
        path = self.cache / "2099-02-05.json"

        def other_run_then_answer():
            write_json(path, {"claude": {"other-model:abc": "another run's answer"}})
            return answer(AGREE, REJECT)
        runners, _ = self.runners(claude=other_run_then_answer, codex=answer(AGREE, REJECT), agy=answer(AGREE))
        self.pick(runners)
        cache = json.loads(path.read_text())
        self.assertIn("other-model:abc", cache["claude"])
        self.assertEqual(len(cache["claude"]), 2)
        self.assertEqual(sorted(cache), ["agy", "claude", "codex"])

    def test_a_lane_error_is_published_by_type_only(self):
        runners, _ = self.runners(claude=answer(AGREE, REJECT), codex=answer(AGREE, REJECT),
                                  agy=RuntimeError("agy failed in /Users/someone/.config with account a@b.c"))
        with mock.patch("sys.stderr") as err:
            _, receipt, _ = self.pick(runners)
        self.assertEqual(receipt["pair_judge"]["lanes"]["agy"]["error"], "RuntimeError")
        self.assertNotIn("/Users", json.dumps(receipt))
        self.assertIn("/Users/someone", "".join(str(c) for c in err.write.call_args_list))

    def test_flag_and_lanes_come_from_the_environment(self):
        with mock.patch.dict(os.environ, {J.FLAG: ""}):
            self.assertFalse(J.enabled())
        with mock.patch.dict(os.environ, {J.FLAG: "1", J.LANES_ENV: "codex, agy"}):
            self.assertTrue(J.enabled())
            self.assertEqual(J.lanes(), ["codex", "agy"])
        with mock.patch.dict(os.environ, {J.LANES_ENV: ""}):
            self.assertEqual(J.lanes(), ["claude", "codex", "agy"])

    def test_possibilities_unchanged_with_the_flag_off_and_reasons_reach_the_editor_with_it_on(self):
        seen = []

        def editor(prompt, timeout):
            seen.append(prompt)
            return json.dumps({"possibilities": []})

        P.LANES["claude"], saved = editor, P.LANES["claude"]
        runners, _ = self.runners(claude=answer(AGREE, REJECT), codex=answer(AGREE, REJECT), agy=answer(AGREE))
        try:
            with mock.patch.dict(os.environ, {J.FLAG: ""}), mock.patch.object(J, "pick", side_effect=AssertionError):
                off = P.generate(self.today, self.data, check=lambda k: True)
            with mock.patch.dict(os.environ, {J.FLAG: "1"}), mock.patch.object(J, "RUNNERS", runners), \
                    mock.patch.object(J, "STATE", self.cache):
                on = P.generate(self.today, self.data, check=lambda k: True)
        finally:
            P.LANES["claude"] = saved
        self.assertNotIn("pair_judge", off["pool"])
        self.assertNotIn('"pairing"', seen[0])
        self.assertNotIn("panel of models", seen[0])
        self.assertIn("pair_judge", on["pool"])
        self.assertIn('"pairing"', seen[1])
        self.assertIn("panel of models", seen[1])


if __name__ == "__main__":
    unittest.main()
