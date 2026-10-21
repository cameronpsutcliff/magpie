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

    def test_never_the_capability_or_maker_under_another_form(self):
        page = ev(key="url:product.example/launch", title="Product 2.0", summary="Product 2.0 plugs into Ollama.", maker="Acme, Inc.")
        self.assertEqual(N.works_with([{"name": "Product"}, {"name": "Ollama"}], page), [{"name": "Ollama"}])  # Product, without its version
        acme = ev(summary="Acme ships a kit for Ollama.", maker="Acme, Inc.")
        self.assertEqual(N.works_with([{"name": "Acme"}, {"name": "Ollama"}], acme), [{"name": "Ollama"}])
        video = ev(key="url:youtube.com/watch?v=x", title="Track tokens with the Usage Canvas | GitHub Copilot", summary="A canvas in VS Code.")
        self.assertEqual(N.works_with([{"name": "GitHub Copilot"}, {"name": "VS Code"}], video), [{"name": "VS Code"}])
        weights = ev(key="hf:meta-llama/llama-3.1-8b", title="meta-llama/Llama-3.1-8B", summary="Llama 3.1 8B runs in Ollama and llama.cpp.")
        self.assertEqual(N.works_with([{"name": "Llama"}, {"name": "Llama 3.1"}, {"name": "meta-llama"}, {"name": "llama.cpp"}], weights),
                         [{"name": "llama.cpp"}])
        engine = ev(key="gh:deepseek-ai/deepinfer", title="deepseek-ai/DeepInfer", summary="Serves DeepSeek models at scale.", maker="DeepSeek")
        self.assertEqual(N.works_with([{"name": "deepseek-ai"}, {"name": "DeepSeek"}, {"name": "DeepSeek models"}], engine), [{"name": "DeepSeek models"}])

    def test_generic_terms_are_not_names(self):
        e = ev(summary="Routes any OpenAI-compatible endpoint, writes HTML, runs local models via Ollama behind one API, and speaks MCP with JSON Schema.")
        names = ["OpenAI-compatible endpoint", "HTML", "local models", "API", "Ollama", "MCP", "JSON Schema"]
        self.assertEqual([w["name"] for w in N.works_with([{"name": n} for n in names], e)], ["Ollama", "MCP", "JSON Schema"])

    def test_the_model_a_quant_is_of_is_the_capability(self):
        exl3 = ev(key="hf:turboderp/qwen3.8-flash-next-exl3", title="1.66 bpw EXL3 quant runs Qwen3.8-Flash-Next on one RTX 5090",
                  summary="A quant of Qwen3.8-Flash-Next for one RTX 5090.", maker="ExLlamaV3 (turboderp)")
        self.assertEqual(N.works_with([{"name": "Qwen3.8-Flash-Next"}, {"name": "RTX 5090"}], exl3), [{"name": "RTX 5090"}])
        gguf = ev(key="hf:bartowski/qwen3-8b-instruct-gguf", title="bartowski/Qwen3-8B-Instruct-GGUF",
                  summary="GGUF builds of Qwen3-8B-Instruct for llama.cpp and LM Studio.", maker="bartowski")
        got = N.works_with([{"name": "Qwen3-8B"}, {"name": "Qwen3-8B-Instruct"}, {"name": "llama.cpp"}, {"name": "LM Studio"}], gguf)
        self.assertEqual(got, [{"name": "llama.cpp"}, {"name": "LM Studio"}])

    def test_a_bare_word_in_a_list_of_apis_is_not_a_name(self):
        aws = ev(key="url:aws.amazon.com/blogs/gpt-on-bedrock", title="Access OpenAI GPT models on Amazon Bedrock", maker="AWS",
                 summary="How to invoke them with the Responses, Chat Completions, and Converse APIs on Amazon Bedrock.")
        names = ["Responses", "Chat Completions", "Converse", "Amazon Bedrock"]
        self.assertEqual([w["name"] for w in N.works_with([{"name": n} for n in names], aws)], ["Amazon Bedrock"])
        only = ev(summary="Calls the Claude API and plugs into Cursor.")
        self.assertEqual(N.works_with([{"name": "Claude"}, {"name": "Cursor"}], only), [{"name": "Cursor"}])  # only ever "Claude API"
        also = ev(summary="Calls the Claude API and plugs into Claude Code.")  # "Claude" also stands on its own here
        self.assertEqual(N.works_with([{"name": "Claude"}, {"name": "Claude Code"}], also), [{"name": "Claude"}, {"name": "Claude Code"}])

    def test_what_it_came_from_is_not_what_it_works_with(self):
        clef = ev(key="url:x.com/ollama/status/1", title="Cloudflare's Clef 27B decision model runs locally in Ollama", maker="Cloudflare",
                  summary="Clef, a 27B multimodal model fine-tuned from Qwen3.8-27B, runs locally in Ollama.")
        self.assertEqual(N.works_with([{"name": "Qwen3.8-27B"}, {"name": "Ollama"}], clef), [{"name": "Ollama"}])
        quant = ev(key="url:reddit.com/r/localllama/1", title="A tiny quant for one card", summary="A 2-bit quant of GLM-5 that runs in llama.cpp.")
        self.assertEqual(N.works_with([{"name": "GLM-5"}, {"name": "llama.cpp"}], quant), [{"name": "llama.cpp"}])

    def test_lineage_and_api_lists_in_other_words(self):
        for text in ("It was fine-tuned from the base model Qwen3.8-27B.", "It was fine-tuned from Alibaba's Qwen3.8-27B.",
                     "Weights quantized from an adapted Qwen3.8-27B.", "A model fine-tuned from the open base known as Qwen3.8-27B."):
            self.assertEqual(N.works_with([{"name": "Qwen3.8-27B"}], ev(summary=text)), [], text)
        lower = ev(summary="Invoke via the responses, chat completions, and converse apis on Bedrock.")
        self.assertEqual(N.works_with([{"name": "responses"}, {"name": "converse"}, {"name": "Bedrock"}], lower), [{"name": "Bedrock"}])

    def test_lineage_and_api_lists_never_reach_past_their_clause(self):
        cases = [("Ollama", "Fine-tuned from Qwen and served in Ollama."), ("Cursor", "Distilled from DeepSeek R1 for use in Cursor."),
                 ("GitHub", "The PR merged on GitHub today."), ("Ollama", "Works with Ollama and other APIs."),
                 ("Bedrock", "Use Bedrock to call models, or the Converse API.")]
        for name, text in cases:
            self.assertEqual(N.works_with([{"name": name}], ev(summary=text)), [{"name": name}], text)

    def test_the_rules_leave_real_names_alone(self):
        e = ev(key="url:example.com/tools", title="MLX ONNX converter", maker="Example",
               summary="An MLX ONNX converter with a Backward Compatible Layer, tested with open webui and Claude Code.")
        names = ["MLX", "Backward Compatible Layer", "open webui", "Claude Code"]
        self.assertEqual([w["name"] for w in N.works_with([{"name": n} for n in names], e)], names)

    def test_what_it_plugs_into_may_lead_its_title(self):
        plugin = ev(key="url:zed.dev/blog/plugin", title="Claude Code plugin for Zed", summary="A plugin.", maker="Zed Industries")
        self.assertEqual(N.works_with([{"name": "Claude Code"}, {"name": "Zed"}], plugin), [{"name": "Claude Code"}])  # Zed leads its maker

    def test_an_item_the_redaction_gate_would_stop_is_dropped(self):
        got = N.works_with([{"name": "Ollama", "how": "loads weights from /Users/someone/models"}, {"name": "vLLM", "how": "serves it"}], ev())
        self.assertEqual(got, [{"name": "vLLM", "how": "serves it"}])

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

    def test_a_cached_brief_is_checked_again_against_the_rebuilt_event(self):
        cached = {"headline": "Kit", "works_with": [{"name": "Ollama"}, {"name": "vLLM"}]}
        N.keep_works_with(cached, ev(summary="Now only Ollama."))  # the text changed between the editor run and the reuse
        self.assertEqual(cached["works_with"], [{"name": "Ollama"}])
        N.keep_works_with(cached, ev(summary="Neither any more."))
        self.assertNotIn("works_with", cached)
        broken = {"headline": "Kit", "works_with": [{"name": "Ollama"}]}
        with mock.patch.object(N, "works_with", side_effect=RuntimeError("boom")):
            N.keep_works_with(broken, ev())
        self.assertEqual(broken, {"headline": "Kit"})

    def test_the_reuse_path_checks_cached_names(self):
        main = (ROOT / "magpie/__main__.py").read_text()
        reuse = main[main.index("if a.reuse_editor"):main.index("elif not a.no_llm")]
        self.assertIn('keep_works_with(e["brief"], e)', reuse)

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
