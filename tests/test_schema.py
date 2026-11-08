"""The ideas index schema (magpie.ideas/1): the published file and past ones conform, the checker finds what is wrong,
and the pipeline publishes only an index that conforms, keeping the last good one otherwise."""
import contextlib
import copy
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from magpie import schema as S
from magpie import views as V
from magpie.util import write_json

ROOT = Path(__file__).resolve().parent.parent
IDEAS = ROOT / "data/v1/ideas/index.json"
PAST = sorted((ROOT / "tests/data/ideas").glob("index-*.json"))  # saved from the published history


def git_history(limit: int = 8) -> list[tuple[str, dict]]:
    """Up to `limit` earlier published versions of the ideas index, spread over its history, when git has them."""
    try:
        revs = subprocess.run(["git", "rev-list", "HEAD", "--", "data/v1/ideas/index.json"], cwd=ROOT, capture_output=True,
                              text=True, timeout=30, check=True).stdout.split()
    except (OSError, subprocess.SubprocessError):
        return []
    picked = revs[:: max(1, len(revs) // limit)][:limit]
    out = []
    for r in picked:
        shown = subprocess.run(["git", "show", f"{r}:data/v1/ideas/index.json"], cwd=ROOT, capture_output=True, text=True, timeout=30)
        if shown.returncode == 0 and shown.stdout.strip():
            out.append((r[:8], json.loads(shown.stdout)))
    return out


def broken(index: dict) -> dict[str, dict]:
    """One copy of the index per way it can be wrong."""
    cases = {}

    def case(name, change):
        x = copy.deepcopy(index)
        change(x)
        cases[name] = x

    case("wrong schema", lambda x: x.update(schema="magpie.ideas/2"))
    case("no ideas", lambda x: x.pop("ideas"))
    case("no position", lambda x: x["ideas"][0].pop("position"))
    case("position from 0", lambda x: x["ideas"][0].update(position=0))
    case("position as a flag", lambda x: x["ideas"][0].update(position=True))
    case("unsafe id", lambda x: x["ideas"][0].update(id="../../etc"))
    case("not a calendar date", lambda x: x["ideas"][0].update(date="2026-02-30"))
    case("no ingredients", lambda x: x["ideas"][0].update(ingredients=[]))
    case("ingredient without a url key", lambda x: x["ideas"][0]["ingredients"][0].pop("url"))
    case("ingredient id not an event id", lambda x: x["ideas"][0]["ingredients"][0].update(id="Thing"))
    case("ingredient url not an iri", lambda x: x["ideas"][0]["ingredients"][0].update(url="not a link"))
    case("ingredient url with a bad escape", lambda x: x["ideas"][0]["ingredients"][0].update(url="https://example.com/%ZZ"))
    case("builds_on without a date", lambda x: x["ideas"][0].update(builds_on={"id": None, "title": "Earlier"}))
    case("built_on_by names an unsafe id", lambda x: x["ideas"][0].update(built_on_by=["Not An Id"]))
    case("score as text", lambda x: x["ideas"][0].update(score="high"))
    return cases


class PublishedFiles(unittest.TestCase):
    def test_the_published_index_conforms(self):
        index = json.loads(IDEAS.read_text())
        self.assertEqual(index["schema"], "magpie.ideas/1")
        self.assertEqual(S.problems(index, "ideas.schema.json"), [])

    def test_past_indexes_conform(self):
        self.assertGreaterEqual(len(PAST), 3)
        for path in PAST:
            with self.subTest(path.name):
                self.assertEqual(S.problems(json.loads(path.read_text()), "ideas.schema.json"), [])

    def test_a_sample_of_the_published_history_conforms(self):
        history = git_history()
        if not history:
            self.skipTest("no git history of data/v1/ideas/index.json here")
        for rev, index in history:
            with self.subTest(rev):
                self.assertEqual(S.problems(index, "ideas.schema.json"), [])

    def test_each_id_starts_with_its_date(self):
        # Not expressible in JSON Schema, so checked here: the page id carries its edition's date.
        for path in [IDEAS, *PAST]:
            for x in json.loads(path.read_text())["ideas"]:
                self.assertTrue(x["id"].startswith(x["date"] + "-"), x["id"])

    def test_the_schema_is_in_the_house_style(self):
        raw = (ROOT / "data/v1/schema/ideas.schema.json").read_text()
        s = json.loads(raw)
        self.assertEqual(json.dumps(s, indent=1) + "\n", raw)  # the way every schema here is written
        self.assertEqual(s["$id"], "https://www.cameronsutcliff.com/magpie-data/schema/ideas.schema.json")
        self.assertIn("(magpie.ideas/1)", s["title"])
        self.assertEqual(s["properties"]["schema"], {"const": "magpie.ideas/1"})


class Checker(unittest.TestCase):
    def setUp(self):
        self.index = json.loads(IDEAS.read_text())

    def test_it_finds_each_way_the_index_can_be_wrong(self):
        for name, x in broken(self.index).items():
            with self.subTest(name):
                self.assertTrue(S.problems(x, "ideas.schema.json"), name)

    def test_a_problem_says_where_it_is(self):
        x = copy.deepcopy(self.index)
        x["ideas"][2]["ingredients"][1]["url"] = "not a link"
        self.assertEqual(S.problems(x, "ideas.schema.json"), ['$.ideas[2].ingredients[1].url: "not a link" is not a valid iri'])

    def test_a_retired_id_needs_only_its_id_date_and_title(self):
        x = copy.deepcopy(self.index)
        x["ideas"].append({"id": "2026-10-01-an-old-idea", "date": "2026-10-01", "title": "An old idea", "retired": True})
        x["ideas"][0]["builds_on"] = {"id": None, "date": "2026-10-01", "title": "Not found"}
        self.assertEqual(S.problems(x, "ideas.schema.json"), [])
        x["ideas"][-1]["retired"] = False
        self.assertTrue(S.problems(x, "ideas.schema.json"))

    def test_fields_may_be_added(self):
        x = copy.deepcopy(self.index)
        x["ideas"][0]["page_url"] = "https://www.cameronsutcliff.com/magpie/i/" + x["ideas"][0]["id"]
        x["note"] = "a later field"
        self.assertEqual(S.problems(x, "ideas.schema.json"), [])

    def test_a_keyword_it_cannot_check_is_an_error_not_a_pass(self):
        with self.assertRaises(S.SchemaError):
            S.problems({}, {"type": "object", "dependentRequired": {"a": ["b"]}})
        # Anywhere in the schema, whether or not this file reaches it.
        with self.assertRaises(S.SchemaError):
            S.problems({}, {"properties": {"x": {"dependentRequired": {"a": ["b"]}}}})
        with self.assertRaises(S.SchemaError):
            S.problems(1, {"anyOf": [True, {"dependentRequired": {}}]})
        with self.assertRaises(S.SchemaError):
            S.problems({}, {"$defs": {"unused": {"type": "strng"}}})
        with self.assertRaises(S.SchemaError):
            S.problems({}, {"else": {"$ref": "#/$defs/missing"}})
        # Behind a reference, wherever its target sits, even after an earlier branch already matched.
        with self.assertRaises(S.SchemaError):
            S.problems(None, {"default": {"unsupported": True}, "anyOf": [True, {"$ref": "#/default"}]})
        # A keyword value of the wrong shape is an error in the schema, not a crash later.
        for bad in [{"properties": []}, {"$ref": 5}, {"required": "id"}, {"pattern": "("}, {"minItems": True}, {"minItems": -1},
                    {"maxLength": -2}, {"allOf": []}, {"anyOf": []}, {"type": []}, {"type": ["string", "string"]}, {"required": ["a", "a"]},
                    {"$ref": "#/%FF"}, {"type": [{}]}, {"minLength": 1.5}, {"$ref": "#$defs"},
                    {"default": {"%ZZ": True}, "$ref": "#/default/%ZZ"}, {"default": {"a~2": True}, "$ref": "#/default/a~2"},
                    {"$defs": {"a b": True}, "$ref": "#/$defs/a b"}, {"default": [True] * 13, "$ref": "#/default/1%D9%A2"},
                    {"default": [True, True], "$ref": "#/default/01"}, {"type": {}},
                    {"$defs": {"n": {"$id": "other.json", "type": "number"}}}, {"$schema": "http://json-schema.org/draft-07/schema#"},
                    {"properties": {"x": {"$schema": "https://json-schema.org/draft/2020-12/schema"}}}, {"title": {"not": "a string"}},
                    {"examples": "x"}]:
            with self.assertRaises(S.SchemaError, msg=bad):
                S.problems({}, bad)

    def test_a_reference_can_point_into_a_list(self):
        schema = {"allOf": [{"type": "string"}], "anyOf": [{"$ref": "#/allOf/0"}]}
        self.assertEqual(S.problems("x", schema), [])
        self.assertTrue(S.problems(1, schema))
        with self.assertRaises(S.SchemaError):
            S.problems("x", {"allOf": [True], "anyOf": [{"$ref": "#/allOf/1"}]})
        # A pointer in a URI fragment is percent-decoded, then ~1 and ~0 (RFC 6901).
        self.assertEqual(S.problems("x", {"$defs": {"a b": {"type": "string"}, "c/d": True}, "allOf": [{"$ref": "#/$defs/a%20b"}, {"$ref": "#/$defs/c~1d"}]}), [])
        self.assertEqual(S.problems("x", {"$defs": {"s": {"type": "string"}}, "$ref": "#%2F$defs%2Fs"}), [])  # the same pointer, escaped
        # An integer bound may be written 1.0: JSON has no separate integer type.
        self.assertEqual(S.problems([], {"minItems": 1.0}), ["$: fewer than 1.0 items"])
        with self.assertRaises(S.SchemaError):
            S.problems("x", {"type": "string", "format": "email"})
        with self.assertRaises(S.SchemaError):
            S.problems({}, {"$ref": "https://example.com/other.json"})

    def test_uri_and_iri_follow_their_rfcs(self):
        ok = lambda v, f: not S.problems(v, {"format": f})  # noqa: E731
        for v in ["https://example.com/a%20b?q=1#top", "https://h:80/x", "mailto:a@b.c", "https://[::1]/x", "https://[::1]:8080/x",
                  "https://[v1.fe]/x", "https://[V1.fe]/x", "https://user:pw@h.com/x", "urn:isbn:0451450523", "https://ex.com:/x"]:
            self.assertTrue(ok(v, "uri") and ok(v, "iri"), v)
        for v in ["https://example.com/%ZZ", "http://", "https://a b.com", "not a link", "https://ex.com/#a#b", "https://ex.com/a\"b",
                  "https://[garbage]/x", "https://example.com/a[b]", "https://ex.com/a|b", "https:///x", "https://h:8a/x", "https://[::1]x/", ""]:
            self.assertFalse(ok(v, "uri") or ok(v, "iri"), v)
        # RFC 3987: private use only in a query, and never a noncharacter.
        self.assertFalse(ok("https://ex.com/\ue000", "iri"))
        self.assertTrue(ok("https://ex.com/?q=\ue000", "iri"))
        self.assertFalse(ok("https://ex.com/\U0001fffe", "iri"))
        self.assertFalse(ok("https://ex.com/\ufdd0", "iri"))
        curly = "https://www.wsj.com/finance/x-ca965d8b\u2019"  # as one published event's link ends
        self.assertTrue(ok(curly, "iri"))
        self.assertFalse(ok(curly, "uri"))

    def test_date_time_follows_rfc_3339(self):
        ok = lambda v: not S.problems(v, {"format": "date-time"})  # noqa: E731
        for v in ["2026-11-07T12:00:00Z", "2026-11-07t12:00:00.123+05:30", "2016-12-31T23:59:60Z", "2016-12-31T18:59:60-05:00",
                  "2017-01-01T00:59:60+01:00", "1972-06-30T23:59:60Z"]:
            self.assertTrue(ok(v), v)
        for v in ["2026-11-07T12:00:00+00:60", "2016-12-31T23:58:60Z", "2026-11-07 12:00:00Z", "2026-02-30T00:00:00Z", "2026-11-07T24:00:00Z",
                  "2026-11-07T12:00:00", "2016-12-30T23:59:60Z", "2026-11-07T23:59:60Z",
                  "\u0662\u0660\u0662\u0666-\u0661\u0661-\u0660\u0667T\u0661\u0662:\u0660\u0660:\u0660\u0660Z"]:
            self.assertFalse(ok(v), v)  # a leap second only on a day that had one; ASCII digits only
        for v in ["0001-01-01T00:00:60+23:59", "9999-12-31T23:59:60-23:59"]:  # the ends of the calendar: refused, never raised
            self.assertFalse(ok(v), v)
        self.assertTrue(S.problems("\u0662\u0660\u0662\u0666-\u0661\u0661-\u0660\u0667", {"format": "date"}))

    def test_every_link_ever_published_passes(self):
        # The ingredient url is an iri, so a link copied with a non-ASCII character never freezes the index.
        for path in [IDEAS, *PAST]:
            for x in json.loads(path.read_text())["ideas"]:
                for i in x.get("ingredients", []):
                    self.assertEqual(S.problems(i["url"], {"format": "iri"}), [], i["url"])

    def test_a_pattern_reads_digits_as_json_schema_does(self):
        # ECMA-262: \d is 0 to 9 only, so an id with another script's digits fails, as pages.py refuses to stamp it.
        self.assertTrue(S.problems("\u0661", {"pattern": "^\\d$"}))
        self.assertEqual(S.problems("1", {"pattern": "^\\d$"}), [])
        # Unicode mode, as JSON Schema asks: a class matches a code point, astral ones included; \- is fine inside a class.
        self.assertEqual(S.problems("\U0001F600", {"pattern": "^[^a]$"}), [])
        self.assertEqual(S.problems("a-b", {"pattern": "^a[\\-]b$"}), [])
        self.assertEqual(S.problems("-", {"pattern": "^[\\d-]$"}), [])  # a - at the end of a class is a literal in both
        self.assertEqual(S.problems("]", {"pattern": "^[\\]]$"}), [])
        for ch in "-a5":  # a range, then a literal -, then \d: valid in both, read alike
            self.assertEqual(S.problems(ch, {"pattern": "^[a-b-\\d]$"}), [], ch)
        # Lazy quantifiers read the same in both.
        self.assertEqual(S.problems("aa", {"pattern": "^a+?a$"}), [])
        self.assertEqual(S.problems("aa", {"pattern": "^a{1,2}?a?$"}), [])
        # $ is the end of the string, as in ECMA-262 (Python's $ would also match before a final newline).
        self.assertTrue(S.problems("a\n", {"pattern": "^a$"}))
        # Syntax the two engines read differently is refused, never read one way or the other.
        for pat in ["^\\s$", "^.$", "(?<=a)b", "\\bword", "(?P<n>x)", "\\p{L}", "a{", "[]", "[a", "a*+", "a++", "a?+", "a{1,2}+", "a*?+", "a**",
                    "a\\-b", "]", "a]", "[\\d-a]", "[a-\\w]"]:
            with self.assertRaises(S.SchemaError, msg=pat):
                S.problems("x", {"pattern": pat})
        idea = json.loads(IDEAS.read_text())["ideas"][0]
        x = {"schema": "magpie.ideas/1", "updated": None, "ideas": [{**idea, "id": "\u0662\u0660\u0662\u0666" + idea["id"][4:]}]}
        self.assertTrue(S.problems(x, "ideas.schema.json"))

    def test_nan_and_infinity_are_not_numbers(self):
        for v in [float("nan"), float("inf"), json.loads("1e400")]:
            self.assertTrue(S.problems(v, {"type": ["number", "null"]}), v)
            self.assertTrue(S.problems(v, {"type": "integer"}), v)
        x = json.loads(IDEAS.read_text())
        x["ideas"][0]["score"] = float("nan")
        self.assertTrue(S.problems(x, "ideas.schema.json"))

    def test_json_equality(self):
        self.assertEqual(S.problems(1.0, {"const": 1}), [])
        self.assertTrue(S.problems(True, {"const": 1}))
        self.assertTrue(S.problems(1, {"enum": [True, "1"]}))
        self.assertEqual(S.problems(3.0, {"type": "integer"}), [])

    def test_it_agrees_with_the_reference_implementation(self):
        try:
            from jsonschema import Draft202012Validator, FormatChecker
        except ImportError:
            self.skipTest("jsonschema is not installed")
        schema = S.load("ideas.schema.json")
        Draft202012Validator.check_schema(schema)
        ref = Draft202012Validator(schema, format_checker=FormatChecker())
        self.assertEqual(list(ref.iter_errors(self.index)), [])
        for path in PAST:
            self.assertEqual(list(ref.iter_errors(json.loads(path.read_text()))), [], path.name)
        uri_checked = not ref.is_valid({"schema": "magpie.ideas/1", "updated": None, "ideas": [
            {**self.index["ideas"][0], "ingredients": [{**self.index["ideas"][0]["ingredients"][0], "url": "not a link"}]}]})
        for name, x in broken(self.index).items():
            if name.startswith("ingredient url") and not uri_checked:
                continue  # the reference checks iri only with an optional package installed
            with self.subTest(name):
                self.assertFalse(ref.is_valid(x), name)


class Gate(unittest.TestCase):
    """views.refresh writes the ideas index only when it conforms."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = self.tmp / "v1"
        day = {"date": "2099-01-05", "events": [], "top": [], "coverage": {"events": 1}, "possibilities": {"items": [
            {"title": "Desks backtest overnight", "connection": "Because.", "rank": 9.5, "effort": "a week",
             "ingredient_refs": [{"id": "00000000000d", "title": "New tool", "date": "2099-01-05", "url": "https://example.com/d"}]}]}}
        write_json(self.data / "days" / "2099-01-05.json", day, compact=True)
        self.last_good = {"schema": "magpie.ideas/1", "updated": "2099-01-04", "ideas": []}
        write_json(self.data / "ideas" / "index.json", self.last_good, compact=True)

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def published(self) -> dict:
        return json.loads((self.data / "ideas" / "index.json").read_text())

    def test_a_conforming_index_is_published(self):
        self.assertEqual(V.refresh(self.data), {"ideas": 1, "rollups": 1})
        self.assertEqual(self.published()["updated"], "2099-01-05")

    def test_a_failing_index_is_reported_and_the_last_good_one_stays(self):
        real = V.ideas_index

        def bad(*a, **kw):
            index = real(*a, **kw)
            index["ideas"][0]["ingredients"][0]["url"] = "not a link"
            return index

        err = io.StringIO()
        with mock.patch.object(V, "ideas_index", bad), contextlib.redirect_stderr(err):
            got = V.refresh(self.data)
        self.assertEqual(got, {"ideas": "not published: failed its schema", "rollups": 1})  # the rollup still goes out
        self.assertEqual(self.published(), self.last_good)
        self.assertIn("ideas/index.json not published: it fails ideas.schema.json (1 problem)", err.getvalue())
        self.assertIn('$.ideas[0].ingredients[0].url: "not a link" is not a valid iri', err.getvalue())

    def test_a_raw_source_link_is_made_a_valid_iri_so_it_never_freezes_the_index(self):
        day = json.loads((self.data / "days" / "2099-01-05.json").read_text())
        refs = day["possibilities"]["items"][0]["ingredient_refs"]
        refs += [{"id": "00000000000e", "title": "Raw", "date": "2099-01-05", "url": "https://ex.com/a|b?x={1}&p=50%"},
                 {"id": "00000000000f", "title": "No link", "date": "2099-01-05", "url": "not a link"}]
        write_json(self.data / "days" / "2099-01-05.json", day, compact=True)
        self.assertEqual(V.refresh(self.data), {"ideas": 1, "rollups": 1})
        urls = [i["url"] for i in self.published()["ideas"][0]["ingredients"]]
        self.assertEqual(urls, ["https://example.com/d", "https://ex.com/a%7Cb?x=%7B1%7D&p=50%25", None])
        self.assertEqual(V._link("https://www.wsj.com/x-ca965d8b\u2019"), "https://www.wsj.com/x-ca965d8b\u2019")  # already an IRI: kept

    def test_a_malformed_schema_fails_the_check_and_never_raises(self):
        err = io.StringIO()
        with mock.patch.object(S, "load", return_value={"properties": []}), contextlib.redirect_stderr(err):
            self.assertFalse(S.publishable({}, "ideas.schema.json", "ideas/index.json"))
        self.assertIn("the schema could not be used", err.getvalue())

    def test_on_github_actions_the_report_is_also_a_warning(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertFalse(S.publishable({"schema": "magpie.ideas/1"}, "ideas.schema.json", "ideas/index.json"))
        self.assertTrue(out.getvalue().startswith("::warning title=Magpie schema check::ideas/index.json not published"))

    def test_an_unusable_schema_fails_the_check(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertFalse(S.publishable({}, "no-such.schema.json", "ideas/index.json"))
        self.assertIn("the schema could not be used", err.getvalue())


if __name__ == "__main__":
    unittest.main()
