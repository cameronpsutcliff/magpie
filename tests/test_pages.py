"""page_url: every capability and possibility in a day file links its own page, with an id that never moves."""
import json
import unittest
from pathlib import Path

from magpie import pages as P
from magpie.util import short_hash

ROOT = Path(__file__).resolve().parent.parent
SITE = "https://www.cameronsutcliff.com/magpie"


def day(date="2099-01-05", **kw):
    d = {"date": date, "events": [{"id": short_hash("gh:acme/kit", 12), "key": "gh:acme/kit"},
                                  {"id": short_hash("hf:acme/model", 12), "key": "hf:acme/model"}],
         "top": [short_hash("hf:acme/model", 12)],
         "possibilities": {"items": [{"id": f"{date}-desks-backtest-overnight", "title": "Desks backtest overnight"}]},
         "recent": {"possibilities": [{"date": "2099-01-01", "id": "2099-01-01-clinics-run-speech-locally", "title": "Clinics"}]}}
    d.update(kw)
    return d


class PageIds(unittest.TestCase):
    def test_a_capability_page_is_the_date_and_its_event_id(self):
        self.assertEqual(P.item_id("2099-01-05", "02d0f9ac3aca"), "2099-01-05-02d0f9ac3aca")
        self.assertEqual(P.item_url("2099-01-05", "02d0f9ac3aca"), f"{SITE}/c/2099-01-05-02d0f9ac3aca")
        for date, i in [("2099-1-5", "02d0f9ac3aca"), ("2099-02-30", "02d0f9ac3aca"), ("2099-01-05", "02D0F9AC3ACA"), ("2099-01-05", "../x"), (None, "02d0f9ac3aca"),
                        ("2099-01-05", None), ("2099-01-05", "02d0f9ac3ac")]:
            self.assertIsNone(P.item_url(date, i), (date, i))

    def test_the_same_capability_on_two_days_keeps_its_event_id_and_gets_a_page_each(self):
        a, b = day("2099-01-05"), day("2099-01-06")
        P.stamp(a)
        P.stamp(b)
        self.assertEqual(a["events"][0]["id"], b["events"][0]["id"])  # a hash of the key, whatever the day
        self.assertEqual(a["events"][0]["page_url"], f"{SITE}/c/2099-01-05-{a['events'][0]['id']}")
        self.assertEqual(b["events"][0]["page_url"], f"{SITE}/c/2099-01-06-{a['events'][0]['id']}")

    def test_an_idea_page_needs_a_safe_id_from_its_own_edition(self):
        self.assertEqual(P.idea_url("2099-01-05-desks", "2099-01-05"), f"{SITE}/i/2099-01-05-desks")
        self.assertIsNone(P.idea_url("2099-01-04-desks", "2099-01-05"))
        self.assertIsNone(P.idea_url("idea-2099-01-05-1"))
        self.assertIsNone(P.idea_url("2099-01-05-Desks"))
        self.assertIsNone(P.idea_url("2099-02-30-desks"))


class Stamp(unittest.TestCase):
    def test_it_links_capabilities_possibilities_and_recent_possibilities(self):
        d = day()
        self.assertEqual(P.stamp(d), 4)
        self.assertEqual([e["page_url"] for e in d["events"]],
                         [f"{SITE}/c/2099-01-05-{short_hash('gh:acme/kit', 12)}", f"{SITE}/c/2099-01-05-{short_hash('hf:acme/model', 12)}"])
        self.assertEqual(d["possibilities"]["items"][0]["page_url"], f"{SITE}/i/2099-01-05-desks-backtest-overnight")
        self.assertEqual(d["recent"]["possibilities"][0]["page_url"], f"{SITE}/i/2099-01-01-clinics-run-speech-locally")

    def test_it_is_idempotent_and_drops_a_stale_link(self):
        d = day()
        P.stamp(d)
        self.assertEqual(P.stamp(d), 0)
        d["events"][0]["id"] = "not-an-id"
        d["possibilities"]["items"][0]["page_url"] = f"{SITE}/i/elsewhere"
        self.assertEqual(P.stamp(d), 2)
        self.assertNotIn("page_url", d["events"][0])
        self.assertEqual(d["possibilities"]["items"][0]["page_url"], f"{SITE}/i/2099-01-05-desks-backtest-overnight")

    def test_an_odd_day_is_left_alone(self):
        self.assertEqual(P.stamp({"date": "2099-01-05", "events": None, "possibilities": [], "recent": None}), 0)
        self.assertEqual(P.stamp(None), 0)

    def test_publish_and_the_constraints_lane_stamp_before_they_write(self):
        # Both need live sources to run end to end, so the order of the calls is what is checked here.
        src = (ROOT / "magpie/publish.py").read_text()
        self.assertLess(src.index("assign_ids(day)"), src.index("stamp(day)"))
        self.assertLess(src.index("stamp(day)"), src.index("write_json(day_path, day"))
        text = (ROOT / "magpie/constraints.py").read_text()
        self.assertLess(text.index("stamp(day)"), text.index('write_json(DATA / "days"'))

    def test_the_possibilities_lane_writes_new_possibilities_with_ids_and_page_urls(self):
        import shutil
        import tempfile
        from unittest import mock

        from magpie import possibilities, publish
        from magpie.util import write_json

        tmp = Path(tempfile.mkdtemp())
        try:
            d = day("2099-01-05")
            d.pop("recent")
            write_json(tmp / "days" / "2099-01-05.json", d, compact=True)
            write_json(tmp / "index.json", {"schema": "magpie.index/1", "days": [{"date": "2099-01-05"}], "latest": "2099-01-05"})
            # As generate returns them, and also with no id at all: either way they are written with both.
            fresh = {"items": [{"title": "Labs qualify a local model", "ingredients": [], "id": "2099-01-05-labs-qualify-a-local-model"},
                               {"title": "Clinics review visits", "ingredients": []}]}
            with mock.patch.object(publish, "DATA", tmp), mock.patch.object(possibilities, "generate", return_value=fresh), \
                    mock.patch.object(publish, "refresh_elements"), mock.patch.object(publish, "refresh_views"):
                possibilities.run_for("2099-01-05")
            written = json.loads((tmp / "days" / "2099-01-05.json").read_text())["possibilities"]["items"]
            self.assertEqual([p["id"] for p in written], ["2099-01-05-labs-qualify-a-local-model", "2099-01-05-clinics-review-visits"])
            self.assertEqual([p["page_url"] for p in written], [f"{SITE}/i/{p['id']}" for p in written])
            self.assertTrue((tmp / "latest.json").exists())
        finally:
            shutil.rmtree(tmp)


class PublishedDays(unittest.TestCase):
    def test_every_published_item_links_its_page(self):
        for path in [ROOT / "data/v1/latest.json", *sorted((ROOT / "data/v1/days").glob("*.json"))[-3:]]:
            d = json.loads(path.read_text())
            with self.subTest(path.name):
                for e in d["events"]:
                    self.assertEqual(e.get("page_url"), f"{SITE}/c/{d['date']}-{e['id']}")
                for p in (d.get("possibilities") or {}).get("items") or []:
                    self.assertEqual(p.get("page_url"), f"{SITE}/i/{p['id']}")

    def test_the_day_schema_describes_page_url(self):
        try:
            from jsonschema import Draft202012Validator, FormatChecker
            from referencing import Registry, Resource
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema = json.loads((ROOT / "data/v1/schema/day.schema.json").read_text())
        reg = Registry().with_resources([(schema["$id"], Resource.from_contents(schema))])
        event = Draft202012Validator({"$ref": schema["$id"] + "#/$defs/event/properties/page_url"}, registry=reg, format_checker=FormatChecker())
        idea = Draft202012Validator({"$ref": schema["$id"] + "#/$defs/possibility/properties/page_url"}, registry=reg, format_checker=FormatChecker())
        d = json.loads((ROOT / "data/v1/latest.json").read_text())
        for e in d["events"]:
            self.assertTrue(event.is_valid(e["page_url"]), e["page_url"])
        for p in d["possibilities"]["items"]:
            self.assertTrue(idea.is_valid(p["page_url"]), p["page_url"])
        self.assertFalse(event.is_valid(f"{SITE}/c/2099-01-05-NOTHEX"))
        self.assertFalse(event.is_valid(f"{SITE}/2099-01-05"))


if __name__ == "__main__":
    unittest.main()
