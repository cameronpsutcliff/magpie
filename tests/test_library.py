import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from magpie import library as L
from magpie import possibilities as P
from magpie.util import short_hash, write_json


def ev(key, headline, *, what="", topics=("agents",), maker="Open source", works_with=(), metrics=None, signal=50.0, brief=True):
    e = {"id": short_hash(key, 12), "key": key, "kind": "repo", "title": key.split(":", 1)[1], "summary": what,
         "url": f"https://github.com/{key.split(':', 1)[1]}", "maker": maker, "topics": list(topics), "evidence": "reported",
         "signal": signal, "metrics": metrics or {}}
    if brief:
        e["brief"] = {"headline": headline, "newly_possible": what, "why": "", "limits": "",
                      "works_with": [{"name": n, "how": "plugs in"} for n in works_with]}
    return e


def day(date, events, top=()):
    return {"schema": "magpie.day/1", "date": date, "events": events, "top": list(top)}


# The god's-eye case: a viewer people liked last week, and today a single repo that makes it far better.
EYE = ev("gh:bilal/gods-eye-view", "God's-eye view maps live satellite imagery in the browser",
         what="Browse live satellite and drone imagery as one zoomable globe.", topics=("multimodal",), signal=80.0,
         metrics={"stars_total": 900})
LATER_EYE = dict(EYE, metrics={"stars_total": 4100})
OLD = ev("gh:old/memory-kit", "Memory kit stores agent notes", what="Agents keep notes across sessions.")
TILES = ev("gh:new/tile-fuse", "Tile Fuse stitches satellite imagery into live 3D tiles",
           what="Stream satellite imagery as 3D tiles to any globe viewer.", topics=("multimodal",), works_with=("gods-eye-view",))


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "v1"
        days = [day("2099-01-01", [EYE, OLD], top=[EYE["id"]]), day("2099-01-03", [LATER_EYE]),
                day("2099-01-05", [TILES])]
        for d in days:
            write_json(self.data / "days" / f"{d['date']}.json", d, compact=True)
        self.today = days[-1]

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def test_build_keeps_history_before_the_day(self):
        lib = L.build("2099-01-05", self.data)
        self.assertEqual(set(lib), {EYE["key"], OLD["key"]})  # today's arrival is not yet in the library
        eye = lib[EYE["key"]]
        self.assertEqual((eye["first_seen"], eye["last_seen"], eye["date"]), ("2099-01-01", "2099-01-03", "2099-01-03"))
        self.assertEqual((eye["days_seen"], eye["featured"], eye["stars_gained"]), (2, 1, 3200))
        self.assertGreater(L.adoption(eye), L.adoption(lib[OLD["key"]]))
        self.assertEqual(L.adoption(None), 0.0)

    def test_neighbours_relate_and_skip_the_same_release(self):
        lib = L.build("2099-01-05", self.data)
        near = L.neighbours([TILES], lib, set())[TILES["id"]]
        self.assertEqual(near[0][0], EYE["key"])  # shared words, topic, and Tile Fuse names it as an integration
        self.assertNotIn(OLD["key"], [k for k, _ in near])  # unrelated
        launch = ev("url:new.example/blog/tile-fuse", "Tile Fuse stitches satellite imagery into live 3D tiles")
        self.assertTrue(L.same(TILES, launch))
        self.assertFalse(L.same(TILES, EYE))

    def test_works_with_counts_only_earlier_editions(self):
        post = {"name": "AI News", "url": "https://news.example/7", "title": "Issue 7"}
        a, b, c = (dict(ev(k, k), mentions=[post] if k != "gh:c/three" else []) for k in ("gh:a/one", "gh:b/two", "gh:c/three"))
        later = day("2099-02-02", [c])
        later["possibilities"] = {"items": [{"title": "One with three", "ingredients": [a["id"], c["id"]]}]}
        data = self.tmp / "w"
        for d in (day("2099-02-01", [a, b]), later):
            write_json(data / "days" / f"{d['date']}.json", d, compact=True)
        self.assertEqual(L.build("2099-02-02", data)["gh:a/one"]["works_with"], 1)  # covered with b; the idea came later
        self.assertEqual(L.build("2099-02-03", data)["gh:a/one"]["works_with"], 2)

    def test_a_lone_arrival_keeps_its_best_weak_partner(self):
        filler = " ".join(f"word{i}" for i in range(40))
        lone = ev("gh:new/ledgerline", "Ledgerline reconciles invoices", what=f"Ledgerline reconciles invoices. {filler}", topics=("finance",))
        weak = ev("gh:old/quill", "Quill drafts letters", what=f"Quill drafts letters and invoices. {' '.join(f'other{i}' for i in range(40))}",
                  topics=("writing",))
        data = self.tmp / "q"
        for d in (day("2099-03-01", [weak]), day("2099-03-02", [lone])):
            write_json(data / "days" / f"{d['date']}.json", d, compact=True)
        lib = L.build("2099-03-02", data)
        near = L.neighbours([lone], lib, set())[lone["id"]]
        self.assertEqual([k for k, _ in near], ["gh:old/quill"])
        self.assertLess(near[0][1], L.MIN_SCORE)  # below the floor, kept because it is the arrival's only partner
        self.assertEqual(L.pick([lone], lib, set(), check=lambda k: True)[0], ["gh:old/quill"])

    def test_pick_uses_live_capabilities_only(self):
        lib = L.build("2099-01-05", self.data)
        keys, receipt = L.pick([TILES], lib, set(), check=lambda k: False)
        self.assertEqual((keys, receipt["not_live"]), ([], [EYE["key"]]))
        keys, receipt = L.pick([TILES], lib, set(), check=lambda k: None)
        self.assertEqual((keys, receipt["unchecked"]), ([], [EYE["key"]]))  # a check that cannot run does not let it in
        keys, _ = L.pick([TILES], lib, set(), check=lambda k: True)
        self.assertEqual(keys, [EYE["key"]])

    def test_a_quiet_day_with_one_repo_still_combines_with_the_library(self):
        seen = {}

        def lane(prompt, timeout):
            seen["prompt"] = prompt
            return json.dumps({"possibilities": [
                {"title": "Live 3D globe of any region", "connection": "Tile Fuse feeds the viewer.",
                 "ingredients": [TILES["id"], EYE["id"]], "chain": ["a", "b", "c"], "effort": "a week",
                 "scores": {"novelty": 4, "feasibility": 4, "value": 4, "evidence": 3}},
                {"title": "Made-up pairing", "ingredients": [EYE["id"], OLD["id"]], "chain": []}]})

        P.LANES["claude"], saved = lane, P.LANES["claude"]
        try:
            out = P.generate(self.today, self.data, check=lambda k: True)
        finally:
            P.LANES["claude"] = saved
        self.assertIn('"first_seen": "2099-01-01"', seen["prompt"])
        self.assertEqual(out["pool"]["today"], 1)
        self.assertEqual(out["pool"]["library"], 1)
        [item] = out["items"]  # the pairing without a today ingredient was dropped
        self.assertEqual(item["mix"], "today_x_library")
        self.assertEqual(item["amplification"]["lifts"], EYE["id"])
        self.assertGreater(item["amplification"]["factor"], 1)
        self.assertEqual(item["amplification"]["signals"]["first_seen"], "2099-01-01")
        refs = {r["id"]: r for r in item["ingredient_refs"]}
        self.assertEqual((refs[TILES["id"]]["today"], refs[TILES["id"]]["first_seen"]), (True, "2099-01-05"))
        self.assertEqual((refs[EYE["id"]]["today"], refs[EYE["id"]]["date"], refs[EYE["id"]]["first_seen"]),
                         (False, "2099-01-03", "2099-01-01"))

    def test_amplification_lifts_the_rank_and_both_mixes_survive(self):
        base = {"effort": "a week", "scores": {"value": 4, "feasibility": 4, "evidence": 3, "novelty": 3}}
        flat = dict(base, mix="today_x_today", amplification={"factor": 1.0})
        lifted = dict(base, mix="today_x_library", amplification={"factor": 1.5})
        self.assertIs(P.rank([flat, lifted])[0], lifted)
        strong = [dict(base, mix="today_x_library", amplification={"factor": 1.5}) for _ in range(5)]
        weak = dict(base, mix="today_x_today", amplification={"factor": 1.0})
        kept = P.choose(P.rank(strong + [weak]), 5)
        self.assertEqual(len(kept), 5)
        self.assertEqual({p["mix"] for p in kept}, {"today_x_today", "today_x_library"})
        only = P.choose(P.rank([dict(p) for p in strong]), 3)
        self.assertEqual(len(only), 3)  # one mix proposed: rank alone decides


def entry(e):
    return {"event": e, "first_seen": "2099-01-01", "days_seen": 1, "peak": 50.0, "stars_gained": 0, "works_with": 0}


class StatedIntegrationTests(unittest.TestCase):
    """Named integrations read from text (MAGPIE_TEXT_NAMED): the name heuristics, then the bonus through neighbours."""

    def test_tok_drops_versions_but_not_names(self):
        self.assertEqual([L._tok(w) for w in ("Qwen3.8", "GPT-6", "deepseek-v4.1-flash", "Code2Skill", "v4.1", "vLLM")],
                         ["qwen", "gpt", "deepseek", "code2skill", "", "vllm"])

    def test_shaped_names(self):
        self.assertTrue(all(L._shaped(w) for w in ("DeepInfer", "vLLM", "Qwen3", "MCP")))
        self.assertFalse(any(L._shaped(w) for w in ("Claude", "open-sources", "on-policy", "KV")))

    def test_phrases_take_a_family_prefix_only_from_a_known_repo(self):
        self.assertEqual(L._phrases("sglang-omni", {"sglang"}), {("sglang-omni",), ("sglang",)})
        self.assertEqual(L._phrases("sglang-omni", set()), {("sglang-omni",)})
        self.assertEqual(L._phrases("Claude Code", set()), {("claude", "code")})
        self.assertEqual(L._phrases("Flash", set()), set())  # a tier word is not a name

    def test_subject_is_the_product_not_the_maker_or_a_mention(self):
        s = L._subject
        self.assertEqual(s("DeepSeek opens its DeepInfer inference framework", {"deepseek"}, set()), "DeepInfer")
        self.assertEqual(s("Claude Code falls back to AGENTS.md", set(), set()), "Claude Code")  # a run of capitals
        self.assertEqual(s("Free Claude Code adds browser harness sessions", set(), set()), "")  # opens with a plain word
        self.assertEqual(s("Marketing skills pack for Claude Code agents", set(), set()), "")
        self.assertEqual(s("Claude designs protein binders", set(), {"claude"}), "Claude")
        self.assertEqual(s("Claude designs protein binders", set(), set()), "")  # not a known proper noun here

    def test_products_skip_posts_about_something(self):
        post = dict(ev("url:x.com/a/status/1", "Claude reportedly discovers a novel enzyme"), kind="community")
        repo = ev("gh:sgl-project/sglang-omni", "SGLang-Omni serves audio and multimodal models")
        self.assertEqual(L.products(post, {"claude"}, set()), set())
        self.assertEqual(L.products(repo, set(), {"sglang"}), {("sglang-omni",), ("sglang",)})

    def test_states_whole_names_written_as_names(self):
        def win(text):
            return L._windows({"key": "gh:a/b", "title": "", "brief": {"newly_possible": text}})
        self.assertTrue(L._states(win("Optimised models deploy to TensorRT-LLM or vLLM."), {("vllm",)}))
        self.assertFalse(L._states(win("It works with Claude Code."), {("claude",)}))  # Claude is not Claude Code
        self.assertTrue(L._states(win("It works with Claude Code."), {("claude", "code")}))
        self.assertFalse(L._states(win("It runs on hindsight alone."), {("hindsight",)}))  # a plain word, not the repo
        self.assertTrue(L._states(win("It configures the agents you use: Claude Code, Cursor, OpenCode, Codex."),
                                  {("opencode",)}))  # a list item, not a two-word name
        self.assertFalse(L._states(win("An open alternative to Muse."), {("muse",)}))  # not an integration
        works = {"key": "gh:a/b", "title": "", "brief": {"works_with": [{"name": "SGLang", "how": "serves it"}]}}
        self.assertTrue(L._states(L._windows(works), {("sglang",)}))

    def test_the_bonus_comes_once_and_only_when_switched_on(self):
        vllm = ev("gh:vllm-project/vllm", "vLLM serves models fast", what="High-throughput model serving.", topics=("inference",))
        fill = [ev(f"gh:f/filler-{i}", f"Filler {i} does a thing", what=f"Unrelated thing {i}.") for i in range(7)]
        lib = {e["key"]: entry(e) for e in [vllm, *fill]}  # big enough that one mention of vLLM is not common
        opt = ev("gh:nvidia/model-optimizer", "Model-Optimizer compresses models", topics=("efficiency",),
                 what="Quantise and prune a model, then deploy to TensorRT-LLM or vLLM.")

        def score(arrival, **kw):
            return dict(L.neighbours([arrival], lib, set(), **kw)[arrival["id"]]).get(vllm["key"], 0.0)

        off, on = score(opt, text_named=False), score(opt, text_named=True)
        self.assertEqual(round(on - off, 3), 0.5)
        with mock.patch.dict(os.environ, {L.TEXT_NAMED: "1"}):
            self.assertEqual(score(opt), on)
        with mock.patch.dict(os.environ, {L.TEXT_NAMED: ""}):
            self.assertEqual(score(opt), off)
        listed = dict(opt, brief=dict(opt["brief"], works_with=[{"name": "vllm", "how": "deploys to it"}]))
        self.assertEqual(score(listed, text_named=True), score(listed, text_named=False))  # main's rule fired; no second 0.5


if __name__ == "__main__":
    unittest.main()
