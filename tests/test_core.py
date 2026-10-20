import json
import unittest

from magpie.build import build, rank_and_select
from magpie.items import Item
from magpie.util import canonical_keys, now_utc
import magpie.__main__ as cli


def item(source_id, kind, url, title, body="", **metrics):
    return Item(source_id=source_id, source_kind=kind, source_name=source_id, url=url, title=title,
                summary=title, published_at="2099-01-01T00:00:00Z", metrics=metrics).finalize(body).to_dict()


class CoreTests(unittest.TestCase):
    def test_canonical_keys(self):
        self.assertEqual(canonical_keys("https://github.com/Foo/Bar.git"), ["gh:foo/bar"])
        self.assertEqual(canonical_keys("https://huggingface.co/papers/2510.12345"), ["arxiv:2510.12345"])
        self.assertEqual(canonical_keys("https://github.com/sponsors/someone"), [])

    def test_url_key_keeps_identity_query(self):
        from magpie.util import url_key
        a = url_key("https://www.youtube.com/watch?v=abc123&si=xyz")
        self.assertEqual(a, url_key("https://youtu.be/abc123"))
        self.assertNotEqual(a, url_key("https://www.youtube.com/watch?v=def456"))
        self.assertNotEqual(url_key("https://news.ycombinator.com/item?id=1"), url_key("https://news.ycombinator.com/item?id=2"))
        self.assertEqual(url_key("https://example.com/post/?utm_source=x"), "url:example.com/post")

    def test_mentions_corroborate_without_new_events(self):
        items = [
            item("github-trending-daily", "repo", "https://github.com/acme/agentkit", "acme/agentkit", stars_day=900),
            item("hacker-news", "community", "https://github.com/acme/agentkit", "Show HN: AgentKit, an agent harness"),
            item("ainews", "newsletter", "https://news.example/issue", "Issue 42", body="see https://github.com/acme/agentkit"),
        ]
        events = build(items, {"keys": {}}, "2099-01-01")["events"]
        ev = next(e for e in events if e["key"] == "gh:acme/agentkit")
        self.assertEqual(ev["source_groups"], 3)
        self.assertEqual(ev["evidence"], "corroborated")
        self.assertEqual(len(ev["mentions"]), 1)

    def test_edited_events_lead(self):
        items = [item("github-trending-daily", "repo", f"https://github.com/a/agent{i}", f"a/agent{i} llm agent", stars_day=100 * i)
                 for i in range(1, 8)]
        events = build(items, {"keys": {}}, "2099-01-01")["events"]
        self.assertEqual(len(events), 7)
        weakest = events[-1]
        weakest["brief"] = {"impact": 5, "headline": "x"}
        self.assertEqual(rank_and_select(events), [weakest["id"]])

    def test_redaction_gate(self):
        with self.assertRaises(SystemExit):
            cli.scrub({"x": "/Users/someone/secret"})
        cli.scrub({"x": "Apache Spark ships a new connector"})

    def test_possibilities_ground_and_rank(self):
        from magpie.possibilities import rank, ungrounded_numbers
        rows = [{"id": "a", "what": "runs 7B models in 4 GB"}, {"id": "b", "what": "adds JSON output"}]
        ok = {"connection": "A 7B model in 4 GB can emit JSON", "chain": ["then a test with 20 files"]}
        self.assertEqual(ungrounded_numbers(ok, rows), ["20"])
        self.assertEqual(ungrounded_numbers({"connection": "A 7B model", "chain": []}, rows), [])
        quick = {"effort": "a week", "scores": {"value": 4, "feasibility": 4, "evidence": 3, "novelty": 3}}
        slow = {"effort": "a quarter", "scores": {"value": 5, "feasibility": 4, "evidence": 3, "novelty": 3}}
        self.assertIs(rank([slow, quick])[0], quick)

    def test_scout_inbox_doubt_and_roster(self):
        import tempfile
        from pathlib import Path
        from magpie.collectors import inbox
        with tempfile.TemporaryDirectory() as tmp:
            box = Path(tmp) / "x"
            box.mkdir()
            ran = now_utc().isoformat()
            (box / "a.json").write_text(json.dumps({"ran_at": ran, "items": [
                {"title": "Model X claims SOTA", "url": "https://x.com/a/status/1", "handle": "@a", "summary": "Claims a record.",
                 "links": ["https://github.com/a/x"], "evidence": "claim",
                 "doubt": {"level": 1, "community_note": "The benchmark was run on its own training set."}},
                {"title": "Repo Y", "url": "https://x.com/b/status/2", "handle": "@b", "summary": "Ships Y.", "evidence": "artifact"}]}))
            roster_file = Path(tmp) / "roster.json"
            roster_file.write_text(json.dumps({"accounts": [
                {"handle": "@new_lab", "group": "labs", "status": "trial"},
                {"handle": "@_inception_ai", "status": "retired", "retired_reason": "test"}]}))
            old = inbox.INBOX, inbox.ROSTER
            inbox.INBOX, inbox.ROSTER = box, roster_file
            try:
                items = {i.url: i for i in inbox.x_from_grok_bot()}
                handles = {a["handle"] for a in inbox.roster()}
            finally:
                inbox.INBOX, inbox.ROSTER = old
        flagged = items["https://x.com/a/status/1"]
        self.assertEqual(flagged.extra["doubt"]["level"], 2)
        self.assertIn("Flagged on X: Community Note", flagged.summary)
        self.assertLessEqual(flagged.trust, 0.4)
        self.assertEqual(items["https://x.com/b/status/2"].trust, 0.7)
        self.assertIn("@new_lab", handles)
        self.assertNotIn("@_inception_ai", handles)

    def test_saved_posts_defer_to_scout(self):
        import os
        import tempfile
        from pathlib import Path
        from magpie.collectors import inbox
        with tempfile.TemporaryDirectory() as tmp:
            box = Path(tmp) / "x"
            box.mkdir()
            ran = now_utc().isoformat()
            (box / "s.json").write_text(json.dumps({"ran_at": ran, "items": [
                {"title": "Vetted", "url": "https://x.com/a/status/1", "origin": "saved", "handle": "@a"}],
                "saved_rejected": [{"url": "https://x.com/b/status/2", "reason": "funding news"}]}))
            saves = Path(tmp) / "saved.jsonl"
            saves.write_text("\n".join(json.dumps({"url": f"https://x.com/{h}/status/{n}", "text": "t", "author_username": h,
                                                    "created_at": ran, "first_seen_at": ran}) for h, n in (("a", 1), ("b", 2), ("c", 3))))
            old = inbox.INBOX, os.environ.get("MAGPIE_X_SAVED")
            inbox.INBOX, os.environ["MAGPIE_X_SAVED"] = box, str(saves)
            try:
                saved = inbox.x_saved()
                bot = inbox.x_from_grok_bot()
            finally:
                inbox.INBOX = old[0]
                if old[1] is None:
                    os.environ.pop("MAGPIE_X_SAVED", None)
                else:
                    os.environ["MAGPIE_X_SAVED"] = old[1]
        self.assertEqual([i.url for i in saved], ["https://x.com/c/status/3"])
        self.assertTrue(saved[0].extra["saved"])
        self.assertTrue(bot[0].extra["saved"])

    def test_rising_readme_and_buzz(self):
        import tempfile
        from pathlib import Path
        from magpie import rising
        md = "# Tool\n\nEnglish \u00b7 Deutsch \u00b7 Espanol\n\n[![b](x.svg)](y)\n\nTool serves **local models** with [fast decoding](z) on a laptop.\n"
        self.assertEqual(rising._plain(md), "Tool serves local models with fast decoding on a laptop.")
        events = [{"id": "e1", "key": "gh:a/tool", "sources": [
            {"name": "GitHub trending", "kind": "repo", "url": "https://github.com/a/tool", "title": "a/tool"},
            {"name": "X: @a", "kind": "social", "url": "https://x.com/a/status/1", "title": "Tool ships"}], "mentions": []}]
        with tempfile.TemporaryDirectory() as tmp:
            out = rising.enrich({"day": [{"repo": "a/Tool"}]}, events, Path(tmp), fetch=False)
        row = out["day"][0]
        self.assertEqual(row["event_id"], "e1")
        self.assertEqual([b["url"] for b in row["buzz"]], ["https://x.com/a/status/1"])

    def test_json_feed(self):
        import tempfile
        from pathlib import Path
        from magpie import publish
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            (data / "days").mkdir()
            ev = {"id": "e1", "title": "a/tool", "url": "https://github.com/a/tool", "topics": ["agents"],
                  "brief": {"headline": "Tool ships", "newly_possible": "Agents can run it locally."}}
            day = {"date": "2099-01-02", "generated_at": "2099-01-02T11:00:00Z", "edition": {"headline": "Tool ships", "dek": "One line."},
                   "events": [ev], "top": ["e1"], "coverage": {"events": 1}, "possibilities": {"items": [{"title": "A new idea"}]}}
            (data / "days" / "2099-01-02.json").write_text(json.dumps(day))
            old = publish.DATA
            publish.DATA = data
            try:
                publish.write_feed()
                feed = json.loads((data / "feed.json").read_text())
            finally:
                publish.DATA = old
        self.assertEqual(feed["version"], "https://jsonfeed.org/version/1.1")
        item = feed["items"][0]
        self.assertEqual(item["id"], "2099-01-02")
        self.assertIn("Tool ships", item["content_html"])
        self.assertIn("A new idea", item["content_html"])


if __name__ == "__main__":
    unittest.main()
