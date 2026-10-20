import unittest

from magpie.build import build, rank_and_select
from magpie.items import Item
from magpie.util import canonical_keys
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


if __name__ == "__main__":
    unittest.main()
