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
                                                                        "impact": 4, "ripples": [{"topic": "agents", "effect": "Agents get gamma"}]})
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

    def test_slugs(self):
        self.assertEqual(E.slug_base("gh:Owner/Repo.js"), "gh-owner-repo-js")
        long = E.slug_base("url:example.com/" + "a-very-long-path/" * 8)
        self.assertLessEqual(len(long), 72)
        self.assertRegex(long, r"-[0-9a-f]{8}$")
        taken = {"gh-a-b-c": "gh:a/b-c"}
        self.assertEqual(E.slug_for("gh:a/b-c", taken), "gh-a-b-c")
        self.assertRegex(E.slug_for("gh:a-b/c", taken), r"^gh-a-b-c-[0-9a-f]{8}$")

    def test_which_elements_get_pages(self):
        E.rebuild(self.data)
        index = json.loads((self.data / "elements" / "index.json").read_text())
        keys = {e["key"] for e in index["elements"]}
        # beta: two days. acme/agent and gamma: a new possibility. acme/other: an action on record names Acme.
        self.assertEqual(keys, {"gh:beta/kit", "gh:acme/agent", "gh:gamma/tool", "gh:acme/other"})
        self.assertNotIn("gh:solo/once", keys)  # one day, no links

    def test_page_contents(self):
        E.rebuild(self.data)
        agent = self.page("gh-acme-agent")
        self.assertEqual(agent["link"], {"url": "https://github.com/acme/agent", "label": "GitHub"})
        self.assertEqual(agent["name"], "acme/agent")
        self.assertEqual([p["title"] for p in agent["possibilities"]], ["Agent plus gamma"])
        self.assertEqual(agent["possibilities"][0]["date"], "2099-01-02")  # the day the idea ran, not the ingredient's day
        self.assertEqual(agent["works_with"][0]["key"], "gh:gamma/tool")
        self.assertEqual([c["match"] for c in agent["constraints"]], ["maker"])
        gamma = self.page("gh-gamma-tool")
        self.assertEqual(gamma["days_featured"], 1)
        self.assertEqual(gamma["best_rank"], 1)
        self.assertEqual(gamma["knock_on"][0]["effects"][0]["effect"], "Agents get gamma")
        beta = self.page("gh-beta-kit")
        self.assertEqual(beta["days_seen"], 2)
        self.assertEqual(beta["first_seen"], "2099-01-01")
        tie = next(w for w in beta["works_with"] if w["key"] == "gh:gamma/tool")
        self.assertEqual(tie["together"], 1)  # the same newsletter covered both on 2099-01-02
        self.assertIn("AI News: Issue 7", tie["via"])
        self.assertEqual([f["url"] for f in beta["find"]], [NEWS["url"]])  # the repo link itself is the canonical link

    def test_update_is_idempotent_and_matches_rebuild(self):
        E.rebuild(self.data)
        built = self.pages()
        E.update("2099-01-02", self.data)
        self.assertEqual(self.pages(), built)

        seq = self.tmp / "seq"
        shutil.copytree(self.data, seq)
        shutil.rmtree(seq / "elements")
        for d in sorted(self.days):
            E.update(d, seq)
        self.assertEqual(self.pages(seq), built)

    def test_republish_replaces_the_day(self):
        E.rebuild(self.data)
        d2 = self.days["2099-01-02"]
        d2["events"] = [e for e in d2["events"] if e["key"] != "gh:beta/kit"]
        write_json(self.data / "days" / "2099-01-02.json", d2, compact=True)
        E.update("2099-01-02", self.data)
        self.assertFalse((self.data / "elements" / "gh-beta-kit.json").exists())  # back to one day, no links
        index = json.loads((self.data / "elements" / "index.json").read_text())
        self.assertNotIn("gh:beta/kit", {e["key"] for e in index["elements"]})

    def test_action_on_record_only_counts_from_its_first_sighting(self):
        early = dict(ACTION, first_seen="2099-01-03")  # recorded after acme/other appeared
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": [early]})
        E.rebuild(self.data)
        self.assertFalse((self.data / "elements" / "gh-acme-other.json").exists())

    def test_action_page_lists_the_pages_it_names(self):
        d2 = self.days["2099-01-02"]
        d2["events"].append(dict(ev(ACTION["key"], ACTION["title"], maker="An agency", kind="constraint"), evidence="official",
                                 constraint={"status": "final", "jurisdiction": "US"},
                                 brief={"headline": ACTION["headline"], "harder": ACTION["harder"]}))
        write_json(self.data / "days" / "2099-01-02.json", d2, compact=True)
        E.rebuild(self.data)
        page = self.page("policy-fr-2099-1")
        self.assertEqual(page["name"], "Agency bars Acme models")
        self.assertEqual(page["link"]["url"], ACTION["doc_url"])
        self.assertEqual({n["key"] for n in page["names"]}, {"gh:acme/agent", "gh:acme/other"})
        self.assertEqual(page["constraint"]["who"], ACTION["who"])

    def test_publish_never_fails_on_elements(self):
        from magpie import publish
        orig = E.update
        E.update = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        try:
            publish.refresh_elements("2099-01-02")  # prints, does not raise
        finally:
            E.update = orig

    def test_pages_validate(self):
        try:
            from jsonschema import Draft202012Validator
            from referencing import Registry, Resource
        except ImportError:
            self.skipTest("jsonschema is not installed")
        E.rebuild(self.data)
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
