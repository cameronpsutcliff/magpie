import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path

from magpie import discover
from magpie.items import Item


def item(source_id, kind, url, title, body="", trust=0.7, author=None, links=None):
    it = Item(source_id=source_id, source_kind=kind, source_name=source_id, url=url, title=title, summary=title,
              published_at="2099-01-01T00:00:00Z", trust=trust, author=author, extra={"links": links} if links else {})
    if source_id in ("x-grok-pulse", "x-grok-bot"):
        it.source_name = f"X: {author}"
    return it.finalize(body or " ".join(links or [])).to_dict()


def event(key, sources, hype=False, mentions=()):
    return {"id": key, "key": key, "kind": "repo", "title": key, "url": "https://example.com", "evidence": "reported",
            "sources": [{"name": n, "url": "https://example.com"} for n in sources],
            "mentions": [{"name": n, "url": "https://example.com"} for n in mentions],
            **({"brief": {"hype": True, "headline": key}} if hype else {})}


SOURCES = {"version": 1, "feeds": [
    {"id": "trusted-letter", "name": "Trusted Letter", "kind": "newsletter", "url": "https://trusted.example/feed", "trust": 0.75},
    {"id": "quiet-letter", "name": "Quiet Letter", "kind": "newsletter", "url": "https://quiet.example/feed", "trust": 0.7},
    {"id": "weak-letter", "name": "Weak Letter", "kind": "newsletter", "url": "https://weak.example/feed", "trust": 0.5},
    {"id": "lab-news", "name": "Lab news", "kind": "news", "vendor": "Lab", "url": "https://lab.example/rss.xml", "trust": 0.75},
], "apis": []}
YOUTUBE = {"channels": [
    {"channel_id": "UCwatchedwatchedwatched1", "handle": "@Watched", "name": "Watched", "priority": "high"},
], "dropped": [{"channel_id": "UCdroppeddroppeddropped1", "name": "Dropped"}]}
ROSTER = {"accounts": [
    {"handle": "@vetted", "signal_to_noise": 5},
    {"handle": "@noisy", "signal_to_noise": 4},
    {"handle": "@lowsnr", "signal_to_noise": 3},
], "avoid": [{"handle": "@shouty"}]}


class Repo:
    """A throwaway Magpie root with config, run items and published editions."""

    def __init__(self, end="2099-03-01"):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.end = end
        (self.root / "config").mkdir()
        self.write("config/sources.json", SOURCES)
        self.write("config/x-accounts.json", ROSTER)
        (self.root / "config/youtube.json").write_text(json.dumps(YOUTUBE, indent=2) + "\n")

    def write(self, rel, obj):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(obj))

    def read(self, rel):
        return json.loads((self.root / rel).read_text())

    def day(self, back=0):
        return (dt.date.fromisoformat(self.end) - dt.timedelta(days=back)).isoformat()

    def run_items(self, items, back=0):
        self.write(f"state/runs/{self.day(back)}/items.json", {"items": items, "rising": {}, "health": []})

    def editions(self, days, events_for=lambda d: [], skip=0, backfilled=False):
        for back in range(skip, skip + days):
            d = self.day(back)
            extra = {"run": {"where": "backfill"}, "edition": {"backfilled": True}} if backfilled else {}
            self.write(f"data/v1/days/{d}.json", {"date": d, "events": events_for(d), "top": [], **extra})

    def run(self, **kw):
        kw.setdefault("check_feed", lambda url: (True, "ok"))
        return discover.run(self.root, self.end, **kw)

    def close(self):
        self.tmp.cleanup()


class DiscoverTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo()

    def tearDown(self):
        self.repo.close()

    def test_references_find_sources_but_not_self(self):
        x = item("x-grok-pulse", "social", "https://x.com/vetted/status/1", "New agent kit from @newlab",
                 author="@vetted", links=["https://github.com/NewLab/kit", "https://x.com/i/status/2"])
        # X accounts are Magpie Scout's, so a post names a GitHub owner here but no X account.
        self.assertEqual({r["id"] for r in discover.references(x)}, {"github:newlab"})
        nl = item("trusted-letter", "newsletter", "https://trusted.example/p/issue-9", "Issue 9",
                  body='<a href="https://trusted.example/p/issue-8">last week</a> '
                       '<a href="https://www.youtube.com/@LabTalks">talk</a> <a href="https://deepdive.substack.com/p/x">post</a> '
                       '<a href="https://mail.dropbox.com/share">files</a> write to me@inbox.example')
        refs = {r["id"]: r for r in discover.references(nl)}
        self.assertEqual(set(refs), {"youtube:@labtalks", "newsletter:deepdive.substack.com"})
        self.assertEqual(refs["newsletter:deepdive.substack.com"]["feed_url"], "https://deepdive.substack.com/feed")

    def test_promotion_needs_two_independent_trusted_sources(self):
        r = self.repo
        r.run_items([
            # Two GitHub collectors are one source group, so trending twice is one citer.
            item("github-trending-daily", "repo", "https://github.com/solo/agent", "solo/agent agent"),
            item("github-trending-weekly", "repo", "https://github.com/solo/agent", "solo/agent agent"),
            # Trusted newsletter plus untrusted newsletter: still one trusted citer.
            item("trusted-letter", "newsletter", "https://trusted.example/p/a", "A", body="https://github.com/pair/llm"),
            item("weak-letter", "newsletter", "https://weak.example/p/a", "A", trust=0.5, body="https://github.com/pair/llm"),
            # Low signal-to-noise roster accounts do not vouch.
            item("x-grok-pulse", "social", "https://x.com/lowsnr/status/1", "LLM", author="@lowsnr",
                 links=["https://github.com/pair/llm"]),
            # A trusted newsletter and a vetted X account: promoted.
            item("trusted-letter", "newsletter", "https://trusted.example/p/b", "B", body="https://github.com/duo/model-kit"),
            item("x-grok-pulse", "social", "https://x.com/vetted/status/2", "Model kit", author="@vetted",
                 links=["https://github.com/duo/model-kit"]),
        ], back=2)
        out = r.run()
        self.assertEqual([e["id"] for e in out["result"]["promoted"]], ["github:duo"])
        watching = {e["id"] for e in out["result"]["watching"]}
        self.assertTrue({"github:solo", "github:pair"} <= watching)
        orgs = r.read("config/sources.json")["github_orgs"]
        self.assertEqual([o["org"] for o in orgs], ["duo"])
        self.assertEqual(orgs[0]["origin"], "discovery")

    def test_promotions_write_configs_candidates_and_log(self):
        r = self.repo
        roster_before = (r.root / "config/x-accounts.json").read_text()
        r.run_items([
            item("trusted-letter", "newsletter", "https://trusted.example/p/c", "C",
                 body="https://x.com/rising/status/1 and https://deepdive.substack.com/p/evals"),
            item("x-grok-pulse", "social", "https://x.com/vetted/status/3", "Evals writeup", author="@vetted",
                 links=["https://deepdive.substack.com/p/evals", "https://x.com/rising/status/1"]),
        ], back=1)
        out = r.run()
        self.assertEqual({e["id"] for e in out["result"]["promoted"]}, {"newsletter:deepdive.substack.com"})
        self.assertEqual((r.root / "config/x-accounts.json").read_text(), roster_before)
        feed = next(f for f in r.read("config/sources.json")["feeds"] if f.get("origin") == "discovery")
        self.assertEqual(feed["url"], "https://deepdive.substack.com/feed")
        self.assertLess(feed["trust"], discover.MIN_TRUST)
        cands = r.read("config/candidates.json")
        self.assertEqual({c["status"] for c in cands["candidates"]}, {"promoted"})
        log = (r.root / f"reports/discovery/{r.end}.md").read_text()
        self.assertIn("python -m magpie discover --veto newsletter:deepdive.substack.com", log)
        self.assertNotIn(chr(0x2014), log)

        # Rerunning the same week changes nothing and keeps the earlier log.
        again = r.run()
        self.assertEqual(again["result"]["promoted"], [])
        self.assertIn("newsletter:deepdive.substack.com", (r.root / f"reports/discovery/{r.end}.md").read_text())

    def test_discovered_sources_do_not_vouch(self):
        r = self.repo
        sources = r.read("config/sources.json")
        sources["feeds"].append({"id": "nl-found", "name": "found", "kind": "newsletter", "url": "https://found.example/feed",
                                 "trust": 0.9, "origin": "discovery"})
        r.write("config/sources.json", sources)
        r.run_items([
            item("nl-found", "newsletter", "https://found.example/p/1", "1", trust=0.9, body="https://github.com/next/tool"),
            item("trusted-letter", "newsletter", "https://trusted.example/p/1", "1", body="https://github.com/next/tool"),
        ])
        self.assertEqual(r.run(dry_run=True)["result"]["promoted"], [])

    def test_promoted_feed_ids_stay_unique(self):
        r = self.repo
        hosts = ["https://foo.example.com/p/a", "https://foo-example.com/p/b"]
        r.run_items([
            item("trusted-letter", "newsletter", "https://trusted.example/p/u", "U", body=" ".join(hosts)),
            item("x-grok-pulse", "social", "https://x.com/vetted/status/11", "U", author="@vetted", links=hosts),
        ])
        r.run()
        ids = [f["id"] for f in r.read("config/sources.json")["feeds"] if f.get("origin") == "discovery"]
        self.assertEqual(sorted(ids), ["nl-foo-example-com", "nl-foo-example-com-2"])

    def test_failed_feed_check_and_offline_hold_newsletters(self):
        r = self.repo
        r.run_items([
            item("trusted-letter", "newsletter", "https://trusted.example/p/d", "D", body="https://broken.substack.com/p/x"),
            item("x-grok-pulse", "social", "https://x.com/vetted/status/4", "D", author="@vetted",
                 links=["https://broken.substack.com/p/x"]),
        ])
        held = r.run(dry_run=True, check_feed=lambda url: (False, "HTTP 404"))["result"]["held"]
        self.assertEqual([h["reason"] for h in held], ["feed check failed: HTTP 404"])
        held = r.run(dry_run=True, check_feed=None)["result"]["held"]
        self.assertIn("offline", held[0]["reason"])

    def test_zero_kept_demotion_waits_for_21_days(self):
        r = self.repo
        kept = lambda d: [event("gh:a/b", ["Trusted Letter", "Weak Letter", "X: @vetted", "X: @noisy", "X: @lowsnr"])]
        r.editions(20, kept)
        out = r.run(dry_run=True)
        self.assertEqual(out["result"]["demoted"], [])
        self.assertIn("waiting for 21 live editions", out["log"])
        r.editions(21, kept)
        out = r.run()
        self.assertEqual([d["id"] for d in out["result"]["demoted"]], ["feed:quiet-letter"])
        sources = r.read("config/sources.json")
        self.assertNotIn("quiet-letter", [f["id"] for f in sources["feeds"]])
        self.assertEqual(sources["benched"][0]["benched"]["date"], r.end)

    def test_three_hype_flags_demote_but_vendor_feeds_and_x_accounts_stay(self):
        r = self.repo
        roster_before = (r.root / "config/x-accounts.json").read_text()
        r.editions(3, lambda d: [event(f"gh:hype/{d}", ["Quiet Letter", "Lab news", "X: @noisy"], hype=True),
                                 event("gh:calm/x", ["Trusted Letter"], hype=False)])
        out = r.run()
        self.assertEqual([d["id"] for d in out["result"]["demoted"]], ["feed:quiet-letter"])
        sources = r.read("config/sources.json")
        self.assertEqual([b["id"] for b in sources["benched"]], ["quiet-letter"])
        self.assertIn("lab-news", [f["id"] for f in sources["feeds"]])
        self.assertEqual((r.root / "config/x-accounts.json").read_text(), roster_before)

    def test_same_story_flagged_twice_counts_once(self):
        r = self.repo
        r.editions(3, lambda d: [event("gh:hype/same", ["Quiet Letter"], hype=True)])
        self.assertEqual(r.run(dry_run=True)["result"]["demoted"], [])

    def test_veto_undoes_and_is_remembered(self):
        r = self.repo
        r.run_items([
            item("trusted-letter", "newsletter", "https://trusted.example/p/e", "E", body="https://deepdive.substack.com/p/e"),
            item("x-grok-pulse", "social", "https://x.com/vetted/status/5", "E", author="@vetted",
                 links=["https://deepdive.substack.com/p/e"]),
        ])
        r.editions(3, lambda d: [event(f"gh:hype/{d}", ["Quiet Letter"], hype=True)])
        r.run()
        msgs = discover.veto(r.root, ["newsletter:deepdive.substack.com", "Feed:Quiet-Letter", "x:@vetted"])
        self.assertIn("promotion undone", msgs[0])
        self.assertIn("restored", msgs[1])
        self.assertIn("Magpie Scout", msgs[2])
        feeds = [f["id"] for f in r.read("config/sources.json")["feeds"]]
        self.assertIn("quiet-letter", feeds)
        self.assertFalse(any(f.startswith("nl-") for f in feeds))
        vetoes = r.read("config/vetoes.json")
        self.assertEqual((vetoes["never_promote"], vetoes["never_demote"]),
                         (["newsletter:deepdive.substack.com"], ["feed:quiet-letter"]))
        out = r.run(dry_run=True)
        self.assertEqual((out["result"]["promoted"], out["result"]["demoted"]), ([], []))
        self.assertEqual(out["result"]["skipped"], ["newsletter:deepdive.substack.com"])

    def test_x_accounts_are_never_candidates(self):
        r = self.repo
        r.run_items([
            item("x-grok-pulse", "social", "https://x.com/shouty/status/1", "World first", author="@shouty"),
            item("trusted-letter", "newsletter", "https://trusted.example/p/f", "F", body="https://x.com/shouty/status/1"),
            item("x-grok-bot", "social", "https://x.com/vetted/status/6", "F", author="@vetted",
                 links=["https://x.com/shouty/status/1"]),
        ])
        out = r.run(dry_run=True)
        ids = {e["id"] for k in ("promoted", "held", "watching") for e in out["result"][k]}
        self.assertFalse(any(i.startswith("x:") for i in ids))

    def test_weekly_cap(self):
        r = self.repo
        r.run_items([item("trusted-letter", "newsletter", f"https://trusted.example/p/{i}", "G", body=f"https://github.com/org{i}/ai")
                     for i in range(8)] +
                    [item("x-grok-pulse", "social", f"https://x.com/vetted/status/{i}", "G", author="@vetted",
                          links=[f"https://github.com/org{i}/ai"]) for i in range(8)])
        out = r.run(dry_run=True)
        self.assertEqual(len(out["result"]["promoted"]), discover.CAPS["github"])
        self.assertEqual(len(out["result"]["held"]), 8 - discover.CAPS["github"])

    def test_backfilled_editions_do_not_count_toward_zero_kept(self):
        r = self.repo
        kept = lambda d: [event("gh:a/b", ["Trusted Letter", "Weak Letter", "X: @vetted", "X: @noisy", "X: @lowsnr"])]
        r.editions(5, kept)
        r.editions(30, skip=5, backfilled=True)
        out = r.run(dry_run=True)
        self.assertEqual(out["result"]["demoted"], [])
        self.assertIn("30 backfilled editions do not count", out["log"])

    def test_zero_kept_needs_21_live_editions_not_21_days(self):
        r = self.repo
        kept = lambda d: [event("gh:a/b", ["Trusted Letter", "Weak Letter", "X: @vetted", "X: @noisy", "X: @lowsnr"])]
        # 21 days old, but a five-day backfilled gap leaves 16 live editions: still waiting.
        r.editions(10, kept)
        r.editions(5, skip=10, backfilled=True)
        r.editions(6, kept, skip=15)
        out = r.run(dry_run=True)
        self.assertEqual(out["result"]["demoted"], [])
        self.assertIn("16 so far", out["log"])
        # Five more live editions before the gap make 21: the window reaches back past it.
        r.editions(5, kept, skip=21)
        out = r.run(dry_run=True)
        self.assertEqual([d["id"] for d in out["result"]["demoted"]], ["feed:quiet-letter"])
        self.assertIn(f"live editions from {r.day(25)} to {r.end}", out["log"])

    def test_hype_flags_count_in_backfilled_editions(self):
        r = self.repo
        r.editions(3, lambda d: [event(f"gh:hype/{d}", ["Quiet Letter"], hype=True)], backfilled=True)
        self.assertEqual([d["id"] for d in r.run(dry_run=True)["result"]["demoted"]], ["feed:quiet-letter"])

    def test_grok_bot_posts_cite_like_the_pulse(self):
        r = self.repo
        r.run_items([
            # The same roster account through the pulse and Grok Bot is one citer.
            item("x-grok-pulse", "social", "https://x.com/vetted/status/7", "Kit", author="@vetted",
                 links=["https://github.com/one/kit"]),
            item("x-grok-bot", "social", "https://x.com/vetted/status/8", "Kit", author="@vetted",
                 links=["https://github.com/one/kit"]),
            # A Grok Bot post from a curated account vouches like a pulse post.
            item("x-grok-bot", "social", "https://x.com/noisy/status/9", "Two", author="@noisy",
                 links=["https://github.com/two/tool"]),
            item("trusted-letter", "newsletter", "https://trusted.example/p/h", "H", body="https://github.com/two/tool"),
        ])
        out = r.run(dry_run=True)
        self.assertEqual([e["id"] for e in out["result"]["promoted"]], ["github:two"])
        self.assertIn("github:one", {e["id"] for e in out["result"]["watching"]})

    def test_scout_source_leads_become_candidates(self):
        r = self.repo
        yt = "https://www.youtube.com/channel/UCleadleadleadleadlead12"
        r.write("inbox/x-roster.json", {"updated_at": f"{r.end}T09:30:00Z", "accounts": [
            {"handle": "@ontrial", "status": "trial", "signal_to_noise": 5}], "source_leads": [
            {"kind": "newsletter", "name": "Lead Letter", "url": "https://leadletter.substack.com", "seen_via": ["@vetted", "noisy"]},
            {"kind": "youtube", "name": "Lead Talks", "url": yt, "seen_via": ["@vetted", "@noisy"]},
            # A Scout addition does not vouch, and neither does a low signal-to-noise account.
            {"kind": "blog", "name": "Example blog", "url": "https://blog.example.org/", "seen_via": ["@vetted", "@ontrial"]},
            {"kind": "changelog", "name": "Tool", "url": "https://tool.example.net/changelog.xml", "seen_via": ["@vetted", "@lowsnr"]},
            {"kind": "blog", "url": "not a url", "seen_via": ["@vetted", "@noisy"]}, "junk",
            {"kind": "blog", "url": "https://odd.example.com/", "seen_via": 1},
            {"kind": "blog", "url": "https://odder.example.com/", "seen_via": "@vetted"},
            {"kind": "blog", "url": "https://oddest.example.com/", "seen_via": [None, 3, {"h": 1}, "@vetted"]}]})
        out = r.run()
        self.assertEqual({e["id"] for e in out["result"]["promoted"]},
                         {"newsletter:leadletter.substack.com", "youtube:channel/ucleadleadleadleadlead12"})
        watching = {e["id"]: e for e in out["result"]["watching"]}
        self.assertEqual(set(watching), {"newsletter:blog.example.org", "newsletter:tool.example.net",
                                         "newsletter:oddest.example.com"})
        self.assertEqual(watching["newsletter:tool.example.net"]["feed_url"], "https://tool.example.net/changelog.xml")
        self.assertEqual(watching["newsletter:blog.example.org"]["feed_url"], "https://blog.example.org/feed")
        self.assertEqual(r.read("config/youtube.json")["channels"][-1]["name"], "Lead Talks")

    def test_stale_or_broken_scout_files_add_nothing(self):
        r = self.repo
        lead = {"kind": "newsletter", "url": "https://leadletter.substack.com", "seen_via": ["@vetted", "@noisy"]}
        r.write("inbox/x-roster.json", {"updated_at": r.day(10), "source_leads": [lead]})
        self.assertEqual(r.run(dry_run=True)["result"]["promoted"], [])
        path = r.root / "inbox/x-roster.json"
        for broken in ("", "[]", '{"source_leads": null}', '{"updated_at": "nope", "source_leads": ["x", 3]}'):
            path.write_text(broken)
            self.assertEqual(discover.scout_leads(path)[1], [])
            self.assertEqual(r.run(dry_run=True)["result"]["promoted"], [])

    def test_doubted_x_posts_do_not_vouch(self):
        r = self.repo
        bot = item("x-grok-bot", "social", "https://x.com/vetted/status/13", "Claim", author="@vetted",
                   links=["https://github.com/claimed/model"])
        bot["extra"]["doubt"] = {"level": 2, "note": "Community Note: benchmark not reproduced", "url": None}
        r.run_items([bot, item("trusted-letter", "newsletter", "https://trusted.example/p/t", "T",
                               body="https://github.com/claimed/model")])
        out = r.run(dry_run=True)
        self.assertEqual(out["result"]["promoted"], [])
        self.assertIn("github:claimed", {e["id"] for e in out["result"]["watching"]})

    def test_youtube_promotes_into_youtube_json_by_channel_id(self):
        r = self.repo
        links = ["https://www.youtube.com/channel/UCnewnewnewnewnewnewnew1", "https://www.youtube.com/@Watched",
                 "https://www.youtube.com/channel/UCdroppeddroppeddropped1", "https://www.youtube.com/@HandleOnly"]
        r.run_items([
            item("trusted-letter", "newsletter", "https://trusted.example/p/v", "V", body=" ".join(links)),
            item("x-grok-pulse", "social", "https://x.com/vetted/status/10", "V", author="@vetted", links=links),
        ])
        out = r.run()
        self.assertEqual([e["id"] for e in out["result"]["promoted"]], ["youtube:channel/ucnewnewnewnewnewnewnew1"])
        self.assertEqual([e["id"] for e in out["result"]["held"]], ["youtube:@handleonly"])
        self.assertIn("channel id", out["result"]["held"][0]["reason"])
        self.assertEqual(out["result"]["skipped"], ["youtube:channel/ucdroppeddroppeddropped1"])
        raw = (r.root / "config/youtube.json").read_text()
        self.assertTrue(raw.endswith("}\n") and raw.startswith('{\n  "channels"'))
        added = json.loads(raw)["channels"][-1]
        self.assertEqual((added["channel_id"], added["priority"], added["origin"]),
                         ("UCnewnewnewnewnewnewnew1", "medium", "discovery"))
        msgs = discover.veto(r.root, ["youtube:channel/ucnewnewnewnewnewnewnew1"])
        self.assertIn("promotion undone", msgs[0])
        self.assertEqual([c["name"] for c in r.read("config/youtube.json")["channels"]], ["Watched"])

    def test_youtube_channels_are_independent_citers_and_medium_ones_do_not_vouch(self):
        r = self.repo
        r.run_items([
            item("youtube-channels", "video", "https://www.youtube.com/watch?v=a1", "A", author="Watched",
                 links=["https://github.com/vid/tool"]),
            item("youtube-channels", "video", "https://www.youtube.com/watch?v=a2", "B", author="Other", trust=0.55,
                 links=["https://github.com/vid/tool"]),
        ])
        out = r.run(dry_run=True)
        self.assertEqual(out["result"]["promoted"], [])
        self.assertIn("github:vid", {e["id"] for e in out["result"]["watching"]})


if __name__ == "__main__":
    unittest.main()
