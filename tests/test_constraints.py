import csv
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from magpie import constraints as K
from magpie import trackers as T
from magpie.build import build, rank_and_select
from magpie.collectors.policy import is_ai_policy
from magpie.items import Item

DATE = "2099-01-10"


def policy_item(url, title, summary="", **extra):
    it = Item(source_id="federal-register", source_kind="policy", source_name="Federal Register", url=url, title=title,
              summary=summary or title, published_at=f"{DATE}T12:00:00+00:00", trust=0.95,
              keys=[f"policy:fr:{extra.pop('num', 'X1')}"], extra={"primary": True, "jurisdiction": "US", **extra})
    return it.to_dict()


def social(url, title, links=(), kind="social"):
    return Item(source_id="x-grok-bot", source_kind=kind, source_name="X: @someone", url=url, title=title, summary=title,
                published_at=f"{DATE}T09:00:00+00:00", extra={"links": list(links)}).finalize(" ".join(links)).to_dict()


class FakeLane:
    """Stands in for a model: returns a fixed constraints payload for whatever ids it is shown."""

    def __init__(self, make):
        self.make, self.prompts = make, []

    def __call__(self, prompt, timeout):
        self.prompts.append(prompt)
        rows = json.loads(prompt.split("CANDIDATES:\n", 1)[1])
        return json.dumps({"constraints": [self.make(r) for r in rows]})


class ConstraintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data, self.state = self.tmp / "data", self.tmp / "state"
        self._lanes, self._page = dict(K.LANES), K._page_text
        K._page_text = lambda url, limit=3000: "Comments must be received by November 15, 2099. The rule takes effect on February 1, 2100."

    def tearDown(self):
        K.LANES.clear()
        K.LANES.update(self._lanes)
        K._page_text = self._page

    def test_policy_filter_is_strict(self):
        self.assertTrue(is_ai_policy("Exclusion and Removal of DeepSeek Products From Federal Civilian Systems"))
        self.assertTrue(is_ai_policy("Export controls on advanced computing items"))
        self.assertFalse(is_ai_policy("Medicare Program; Implementation of Sections 6219"))
        self.assertFalse(is_ai_policy("Albania: trade notice"))

    def test_candidates_need_an_official_document(self):
        items = [
            policy_item("https://www.federalregister.gov/d/2099-1", "Export controls on model weights", num="2099-1"),
            social("https://x.com/a/status/1", "Commerce adds export controls on model weights",
                   links=["https://www.bis.gov/press-release/weights"]),
            social("https://x.com/b/status/2", "Senator says AI ban is coming"),  # no document: not a constraint
            social("https://x.com/c/status/3", "New open model beats GPT on coding", links=["https://github.com/acme/m"]),
        ]
        got = {c["_key"]: c for c in K.candidates(items)}
        self.assertIn("policy:fr:2099-1", got)
        self.assertTrue(any(c["_doc"] == "https://www.bis.gov/press-release/weights" for c in got.values()))
        self.assertEqual(len(got), 2)

    def test_dates_must_be_in_the_document(self):
        text = "Comments must be received on or before November 15, 2099."
        self.assertEqual(K.grounded_date("2099-11-15", text), "2099-11-15")
        self.assertIsNone(K.grounded_date("2099-12-01", text))
        self.assertIsNone(K.grounded_date("not a date", text))

    def test_generate_builds_events_register_and_drops_ungrounded(self):
        items = [policy_item("https://www.federalregister.gov/d/2099-1", "Registration before publishing open weights",
                             num="2099-1", comments_close_on="2099-11-15"),
                 policy_item("https://www.federalregister.gov/d/2099-2", "Agency AI procurement notice", num="2099-2")]

        def make(r):
            if "procurement" in r["title"]:
                return {"id": r["id"], "keep": True, "headline": "Agencies must drop 40 models", "harder": "Agencies lose 40 models.",
                        "status": "in_force", "layers": ["hosting"], "impact": 3}
            return {"id": r["id"], "keep": True, "headline": "Open weights need registration before release",
                    "harder": "Labs must register before publishing open weights.", "who": "open-weight labs",
                    "why": "Releases slow down.", "limits": "Draft only.", "status": "draft", "instrument": "rule",
                    "layers": ["publishing", "bogus"], "jurisdiction": "US", "issuer": "Commerce", "effective": "2100-02-01",
                    "comment_deadline": "2099-12-31", "impact": 4, "ripples": [{"topic": "models", "effect": "Slower open releases"}]}

        K.LANES.clear()
        K.LANES["claude"] = FakeLane(make)
        events, section = K.generate(items, DATE, self.data, self.state)
        self.assertEqual(len(events), 1)  # the procurement item named numbers its document does not contain
        ev = events[0]
        self.assertEqual(ev["kind"], "constraint")
        self.assertEqual(ev["evidence"], "official")
        self.assertEqual(ev["constraint"]["layers"], ["publishing"])
        self.assertEqual(ev["constraint"]["comment_deadline"], "2099-11-15")  # the issuer's own date wins
        self.assertEqual(ev["constraint"]["effective"], "2100-02-01")  # stated in the document text
        self.assertEqual(section["dropped"][0]["why"], "numbers not in its document: 40")
        self.assertTrue(section["items"][0]["new"])

        K.update_register(section, DATE, self.data)
        reg = json.loads((self.data / "constraints.json").read_text())
        self.assertEqual(reg["schema"], "magpie.constraints/1")
        self.assertEqual(len(reg["items"]), 1)

        # Next day: nothing new is collected, but the open draft stays on the page and no model is called again.
        K.LANES["claude"] = FakeLane(lambda r: self.fail("should not classify again"))
        events2, section2 = K.generate([], "2099-01-11", self.data, self.state)
        self.assertEqual(events2, [])
        self.assertEqual(len(section2["items"]), 1)
        self.assertFalse(section2["items"][0]["new"])

    def test_open_window(self):
        self.assertTrue(K.is_open({"comment_deadline": "2099-02-01", "first_seen": "2098-01-01"}, DATE))
        self.assertTrue(K.is_open({"first_seen": "2099-01-05"}, DATE))
        self.assertFalse(K.is_open({"first_seen": "2098-12-01"}, DATE))
        self.assertFalse(K.is_open({"status": "withdrawn", "comment_deadline": "2099-02-01"}, DATE))

    def test_constraints_lead_only_when_they_cut_access_today(self):
        caps = build([Item(source_id="github-trending-daily", source_kind="repo", source_name="gh",
                           url="https://github.com/acme/agentkit", title="acme/agentkit agent harness", summary="agent harness",
                           published_at="2099-01-01T00:00:00Z", metrics={"stars_day": 900}).finalize().to_dict()],
                     {"keys": {}}, "2099-01-01")["events"]
        rec = {"id": "c1", "key": "policy:fr:1", "headline": "h", "title": "t", "harder": "x", "who": "", "why": "", "limits": "",
               "status": "in_force", "instrument": "order", "layers": ["access"], "jurisdiction": "US", "issuer": "DHS",
               "effective": None, "comment_deadline": None, "access_now": False, "impact": 5, "ripples": [], "hats": {},
               "url": "https://www.federalregister.gov/d/1", "doc_url": "https://www.federalregister.gov/d/1", "primary": True,
               "sources": [{"name": "Federal Register", "url": "https://www.federalregister.gov/d/1", "title": "t"}],
               "first_seen": "2099-01-01", "last_seen": "2099-01-01"}
        rec["signal"], rec["parts"] = K._signal(rec, "2099-01-01")
        quiet = K._event(rec, "2099-01-01")
        self.assertNotIn("c1", rank_and_select(caps + [quiet]))
        loud = K._event(dict(rec, id="c2", access_now=True), "2099-01-01")
        self.assertIn("c2", rank_and_select(caps + [loud]))

    def test_build_ignores_policy_items(self):
        events = build([policy_item("https://www.federalregister.gov/d/9", "Artificial intelligence export rule", num="9")],
                       {"keys": {}}, DATE)["events"]
        self.assertEqual(events, [])


class TrackerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def _epoch(self, rows):
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=["Model", "Display name", "eci", "date", "Organization", "Country (of organization)",
                                            "Model accessibility", "Accessibility group"])
        w.writeheader()
        for model, eci, date, group, country in rows:
            w.writerow({"Model": model, "Display name": model, "eci": eci, "date": date, "Organization": "Org",
                        "Country (of organization)": country, "Model accessibility": "", "Accessibility group": group})
        z = self.tmp / "state" / "epoch" / "benchmark_data.zip"
        z.parent.mkdir(parents=True)
        with zipfile.ZipFile(z, "w") as f:
            f.writestr("epoch_capabilities_index/eci_scores.csv", buf.getvalue())

    def test_gap_measures_from_the_first_closed_model_at_that_level(self):
        self._epoch([("Closed A", 140, "2025-01-01", "Closed weights", "United States of America"),
                     ("Closed B", 150, "2025-06-01", "Closed weights", "United States of America"),
                     ("Open X", 138, "2025-05-01", "Open weights", "China"),
                     ("Open Y", 151, "2025-10-01", "Open weights", "China"),
                     ("Future", 199, "2100-01-01", "Closed weights", "United States of America")])
        g = T.gap("2026-01-01", self.tmp / "state")
        self.assertEqual(g["open"]["model"], "Open Y")
        self.assertEqual(g["closed_match"], None)  # Open Y beats every closed model released by then
        self.assertEqual(g["lag_months"], 0.0)
        hist = {h["model"]: h["lag_months"] for h in g["history"]}
        self.assertEqual(hist["Open X"], round(120 / 30.44, 1))  # Closed A reached 140 in January, 120 days earlier

    def test_hardware_prices_change_and_ignore_bad_rows(self):
        data = self.tmp / "data"
        data.mkdir()
        (data / "hardware.json").write_text(json.dumps({"observations": [
            {"device": "dgx-spark", "date": "2026-11-01", "kind": "list", "price": 7645, "url": "https://example.com/spark"},
            {"device": "dgx-spark", "date": "2026-11-02", "kind": "street", "price": 9000, "url": "http://insecure.example"},
            {"device": "not-a-device", "date": "2026-11-02", "kind": "list", "price": 1, "url": "https://example.com"},
        ]}))
        hw = T.hardware("2026-11-05", data)
        spark = next(r for r in hw["devices"] if r["id"] == "dgx-spark")
        self.assertEqual(spark["list"]["price"], 7645)
        self.assertEqual(spark["list_change_pct"], 10.0)
        self.assertIsNone(spark["street"])
        self.assertEqual(spark["ceiling_tps"], round(273 / 16))
        self.assertIn("DGX Spark lists for 10% more", hw["finding"])


if __name__ == "__main__":
    unittest.main()
