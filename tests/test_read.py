import json
import unittest
from unittest import mock

from magpie import read as R
from magpie.collectors import feeds
from magpie.util import ungrounded_numbers


def row(i, **over):
    base = {"id": i, "headline": f"Thing {i}", "newly_possible": "Run a 27B model on one GPU.", "why": "Cheaper.", "limits": "",
            "maker": "Acme", "evidence": "reproduced", "impact": 4, "metrics": {"stars_day": 900}, "sources": ["Acme ships it"]}
    return {**base, **over}


class ReadTests(unittest.TestCase):
    def test_numbers_are_whole_tokens(self):
        self.assertEqual(ungrounded_numbers(["900 stars"], "gained 900 stars"), [])
        self.assertEqual(ungrounded_numbers(["90 stars"], "gained 900 stars at /90/"), ["90"])

    def test_keep_needs_known_ids_and_grounded_numbers(self):
        rows = {"a": row("a"), "b": row("b")}
        paras = [
            {"lead": "Acme put a 27B model on one GPU.", "body": "It gained 900 stars today.", "ids": ["a"]},
            {"lead": "Unknown thing.", "body": "x", "ids": ["zzz"]},
            {"lead": "Acme cut costs 73%.", "body": "", "ids": ["b"]},
            "not a paragraph",
        ]
        kept = R.keep(paras, rows)
        self.assertEqual([p["ids"] for p in kept], [["a"]])

    def test_generate_reads_the_ranked_top(self):
        day = {"date": "2099-01-01", "edition": {"headline": "H", "dek": "D"}, "top": ["a", "b", "c"],
               "events": [{"id": k, "title": f"T{k}", "summary": "", "brief": {"headline": f"Thing {k}"}, "metrics": {}, "sources": []}
                          for k in ("a", "b", "c")]}
        seen = {}

        def lane(prompt, timeout):
            seen["prompt"] = prompt
            return json.dumps({"read": [{"lead": "Thing a leads.", "body": "Thing b follows.", "ids": ["a", "b"]}]})

        with mock.patch.dict(R.LANES, {"claude": lane}, clear=True):
            out = R.generate(day)
        self.assertEqual(out["items"][0]["ids"], ["a", "b"])
        self.assertIn('"id": "c"', seen["prompt"])

    def test_ai_only_feeds_drop_other_topics(self):
        self.assertTrue(feeds.AI_TEXT.search("Renting the frontier: how we cut AI forecast costs"))
        self.assertTrue(feeds.AI_TEXT.search("AI Agent Harness"))
        self.assertFalse(feeds.AI_TEXT.search("When software meets hardware"))
        self.assertFalse(feeds.AI_TEXT.search("Airport retail strategy"))


if __name__ == "__main__":
    unittest.main()
