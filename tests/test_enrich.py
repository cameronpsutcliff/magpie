import json
import os
import unittest
from unittest import mock

from magpie import enrich as N
from magpie.util import ROOT


def ev(**kw):
    e = {"id": "e1", "key": "gh:acme/kit", "kind": "repo", "title": "acme/kit", "signal": 60.0, "evidence": "reported",
         "summary": "A local agent kit that runs models through Ollama or vLLM and exposes an MCP server to Claude Code.",
         "vendors": ["Acme"], "maker": "Acme", "topics": ["agents"], "metrics": {"stars_day": 120},
         "url": "https://github.com/acme/kit", "created_at": None, "license": None,
         "sources": [{"name": "Hacker News", "url": "https://news.ycombinator.com/item?id=1", "title": "Show HN: Kit, an agent kit for LangGraph"}],
         "mentions": [{"name": "AI News", "url": "https://news.example/7", "title": "Issue 7"}]}
    e.update(kw)
    return e


class WorksWithTests(unittest.TestCase):
    def test_keeps_only_names_the_events_own_words_spell(self):
        got = N.works_with([{"name": "ollama", "how": "serves the models"}, {"name": "LangGraph"}, {"name": "LlamaIndex", "how": "plugs in"},
                            {"name": "Claude Code", "how": "connects over MCP"}], ev())
        self.assertEqual(got, [{"name": "Ollama", "how": "serves the models"}, {"name": "LangGraph"},
                               {"name": "Claude Code", "how": "connects over MCP"}])  # LlamaIndex is nowhere in its text

    def test_never_the_capability_its_maker_or_who_reported_it(self):
        e = ev(summary="Kit from Acme, covered by Hacker News and AI News, runs on Ollama.")
        got = N.works_with([{"name": "Kit"}, {"name": "Acme"}, {"name": "Hacker News"}, {"name": "AI News"}, {"name": "Ollama"}], e)
        self.assertEqual(got, [{"name": "Ollama"}])

    def test_a_whole_name_not_part_of_a_word(self):
        e = ev(summary="Works with vLLMx and Ollama.")
        self.assertEqual(N.works_with([{"name": "vLLM"}, {"name": "Olla"}, {"name": "Ollama"}], e), [{"name": "Ollama"}])

    def test_drops_an_item_whose_numbers_the_sources_do_not_state(self):
        e = ev(summary="Runs Llama 3.1 through Ollama on 2 GPUs.")
        got = N.works_with([{"name": "Llama 3.1", "how": "runs on 8 GPUs"}, {"name": "Llama 3.2"}, {"name": "Ollama", "how": "serves it on 2 GPUs"}], e)
        self.assertEqual(got, [{"name": "Ollama", "how": "serves it on 2 GPUs"}])

    def test_grounds_in_context_and_source_titles_not_urls_or_mentions(self):
        e = ev(summary="An agent kit.", _context=["In the video he wires it to Cursor."],
               url="https://github.com/acme/kit-for-zed", mentions=[{"name": "AI News", "url": "https://news.example/7", "title": "Issue 7: Windsurf"}])
        got = N.works_with([{"name": "Cursor"}, {"name": "LangGraph"}, {"name": "zed"}, {"name": "Windsurf"}], e)
        self.assertEqual(got, [{"name": "Cursor"}, {"name": "LangGraph"}])

    def test_dedupes_caps_and_drops_an_overlong_clause_not_the_name(self):
        e = ev(summary="Ollama, vLLM, LangGraph, Claude Code, Cursor, Zed and Windsurf.")
        names = ["Ollama", "ollama", "vLLM", "LangGraph", "Claude Code", "Cursor", "Zed", "Windsurf"]
        got = N.works_with([{"name": n, "how": "word " * 20 if n == "Ollama" else "ok"} for n in names], e)
        self.assertEqual([w["name"] for w in got], ["Ollama", "vLLM", "LangGraph", "Claude Code", "Cursor"])
        self.assertNotIn("how", got[0])

    def test_a_malformed_answer_is_empty_never_an_error(self):
        for raw in (None, "Ollama", 5, {"name": "Ollama"}, [None, 5, "Ollama", {"name": None}, {"name": ["Ollama"]}, {"how": "x"},
                                                             {"name": "Ollama", "how": 7}, {"name": "--"}, {"name": "2099"}]):
            got = N.works_with(raw, ev())
            self.assertTrue(got == [] or got == [{"name": "Ollama"}], (raw, got))
        self.assertEqual(N.works_with([{"name": "Ollama"}], {"key": "gh:a/b"}), [])  # an event with no text grounds nothing


class EditorTests(unittest.TestCase):
    def run_editor(self, events, answer):
        reply = json.dumps({"edition": {"headline": "Kits", "dek": "A day.", "quiet": True}, "events": answer})
        with mock.patch.dict(N.LANES, {"fake": lambda prompt, timeout: reply}), mock.patch.dict(os.environ, {"MAGPIE_LANES": "fake"}):
            return N.enrich(events, "2099-01-01")

    def test_the_brief_carries_only_checked_names(self):
        events = [ev()]
        _, receipt = self.run_editor(events, [{"id": "e1", "headline": "Kit runs agents locally", "vendor": "Acme",
                                               "works_with": [{"name": "Ollama", "how": "serves models"}, {"name": "LlamaIndex"}]}])
        self.assertEqual(receipt["enriched"], 1)
        self.assertEqual(events[0]["brief"]["works_with"], [{"name": "Ollama", "how": "serves models"}])

    def test_a_bad_works_with_never_costs_the_brief(self):
        events = [ev(), ev(id="e2", key="gh:acme/two", title="acme/two")]
        with mock.patch.object(N, "works_with", side_effect=RuntimeError("boom")):
            _, receipt = self.run_editor(events, [{"id": "e1", "headline": "One", "works_with": [{"name": "Ollama"}]},
                                                  {"id": "e2", "headline": "Two", "works_with": "not a list"}])
        self.assertEqual(receipt["enriched"], 2)
        self.assertEqual([e["brief"]["headline"] for e in events], ["One", "Two"])
        self.assertTrue(all("works_with" not in e["brief"] for e in events))  # none survive: no field

    def test_the_vendor_becomes_the_maker_before_names_are_checked(self):
        events = [ev(maker="Open source", summary="A kit from Ollama that drives Claude Code.")]
        self.run_editor(events, [{"id": "e1", "headline": "Kit", "vendor": "Ollama", "works_with": [{"name": "Ollama"}, {"name": "Claude Code"}]}])
        self.assertEqual(events[0]["brief"]["works_with"], [{"name": "Claude Code"}])

    def test_the_brief_validates(self):
        try:
            from jsonschema import Draft202012Validator
            from referencing import Registry, Resource
        except ImportError:
            self.skipTest("jsonschema is not installed")
        events = [ev()]
        self.run_editor(events, [{"id": "e1", "headline": "Kit", "works_with": [{"name": "Ollama", "how": "serves models"}, {"name": "vLLM"}]}])
        day = json.loads((ROOT / "data/v1/schema/day.schema.json").read_text())
        reg = Registry().with_resources([(day["$id"], Resource.from_contents(day))])
        brief = Draft202012Validator({"$ref": day["$id"] + "#/$defs/event/properties/brief"}, registry=reg)
        self.assertEqual([x.message for x in brief.iter_errors(events[0]["brief"])], [])
        self.assertTrue(list(brief.iter_errors({"works_with": [{"name": "Ollama", "url": "https://ollama.com"}]})))  # names and clauses only


if __name__ == "__main__":
    unittest.main()
