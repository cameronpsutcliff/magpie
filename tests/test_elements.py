import json
import shutil
import tempfile
import unittest
from pathlib import Path

from magpie import elements as E
from magpie.util import ROOT, short_hash, write_json


def ev(key, title, *, maker="Open source", sources=(), mentions=(), brief=None, kind="repo", signal=50.0):
    url = f"https://github.com/{key.split(':', 1)[1]}" if key.startswith("gh:") else f"https://{key.split(':', 1)[1]}"
    e = {"id": short_hash(key, 12), "key": key, "kind": kind, "title": title, "summary": f"{title} summary", "url": url,
         "maker": maker, "vendors": [maker], "topics": ["agents"], "evidence": "reported", "signal": signal, "impact": 2,
         "metrics": {"stars_total": 100}, "sources": [{"name": "GitHub trending", "kind": "repo", "url": url, "title": title}, *sources],
         "mentions": list(mentions)}
    if brief:
        e["brief"] = brief
    return e


def day(date, events, top=(), possibilities=None):
    d = {"schema": "magpie.day/1", "date": date, "events": events, "top": list(top)}
    if possibilities:
        d["possibilities"] = {"items": possibilities}
    return d


NEWS = {"name": "AI News", "url": "https://news.example/issue-7", "title": "Issue 7"}
ACTION = {"key": "policy:fr:2099-1", "id": "p1", "headline": "Agency bars Acme models", "title": "Order on Acme", "harder": "Agencies cannot buy Acme models.",
          "who": "Acme and its resellers", "status": "final", "jurisdiction": "US", "issuer": "An agency", "doc_url": "https://rules.example/2099-1",
          "url": "https://rules.example/2099-1", "sources": [], "first_seen": "2099-01-02", "last_seen": "2099-01-02"}


class ElementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "v1"
        a, b = ev("gh:acme/agent", "acme/Agent", maker="Acme"), ev("gh:beta/kit", "beta/kit", mentions=[NEWS])
        c = ev("gh:gamma/tool", "gamma/tool", mentions=[NEWS], brief={"headline": "Gamma tool ships", "newly_possible": "Run gamma.",
                                                                        "impact": 4, "ripples": [{"topic": "agents", "effect": "Agents get gamma"}],
                                                                        "works_with": [{"name": "Ollama", "how": "serves its models"}]})
        lone = ev("gh:solo/once", "solo/once")
        self.days = {
            "2099-01-01": day("2099-01-01", [a, b, lone]),
            "2099-01-02": day("2099-01-02", [b, c, ev("gh:acme/other", "acme/other", maker="Acme")], top=[c["id"]], possibilities=[{
                "title": "Agent plus gamma", "connection": "They fit.", "effort": "a week", "ingredients": [a["id"], c["id"]],
                "ingredient_refs": [{"id": a["id"], "title": "Acme agent", "date": "2099-01-01"}, {"id": c["id"], "title": "Gamma tool ships", "date": "2099-01-02"}]}]),
        }
        for d, payload in self.days.items():
            write_json(self.data / "days" / f"{d}.json", payload, compact=True)
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": [ACTION]})

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def pages(self, data=None):
        d = (data or self.data) / "elements"
        return {p.name: p.read_text() for p in sorted(d.glob("*.json"))}

    def page(self, slug):
        return json.loads((self.data / "elements" / f"{slug}.json").read_text())

    def test_slugs_are_a_pure_function_of_the_key(self):
        self.assertEqual(E.slug_for("gh:owner/repo.js"), "gh-owner--repo.js")
        self.assertNotEqual(E.slug_for("gh:a/b-c"), E.slug_for("gh:a-b/c"))  # gh-a--b-c and gh-a-b--c
        self.assertEqual(E.slug_for("arxiv:2510.12345"), "arxiv-2510.12345")
        self.assertEqual(E.slug_for("policy:fr:2026-21447"), "policy-fr-2026-21447")
        long = E.slug_for("url:example.com/" + "a-very-long-path/" * 8)
        self.assertLessEqual(len(long), 72)
        self.assertRegex(long, r"^url-example-com-.*-[0-9a-f]{8}$")
        self.assertNotEqual(E.slug_for("url:a.com/x-y"), E.slug_for("url:a.com/x/y"))

    def test_which_elements_get_pages(self):
        E.build(self.data)
        index = json.loads((self.data / "elements" / "index.json").read_text())
        keys = {e["key"] for e in index["elements"]}
        # beta: two days. acme/agent and gamma: a new possibility. acme/other: an action on record names Acme.
        self.assertEqual(keys, {"gh:beta/kit", "gh:acme/agent", "gh:gamma/tool", "gh:acme/other"})
        self.assertNotIn("gh:solo/once", keys)  # one day, no links

    def test_a_library_ingredient_from_before_the_start_gets_a_page(self):
        # acme/agent appeared only on 2099-01-01; pages start on 2099-01-02, where an idea combined it with gamma.
        E.build(self.data, since="2099-01-02")
        agent = self.page("gh-acme--agent")
        self.assertEqual(agent["first_seen"], "2099-01-01")
        self.assertEqual([p["title"] for p in agent["possibilities"]], ["Agent plus gamma"])
        self.assertEqual(agent["works_with"][0]["key"], "gh:gamma/tool")
        self.assertEqual(self.page("gh-gamma--tool")["works_with"][0]["key"], "gh:acme/agent")
        index = json.loads((self.data / "elements" / "index.json").read_text())
        self.assertNotIn("gh:solo/once", {e["key"] for e in index["elements"]})  # seen before the start, never combined

    def test_page_contents(self):
        E.build(self.data)
        agent = self.page("gh-acme--agent")
        self.assertEqual(agent["link"], {"url": "https://github.com/acme/agent", "label": "GitHub"})
        self.assertEqual(agent["name"], "acme/agent")
        self.assertEqual([p["title"] for p in agent["possibilities"]], ["Agent plus gamma"])
        self.assertEqual(agent["possibilities"][0]["date"], "2099-01-02")  # the day the idea ran, not the ingredient's day
        self.assertEqual(agent["works_with"][0]["key"], "gh:gamma/tool")
        self.assertEqual([c["match"] for c in agent["constraints"]], ["maker"])
        gamma = self.page("gh-gamma--tool")
        self.assertEqual(gamma["days_featured"], 1)
        self.assertEqual(gamma["best_rank"], 1)
        self.assertEqual(gamma["knock_on"][0]["effects"][0]["effect"], "Agents get gamma")
        beta = self.page("gh-beta--kit")
        self.assertEqual(beta["days_seen"], 2)
        self.assertEqual(beta["first_seen"], "2099-01-01")
        tie = next(w for w in beta["works_with"] if w["key"] == "gh:gamma/tool")
        self.assertEqual(tie["together"], 1)  # the same newsletter covered both on 2099-01-02
        self.assertIn("AI News: Issue 7", tie["via"])
        self.assertEqual([f["url"] for f in beta["find"]], [NEWS["url"]])  # the repo link itself is the canonical link

    def test_named_integrations_roll_up_beside_the_computed_ones(self):
        named = {"2099-01-01": [{"name": "Ollama", "how": "serves its models"}, {"name": "vLLM"}, "junk", {"how": "no name"}],
                 "2099-01-02": [{"name": "ollama", "how": "runs it locally"}, {"name": "LangChain"}, {"name": "Ollama"}]}
        for d, payload in self.days.items():
            payload["events"] = [dict(e, brief={"headline": "Beta kit", "works_with": named[d]}) if e["key"] == "gh:beta/kit" else e
                                 for e in payload["events"]]
            write_json(self.data / "days" / f"{d}.json", payload, compact=True)
        E.build(self.data)
        beta = self.page("gh-beta--kit")
        self.assertEqual(beta["appearances"][0]["named_integrations"],
                         [{"name": "ollama", "how": "runs it locally"}, {"name": "LangChain"}, {"name": "Ollama"}])
        self.assertEqual(beta["named_integrations"], [
            {"name": "ollama", "days": 2, "first_seen": "2099-01-01", "last_seen": "2099-01-02", "how": ["runs it locally", "serves its models"]},
            {"name": "LangChain", "days": 1, "first_seen": "2099-01-02", "last_seen": "2099-01-02", "how": []},
            {"name": "vLLM", "days": 1, "first_seen": "2099-01-01", "last_seen": "2099-01-01", "how": []}])
        self.assertEqual(beta["works_with"][0]["key"], "gh:gamma/tool")  # the computed list is untouched
        self.assertNotIn("named_integrations", self.page("gh-acme--agent"))  # none named: no field, as before

    def test_a_build_says_exactly_what_the_editions_say(self):
        E.build(self.data)
        built = self.pages()
        E.build(self.data)
        self.assertEqual(self.pages(), built)  # nothing accumulates
        d2 = self.days["2099-01-02"]
        d2["events"] = [e for e in d2["events"] if e["key"] != "gh:beta/kit"]  # a corrected edition
        write_json(self.data / "days" / "2099-01-02.json", d2, compact=True)
        E.build(self.data)
        self.assertFalse((self.data / "elements" / "gh-beta--kit.json").exists())  # back to one day: no page, no stale file
        index = json.loads((self.data / "elements" / "index.json").read_text())
        self.assertNotIn("gh:beta/kit", {e["key"] for e in index["elements"]})

    def test_pages_follow_the_register(self):
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": []})
        E.build(self.data)
        self.assertFalse((self.data / "elements" / "gh-acme--other.json").exists())  # one day, no action yet
        cited = dict(ACTION, sources=[{"name": "AI News", "url": NEWS["url"], "title": "Issue 7"}])
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": [cited]})
        E.build(self.data)
        self.assertEqual([c["match"] for c in self.page("gh-acme--other")["constraints"]], ["maker"])
        self.assertEqual([c["match"] for c in self.page("gh-beta--kit")["constraints"]], ["reported"])
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": []})
        E.build(self.data)  # removed again
        self.assertFalse((self.data / "elements" / "gh-acme--other.json").exists())
        self.assertEqual(self.page("gh-beta--kit")["constraints"], [])

    def test_an_action_page_shows_its_record_and_whom_it_names(self):
        d2 = self.days["2099-01-02"]
        d2["events"].append(dict(ev(ACTION["key"], ACTION["title"], maker="An agency", kind="constraint"), evidence="official",
                                 constraint={"status": "final", "jurisdiction": "US"},
                                 brief={"headline": ACTION["headline"], "harder": ACTION["harder"]}))
        write_json(self.data / "days" / "2099-01-02.json", d2, compact=True)
        E.build(self.data)
        page = self.page("policy-fr-2099-1")
        self.assertEqual(page["name"], "Agency bars Acme models")
        self.assertEqual(page["link"]["url"], ACTION["doc_url"])
        self.assertEqual({n["key"] for n in page["names"]}, {"gh:acme/agent", "gh:acme/other"})
        self.assertEqual(page["constraint"]["who"], ACTION["who"])
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": []})
        E.build(self.data)
        self.assertNotIn("constraint", self.page("policy-fr-2099-1"))  # gone from the register: none of its old details

    def test_pages_start_on_a_recent_edition_and_never_reach_into_the_archive(self):
        import datetime as dt
        dates = [(dt.date(2099, 4, 1) + dt.timedelta(days=i)).isoformat() for i in range(10)]
        for i, d in enumerate(dates):
            write_json(self.data / "days" / f"{d}.json", day(d, [ev("gh:old/one", "old/one")] if i < 3 else []), compact=True)
        self.assertEqual(E.build(self.data)["since"], dates[-7])  # a first build starts seven editions back
        self.assertFalse((self.data / "elements" / "gh-old--one.json").exists())  # seen only before the start
        self.assertEqual(E.build(self.data)["since"], dates[-7])  # later builds keep the start
        for bad in ("2099-01-01", dates[0], "2100-01-01", "nonsense"):
            with self.assertRaises(ValueError):
                E.build(self.data, since=bad)
        self.assertEqual(E.build(self.data, since=dates[-5])["since"], dates[-5])  # one of the last seven may be chosen

    def test_a_long_history_keeps_the_newest_in_full_and_counts_all(self):
        import datetime as dt
        dates = [(dt.date(2099, 2, 1) + dt.timedelta(days=i)).isoformat() for i in range(70)]
        for i, d in enumerate(dates):
            e = ev("gh:long/runner", "long/runner", signal=float(i))
            write_json(self.data / "days" / f"{d}.json", day(d, [e], top=[e["id"]] if i == 3 else []), compact=True)
        E.build(self.data, since=E.build(self.data)["since"])
        write_json(self.data / "elements" / "index.json", {"schema": E.INDEX_SCHEMA, "since": "2099-01-01", "elements": []})
        E.build(self.data)  # the pages' own start, long ago
        page = self.page("gh-long--runner")
        self.assertEqual((len(page["appearances"]), len(page["timeline"]), page["days_seen"]), (E.KEEP, 70, 70))
        self.assertEqual((page["first_seen"], page["best_rank"], page["peak"]["signal"]), ("2099-02-01", 1, 69.0))

    def test_a_page_the_redaction_gate_blocks_is_removed_not_left_stale(self):
        E.build(self.data)
        d2 = self.days["2099-01-02"]
        d2["events"][0]["summary"] = "see /Users/someone/notes"
        write_json(self.data / "days" / "2099-01-02.json", d2, compact=True)
        E.build(self.data)  # does not raise SystemExit
        self.assertFalse((self.data / "elements" / "gh-beta--kit.json").exists())

    def test_publish_never_fails_on_elements(self):
        from magpie import publish
        orig = E.build
        E.build = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            publish.refresh_elements("2099-01-02")  # prints, does not raise
        finally:
            E.build = orig

    def test_pages_validate(self):
        try:
            from jsonschema import Draft202012Validator
            from referencing import Registry, Resource
        except ImportError:
            self.skipTest("jsonschema is not installed")
        E.build(self.data)
        base = "https://www.cameronsutcliff.com/magpie-data/schema/"
        schemas = {p.name: json.loads(p.read_text()) for p in (ROOT / "data/v1/schema").glob("*.json")}
        reg = Registry().with_resources([(base + n, Resource.from_contents(s)) for n, s in schemas.items()])
        page = Draft202012Validator(schemas["element.schema.json"], registry=reg)
        index = Draft202012Validator(schemas["elements.schema.json"], registry=reg)
        for p in (self.data / "elements").glob("*.json"):
            v = index if p.name == "index.json" else page
            self.assertEqual([e.message for e in v.iter_errors(json.loads(p.read_text()))], [], p.name)


if __name__ == "__main__":
    unittest.main()
