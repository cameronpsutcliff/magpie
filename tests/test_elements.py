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

    def rebuild_like(self, seq=None):
        """Rebuild self.data from where its daily run started (its own index, or seq's): never a backfill."""
        src = (seq or self.data) / "elements" / "index.json"
        since = json.loads(src.read_text())["since"] if src.exists() else "2099-01-01"
        (self.data / "elements").mkdir(parents=True, exist_ok=True)
        write_json(self.data / "elements" / "index.json", {"schema": E.INDEX_SCHEMA, "since": since, "updated": None, "elements": []})
        return E.rebuild(self.data)

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
        E.rebuild(self.data)
        index = json.loads((self.data / "elements" / "index.json").read_text())
        keys = {e["key"] for e in index["elements"]}
        # beta: two days. acme/agent and gamma: a new possibility. acme/other: an action on record names Acme.
        self.assertEqual(keys, {"gh:beta/kit", "gh:acme/agent", "gh:gamma/tool", "gh:acme/other"})
        self.assertNotIn("gh:solo/once", keys)  # one day, no links

    def test_page_contents(self):
        E.rebuild(self.data)
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
        E.rebuild(self.data)
        beta = self.page("gh-beta--kit")
        self.assertEqual(beta["appearances"][0]["named_integrations"],
                         [{"name": "ollama", "how": "runs it locally"}, {"name": "LangChain"}, {"name": "Ollama"}])
        self.assertEqual(beta["named_integrations"], [
            {"name": "ollama", "days": 2, "first_seen": "2099-01-01", "last_seen": "2099-01-02", "how": ["runs it locally", "serves its models"]},
            {"name": "LangChain", "days": 1, "first_seen": "2099-01-02", "last_seen": "2099-01-02", "how": []},
            {"name": "vLLM", "days": 1, "first_seen": "2099-01-01", "last_seen": "2099-01-01", "how": []}])
        self.assertEqual(beta["works_with"][0]["key"], "gh:gamma/tool")  # the computed list is untouched
        self.assertNotIn("named_integrations", self.page("gh-acme--agent"))  # none named: no field, as before

        built = self.pages()
        seq = self.tmp / "seq"
        shutil.copytree(self.data, seq)
        shutil.rmtree(seq / "elements")
        for d in sorted(self.days):
            E.update(d, seq)
        self.assertEqual(self.pages(seq), built)

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
        self.assertFalse((self.data / "elements" / "gh-beta--kit.json").exists())  # back to one day, no links
        index = json.loads((self.data / "elements" / "index.json").read_text())
        self.assertNotIn("gh:beta/kit", {e["key"] for e in index["elements"]})

    def test_an_action_naming_a_maker_backfills_its_one_off_items(self):
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": []})
        for d in sorted(self.days):
            E.update(d, self.data)
        self.assertFalse((self.data / "elements" / "gh-acme--other.json").exists())  # one day, no action yet
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": [ACTION]})
        E.update("2099-01-02", self.data)  # the register changed: everything the action could touch is recomputed
        self.assertEqual([c["match"] for c in self.page("gh-acme--other")["constraints"]], ["maker"])
        built = self.pages()
        E.rebuild(self.data)
        self.assertEqual(self.pages(), built)

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

    def test_a_lost_page_file_is_rebuilt_from_the_editions(self):
        E.rebuild(self.data)
        (self.data / "elements" / "gh-beta--kit.json").unlink()
        E.update("2099-01-02", self.data)
        self.assertEqual(self.page("gh-beta--kit")["first_seen"], "2099-01-01")

    def test_a_page_the_redaction_gate_blocks_is_removed_not_left_stale(self):
        E.rebuild(self.data)
        self.assertTrue((self.data / "elements" / "gh-beta--kit.json").exists())
        d2 = self.days["2099-01-02"]
        d2["events"][0]["summary"] = "see /Users/someone/notes"
        write_json(self.data / "days" / "2099-01-02.json", d2, compact=True)
        E.update("2099-01-02", self.data)  # does not raise SystemExit
        self.assertFalse((self.data / "elements" / "gh-beta--kit.json").exists())
        index = json.loads((self.data / "elements" / "index.json").read_text())
        self.assertNotIn("gh:beta/kit", {e["key"] for e in index["elements"]})

    def test_a_removed_action_leaves_no_details_on_its_page(self):
        d2 = self.days["2099-01-02"]
        d2["events"].append(dict(ev(ACTION["key"], ACTION["title"], maker="An agency", kind="constraint"), evidence="official",
                                 constraint={"status": "final"}, brief={"headline": ACTION["headline"]}))
        write_json(self.data / "days" / "2099-01-02.json", d2, compact=True)
        E.rebuild(self.data)
        self.assertIn("constraint", self.page("policy-fr-2099-1"))
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": []})
        E.update("2099-01-02", self.data)
        self.assertNotIn("constraint", self.page("policy-fr-2099-1"))
        self.assertFalse((self.data / "elements" / "gh-acme--other.json").exists())  # it qualified only through the action
        built = self.pages()
        E.rebuild(self.data)
        self.assertEqual(self.pages(), built)

    def test_a_corrected_action_reaches_the_pages_it_touches(self):
        E.rebuild(self.data)
        fixed = dict(ACTION, headline="Agency bars Acme models and services")  # same dates, new words
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": [fixed]})
        E.update("2099-01-01", self.data)
        self.assertEqual(self.page("gh-acme--agent")["constraints"][0]["headline"], fixed["headline"])

    def test_the_timeline_window_folds_older_rows_the_same_way_daily_and_in_a_rebuild(self):
        import datetime as dt
        full, rows = E.FULL_APPEARANCES, E.TIMELINE_ROWS
        E.FULL_APPEARANCES, E.TIMELINE_ROWS = 5, 10
        try:
            start = dt.date(2099, 3, 1)
            dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(25)]
            for i, d in enumerate(dates):
                e = ev("gh:fold/me", "fold/me", signal=[90.0, 10.0][i % 2] if i < 3 else 50.0)
                write_json(self.data / "days" / f"{d}.json", day(d, [e], top=[e["id"]] if i == 1 else []), compact=True)
            seq = self.tmp / "seq"
            shutil.copytree(self.data, seq)
            for d in sorted(p.stem for p in (seq / "days").glob("*.json")):
                E.update(d, seq)
            E.update(dates[12], seq)  # re-publishing a day inside the window changes nothing
            self.rebuild_like(seq)
            self.assertEqual(self.pages(seq), self.pages())
            page = self.page("gh-fold--me")
            self.assertEqual((len(page["timeline"]), len(page["appearances"])), (10, 5))
            self.assertEqual((page["days_seen"], page["first_seen"], page["days_featured"], page["best_rank"]), (25, dates[0], 1, 1))
            self.assertEqual(page["peak"], {"signal": 90.0, "date": dates[2]})
            self.assertEqual(page["older"]["days"], 15)
        finally:
            E.FULL_APPEARANCES, E.TIMELINE_ROWS = full, rows

    def test_an_action_recorded_later_backfills_the_item_that_carried_its_document(self):
        d1 = self.days["2099-01-01"]
        d1["events"].append(ev("url:news.example/acme-order", "Agency orders Acme out", sources=[{"name": "Wire", "url": ACTION["doc_url"], "title": "Order"}]))
        write_json(self.data / "days" / "2099-01-01.json", d1, compact=True)
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": []})
        for d in sorted(self.days):
            E.update(d, self.data)
        slug = E.slug_for("url:news.example/acme-order")
        self.assertFalse((self.data / "elements" / f"{slug}.json").exists())  # no action yet, one day
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": [ACTION]})
        E.update("2099-01-02", self.data)  # the action is seen on 2099-01-02
        self.assertEqual([c["match"] for c in self.page(slug)["constraints"]], ["document"])
        built = self.pages()
        E.rebuild(self.data)
        self.assertEqual(self.pages(), built)

    def test_a_long_history_stays_bounded_and_complete(self):
        import datetime as dt
        start = dt.date(2099, 2, 1)
        dates = [(start + dt.timedelta(days=i)).isoformat() for i in range(70)]
        for i, d in enumerate(dates):
            e = ev("gh:long/runner", "long/runner", signal=float(i), sources=[{"name": f"Feed {i}", "url": f"https://feed.example/{i}", "title": "x"}])
            write_json(self.data / "days" / f"{d}.json", day(d, [e], top=[e["id"]] if i == 3 else []), compact=True)
        seq = self.tmp / "seq"
        shutil.copytree(self.data, seq)
        for d in sorted(p.stem for p in (seq / "days").glob("*.json")):
            E.update(d, seq)
        self.rebuild_like(seq)  # the daily run started on the first edition
        self.assertEqual(self.pages(seq), self.pages())
        page = self.page("gh-long--runner")
        self.assertEqual(len(page["appearances"]), E.FULL_APPEARANCES)
        self.assertEqual(len(page["timeline"]), 70)
        self.assertEqual((page["days_seen"], page["first_seen"], page["best_rank"]), (70, "2099-02-01", 1))
        self.assertEqual(page["peak"], {"signal": 69.0, "date": dates[-1]})
        self.assertEqual(len(page["find"]), 40)  # where to find it reads the newest appearances

    def test_possibilities_are_never_cut(self):
        d2 = self.days["2099-01-02"]
        a, c = self.days["2099-01-01"]["events"][0], d2["events"][1]
        d2["possibilities"] = {"items": [{"title": f"Idea {i}", "ingredients": [a["id"], c["id"]],
                                          "ingredient_refs": [{"id": a["id"], "title": "A", "date": "2099-01-01"}, {"id": c["id"], "title": "C", "date": "2099-01-02"}]}
                                         for i in range(45)]}
        write_json(self.data / "days" / "2099-01-02.json", d2, compact=True)
        E.rebuild(self.data)
        E.update("2099-01-02", self.data)
        self.assertEqual(len(self.page("gh-acme--agent")["possibilities"]), 45)

    def test_pages_start_today_when_nothing_was_seeded(self):
        E.update("2099-01-02", self.data)  # no index yet: the pages start on this edition
        index = json.loads((self.data / "elements" / "index.json").read_text())
        self.assertEqual(index["since"], "2099-01-02")
        # beta/kit also appeared on 2099-01-01, before the start: one appearance, no links, so no page
        self.assertNotIn("gh:beta/kit", {e["key"] for e in index["elements"]})
        E.update("2099-01-01", self.data)  # an edition before the start is never folded in
        self.assertEqual(json.loads((self.data / "elements" / "index.json").read_text()), index)

    def test_a_seed_reaches_back_seven_editions_at_most(self):
        import datetime as dt
        for i in range(10):
            d = (dt.date(2099, 4, 1) + dt.timedelta(days=i)).isoformat()
            write_json(self.data / "days" / f"{d}.json", day(d, [ev("gh:old/one", "old/one")] if i < 3 else []), compact=True)
        result = E.rebuild(self.data)
        self.assertEqual(result["since"], "2099-04-04")  # the last seven of the edition files
        self.assertFalse((self.data / "elements" / "gh-old--one.json").exists())  # seen only before the seed window
        with self.assertRaises(ValueError):  # reaching further back would backfill the archive
            E.rebuild(self.data, since="2099-04-01")
        self.assertFalse((self.data / "elements" / "gh-old--one.json").exists())

    def test_an_old_start_allows_only_itself_or_the_last_seven_editions(self):
        import datetime as dt
        dates = [(dt.date(2099, 8, 1) + dt.timedelta(days=i)).isoformat() for i in range(30)]
        for d in dates:
            write_json(self.data / "days" / f"{d}.json", day(d, []), compact=True)
        E.update(dates[0], self.data)  # the pages started long ago
        self.assertEqual(E.rebuild(self.data)["since"], dates[0])  # a plain rebuild keeps the start
        self.assertEqual(E.rebuild(self.data, since=dates[0])["since"], dates[0])
        with self.assertRaises(ValueError):  # with the old start in place, a date between it and the seed is refused
            E.rebuild(self.data, since=dates[10])
        self.assertEqual(json.loads((self.data / "elements" / "index.json").read_text())["since"], dates[0])
        self.assertEqual(E.rebuild(self.data, since=dates[-7])["since"], dates[-7])  # one of the last seven
        for bad in ("2100-01-01", "2099-08-31", "not-a-date"):  # a future date, a day with no edition, nonsense
            with self.assertRaises(ValueError):
                E.rebuild(self.data, since=bad)
        self.assertEqual(json.loads((self.data / "elements" / "index.json").read_text())["since"], dates[-7])

    def test_a_corrected_document_reaches_existing_pages_and_the_action_page(self):
        E.rebuild(self.data)
        # beta/kit (two days, maker "Open source") links nothing; the action now cites the newsletter that covered it
        fixed = dict(ACTION, sources=[{"name": "AI News", "url": NEWS["url"], "title": "Issue 7"}], status="in_force")
        d2 = self.days["2099-01-02"]
        d2["events"].append(dict(ev(ACTION["key"], ACTION["title"], maker="An agency", kind="constraint"), evidence="official",
                                 constraint={"status": "final"}, brief={"headline": ACTION["headline"]}))
        write_json(self.data / "days" / "2099-01-02.json", d2, compact=True)
        E.rebuild(self.data)
        write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": [fixed]})  # same dates
        E.update("2099-01-01", self.data)
        self.assertEqual([c["match"] for c in self.page("gh-beta--kit")["constraints"]], ["reported"])
        self.assertEqual(self.page("policy-fr-2099-1")["constraint"]["status"], "in_force")
        built = self.pages()
        E.rebuild(self.data)
        self.assertEqual(self.pages(), built)

    def test_evidence_that_leaves_the_full_window_still_links_and_new_actions_find_it(self):
        import datetime as dt
        full = E.FULL_APPEARANCES
        E.FULL_APPEARANCES = 3
        try:
            write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": []})
            dates = [(dt.date(2099, 5, 1) + dt.timedelta(days=i)).isoformat() for i in range(8)]
            for i, d in enumerate(dates):
                srcs = [{"name": "Wire", "url": ACTION["doc_url"], "title": "Order"}] if i == 0 else []
                write_json(self.data / "days" / f"{d}.json", day(d, [ev("gh:win/dow", "win/dow", sources=srcs)]), compact=True)
            seq = self.tmp / "seq"
            shutil.copytree(self.data, seq)
            for d in sorted(p.stem for p in (seq / "days").glob("*.json")):
                E.update(d, seq)
            # the action arrives after the document left the full window
            write_json(seq / "constraints.json", {"schema": "magpie.constraints/1", "items": [dict(ACTION, first_seen=dates[-1], last_seen=dates[-1])]})
            E.update(dates[-1], seq)
            page = json.loads((seq / "elements" / "gh-win--dow.json").read_text())
            self.assertEqual([c["match"] for c in page["constraints"]], ["document"])
            self.assertEqual(page["older"]["links"], {ACTION["key"]: "document"})
            E.update(dates[-1], seq)  # and a quiet day keeps it
            write_json(self.data / "constraints.json", {"schema": "magpie.constraints/1", "items": [dict(ACTION, first_seen=dates[-1], last_seen=dates[-1])]})
            self.rebuild_like(seq)
            self.assertEqual(self.pages(seq), self.pages())
        finally:
            E.FULL_APPEARANCES = full

    def test_republishing_away_old_evidence_drops_the_carried_link(self):
        import datetime as dt
        full = E.FULL_APPEARANCES
        E.FULL_APPEARANCES = 3
        try:
            dates = [(dt.date(2099, 7, 1) + dt.timedelta(days=i)).isoformat() for i in range(8)]
            for i, d in enumerate(dates):
                srcs = [{"name": "Wire", "url": ACTION["doc_url"], "title": "Order"}] if i == 0 else []
                write_json(self.data / "days" / f"{d}.json", day(d, [ev("gh:old/proof", "old/proof", sources=srcs)]), compact=True)
            for d in sorted(p.stem for p in (self.data / "days").glob("*.json")):
                E.update(d, self.data)
            page = self.page("gh-old--proof")
            self.assertEqual(page["older"]["links"], {ACTION["key"]: "document"})  # the document left the full window
            # the first day is corrected: it never carried the document
            write_json(self.data / "days" / f"{dates[0]}.json", day(dates[0], [ev("gh:old/proof", "old/proof")]), compact=True)
            E.update(dates[0], self.data)
            page = self.page("gh-old--proof")
            self.assertNotIn("links", page.get("older") or {})
            self.assertEqual([c["key"] for c in page["constraints"] if c["match"] != "maker"], [])
            built = self.pages()
            self.rebuild_like()
            self.assertEqual(self.pages(), built)
        finally:
            E.FULL_APPEARANCES = full

    def test_republishing_a_day_the_page_no_longer_holds_recomputes_it(self):
        import datetime as dt
        rows = E.TIMELINE_ROWS
        E.TIMELINE_ROWS = 3
        try:
            dates = [(dt.date(2099, 6, 1) + dt.timedelta(days=i)).isoformat() for i in range(6)]
            for d in dates:
                e = ev("gh:re/pub", "re/pub")
                write_json(self.data / "days" / f"{d}.json", day(d, [e], top=[e["id"]]), compact=True)
            for d in sorted(p.stem for p in (self.data / "days").glob("*.json")):
                E.update(d, self.data)
            self.assertEqual(self.page("gh-re--pub")["days_featured"], 6)
            first = self.days.get(dates[0]) or day(dates[0], [ev("gh:re/pub", "re/pub")])  # corrected: it did not lead
            write_json(self.data / "days" / f"{dates[0]}.json", first, compact=True)
            E.update(dates[0], self.data)
            self.assertEqual(self.page("gh-re--pub")["days_featured"], 5)
            built = self.pages()
            self.rebuild_like()
            self.assertEqual(self.pages(), built)
        finally:
            E.TIMELINE_ROWS = rows

    def test_possibilities_fold_past_the_newest_sixty(self):
        kept = E.POSSIBILITIES_KEPT
        E.POSSIBILITIES_KEPT = 10
        try:
            d2 = self.days["2099-01-02"]
            a, c = self.days["2099-01-01"]["events"][0], d2["events"][1]
            d2["possibilities"] = {"items": [{"title": f"Idea {i}", "ingredients": [a["id"], c["id"]],
                                              "ingredient_refs": [{"id": a["id"], "title": "A", "date": "2099-01-01"}, {"id": c["id"], "title": "C", "date": "2099-01-02"}]}
                                             for i in range(15)]}
            write_json(self.data / "days" / "2099-01-02.json", d2, compact=True)
            E.rebuild(self.data)
            page = self.page("gh-acme--agent")
            self.assertEqual((len(page["possibilities"]), page["possibilities_total"], page["older"]["possibilities"]), (10, 15, 5))
            built = self.pages()
            E.update("2099-01-02", self.data)  # the day's ideas are partly folded: recomputed, not double counted
            self.assertEqual(self.pages(), built)
        finally:
            E.POSSIBILITIES_KEPT = kept

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
