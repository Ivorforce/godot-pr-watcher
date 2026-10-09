import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pathspec

from watcher.config import Config
from watcher.index import old_lines_from_patch
from watcher.match import diagnose, match, time_limit
from watcher.render import ZWSP, code_span, defuse, digest, preview
from watcher.rules import check_pattern, parse_rules
from watcher.state import State

EXCLUDE = pathspec.GitIgnoreSpec.from_lines(["/thirdparty/", "*.gen.*"])
CFG = Config(target="godotengine/godot", label="watch", exclude_paths=[], index_window_days=30,
             default_digest="daily", site_url="https://example.github.io/w", repo="example/w")


def rules(text):
    r, errors = parse_rules(f"### Rules\n\n```yaml\n{text}\n```\n")
    assert not errors, errors
    return r


def m(match, **rest):
    """Rules from a match section (YAML flow mapping body) plus top-level keys."""
    extra = "".join(f"\n{k}: {v}" for k, v in rest.items())
    return rules(f"match: {{{match}}}{extra}")


def texts(r, rec, login="me"):
    return [t for _, t in match(r, rec, login, EXCLUDE)]


def pr(**kw):
    rec = {"n": 1, "title": "t", "author": "someone", "draft": False, "labels": [], "state": "open",
           "files": ["core/variant/variant.cpp"], "old_lines": 10, "direct": {}, "transitive": {}}
    rec.update(kw)
    return rec


class PatchTest(unittest.TestCase):
    def test_removed_lines(self):
        patch = "@@ -10,4 +10,3 @@ ctx\n a\n-b\n-c\n+C\n d\n@@ -30,2 +29,2 @@\n x\n-y\n+Y"
        self.assertEqual(old_lines_from_patch(patch), [11, 12, 31])

    def test_pure_addition(self):
        self.assertEqual(old_lines_from_patch("@@ -0,0 +1,2 @@\n+a\n+b"), [])

    def test_no_newline_marker(self):
        self.assertEqual(old_lines_from_patch("@@ -1,2 +1,2 @@\n-a\n\\ No newline at end of file\n b"), [1])


class RulesTest(unittest.TestCase):
    def test_valid(self):
        r = rules("match:\n  paths: [modules/gdscript/]\n  my_lines: 20%\n"
                  "ignore:\n  authors: ['@Foo']\ninclude: [drafts]\ndigest: weekly")
        self.assertEqual(r.paths, ["modules/gdscript/"])
        self.assertEqual(r.ignore_authors, ["foo"])
        self.assertEqual((r.drafts, r.own, r.other_branches), (True, False, False))
        self.assertEqual(r.digest, "weekly")

    def test_my_lines_forms(self):
        self.assertEqual((m("my_lines: 20%").my_lines_share, m("my_lines: 20%").my_lines_count), (0.2, None))
        self.assertEqual(m("my_lines: 0.2").my_lines_share, 0.2)
        self.assertEqual((m("my_lines: 40").my_lines_share, m("my_lines: 40").my_lines_count), (None, 40))
        self.assertEqual(m("authors: ['@Bob']").authors, ["bob"])
        both = m("my_lines: [20%, 40]")
        self.assertEqual((both.my_lines_share, both.my_lines_count), (0.2, 40))

    def test_errors(self):
        for text in ["match: {paths: [a]}\nnope: 1", "match: {my_lines: 0}", "match: {my_lines: 150%}",
                     "match: {my_lines: -3}", "match: {my_lines: lots}", "match: {my_lines: [20%, 30%]}",
                     "match: {my_lines: []}", "match: {paths: 3}", "match: {paths: [a]}\ndigest: monthly",
                     "include: [drafts]", "match: {paths: [a]}\ninclude: [everything]", "- a\n- b",
                     "match: {paths: [unclosed", "match: {paths: ['!core/']}", "match: {paths: [a], nope: 1}",
                     "match: {my_lines: 20%}\nignore: {my_lines: 40}", "match: [a]", "paths: [a]"]:
            r, errors = parse_rules(f"```yaml\n{text}\n```")
            self.assertIsNone(r, text)
            self.assertTrue(errors, text)

    def test_flat_key_hint(self):
        self.assertIn("under `match:`", parse_rules("```yaml\npaths: [a]\n```")[1][0])

    def test_no_block(self):
        self.assertTrue(parse_rules("match: {paths: [a]}")[1])

    def test_slow_patterns_rejected(self):
        for p in ["a**a**a**b", "*a*/*a*/*a*/*a*", "x" * 300, "***"]:
            self.assertIsNotNone(check_pattern(p), p)
        for p in ["modules/gdscript/", "**/tests/**/*.cpp", "core/*.h", "**"]:
            self.assertIsNone(check_pattern(p), p)
        self.assertTrue(parse_rules("```yaml\nmatch: {paths: ['a**b']}\n```")[1])

    def test_unquoted_star_hint(self):
        errors = parse_rules("```yaml\nmatch:\n  paths:\n    - **/*.glsl\n```")[1]
        self.assertIn("need quotes", errors[0])

    def test_time_limit(self):
        spec = pathspec.GitIgnoreSpec.from_lines(["a**a**a**a**b"])  # bypasses validation on purpose
        start = time.monotonic()
        with self.assertRaises(TimeoutError), time_limit(0.2):
            spec.match_file("a" * 200)
        self.assertLess(time.monotonic() - start, 2)

    def test_errors_cant_ping(self):
        r, errors = parse_rules("```yaml\n'`@someone #1 x': 1\nmatch: {paths: [a]}\n```")
        body = preview(CFG, errors, [], {}, [], 1)
        self.assertNotIn("@someone", body)
        self.assertNotIn("#1", body)


class MatchTest(unittest.TestCase):
    def test_hostile_filename(self):
        r = m("paths: ['**']")
        name = "a` @someone #1 https://github.com/godotengine/godot/pull/1 `b"
        reason = texts(r, pr(files=[name]))[0]
        self.assertEqual(reason.count("`"), 4)  # pattern and file name, each one code span
        self.assertTrue(reason.endswith("`"))

    def test_paths(self):
        r = m("paths: [core/variant/]")
        self.assertEqual(texts(r, pr()), ["`core/variant/` → `core/variant/variant.cpp`"])
        self.assertEqual(texts(r, pr(files=["core/io/x.cpp"])), [])

    def test_paths_anchored_at_root(self):
        r = m("paths: [editor/]")
        self.assertTrue(texts(r, pr(files=["editor/x.cpp"])))
        self.assertEqual(texts(r, pr(files=["modules/gdscript/editor/x.cpp"])), [])
        anywhere = m("paths: ['**/editor/', '*.glsl']")
        self.assertTrue(texts(anywhere, pr(files=["modules/gdscript/editor/x.cpp"])))
        self.assertTrue(texts(anywhere, pr(files=["servers/rendering/a.glsl"])))

    def test_ignore_and_exclude(self):
        r = m("paths: ['**/*.cpp']", ignore="{paths: [core/variant/]}")
        self.assertEqual(texts(r, pr()), [])
        self.assertEqual(texts(r, pr(files=["thirdparty/x.cpp"])), [])

    def test_ignore_and_include(self):
        r = m("paths: [core/]", ignore="{labels: [documentation], milestones: ['3.7'], branches: [wip]}")
        self.assertEqual(texts(r, pr(draft=True)), [])
        self.assertEqual(texts(r, pr(default_branch=False)), [])
        self.assertEqual(texts(r, pr(author="me")), [])
        self.assertEqual(texts(r, pr(labels=["documentation"])), [])
        self.assertEqual(texts(r, pr(milestone="3.7")), [])
        self.assertEqual(texts(r, pr(base_ref="wip")), [])
        everything = m("paths: [core/]", include="[drafts, own, other_branches]")
        self.assertTrue(texts(everything, pr(draft=True, author="me", default_branch=False)))

    def test_my_lines(self):
        r = m("my_lines: 30%")
        self.assertEqual(texts(r, pr(direct={"me": 2})), [])
        self.assertEqual(texts(r, pr(direct={"me": 4})), ["your lines 4/10"])
        self.assertEqual(texts(r, pr(direct={"me": 2}, transitive={"me": 2})), ["lines you wrote or approved 4/10"])
        self.assertEqual(texts(r, pr(direct={"me": 9}, old_lines=0)), [])

    def test_my_lines_count(self):
        r = m("my_lines: 40")
        self.assertEqual(texts(r, pr(direct={"me": 39}, old_lines=2000)), [])
        self.assertEqual(texts(r, pr(direct={"me": 40}, old_lines=2000)), ["your lines 40/2000"])
        both = m("my_lines: [20%, 40]")
        self.assertTrue(texts(both, pr(direct={"me": 2}, old_lines=10)))      # share
        self.assertTrue(texts(both, pr(direct={"me": 40}, old_lines=2000)))   # count
        self.assertEqual(texts(both, pr(direct={"me": 39}, old_lines=2000)), [])

    def test_authors(self):
        r = m("authors: [someone]")
        self.assertEqual(texts(r, pr()), ["author someone"])
        self.assertEqual(texts(r, pr(author="other")), [])
        self.assertEqual(texts(r, pr(draft=True)), [])

    def test_labels_trigger(self):
        r = m("labels: ['topic:gdscript']")
        self.assertEqual(texts(r, pr()), [])
        self.assertEqual(texts(r, pr(labels=["bug", "topic:gdscript"])), ["label `topic:gdscript`"])

    def test_branches_and_milestones(self):
        old = pr(default_branch=False, base_ref="3.x", milestone="3.7", files=["core/a.cpp"])
        self.assertEqual(texts(m("branches: [3.x]"), old), ["branch `3.x`"])
        self.assertEqual(texts(m("milestones: ['3.7']"), old), ["milestone `3.7`"])
        self.assertEqual(texts(m("paths: [core/]"), old), [])  # other branches need opting in
        self.assertTrue(texts(m("paths: [core/]", include="[other_branches]"), old))
        self.assertEqual(texts(m("milestones: ['4.6']"), pr(milestone="4.5")), [])

    def test_diagnose(self):
        r = m("paths: [editor/, core/], labels: [bgu], authors: [ghost]")
        notes = diagnose(r, [pr(files=["core/a.cpp"], labels=["bug"])], "me")
        self.assertEqual(len(notes), 3)
        self.assertIn("editor/", notes[0])


class RenderTest(unittest.TestCase):
    def test_code_span(self):
        self.assertEqual(code_span("a`b\nc"), "`a'b c`")
        self.assertEqual(len(code_span("x" * 500)), 82)

    def test_done_prs_struck_through(self):
        from watcher.render import pr_line
        line = pr_line(CFG, pr(n=5, title="a ~~b", state="merged"), ["x"])
        self.assertTrue(line.startswith("- ~~[PR 5]"))
        self.assertIn("~~ (merged) — by", line)
        self.assertNotIn("~~b", line)
        self.assertTrue(pr_line(CFG, pr(n=5), ["x"]).startswith("- [PR 5]"))

    def test_defuse_gh_reference(self):
        self.assertNotIn("GH-12", defuse("see GH-12"))

    def test_digest_overflow(self):
        body = digest(CFG, [(pr(n=1), ["`a`"])], 7, "https://f")
        self.assertIn("8 new PRs match", body)
        self.assertIn("7 more", body)

    def test_defuse(self):
        out = defuse("Fix @foo and #123 see https://github.com/x")
        self.assertNotIn("@foo", out)
        self.assertNotIn("#123", out)
        self.assertNotIn("https://", out)
        self.assertEqual(out.replace(ZWSP, ""), "Fix @foo and #123 see https://github.com/x")

    def test_digest_has_no_target_links(self):
        body = digest(CFG, [(pr(n=12345, title="@bar #9"), ["`core/`"])], 0, "https://example.github.io/w/feeds/me.xml")
        self.assertNotIn("github.com/godotengine", body)
        self.assertNotIn("#12345", body)
        self.assertIn("https://example.github.io/w/pr/?n=12345", body)
        self.assertIn("1 new PR matches", body)


class StateTest(unittest.TestCase):
    def test_prune(self):
        old = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d))
            st.notified = [{"at": old, "engaged": True}, {"at": old, "engaged": None},
                           {"at": new, "engaged": False}]
            st.save()
            st = State(Path(d))
            self.assertEqual(len(st.notified), 1)
            self.assertEqual(st.engagement_stats(), (1, 2))


class QueueTest(unittest.TestCase):
    def test_draft_made_ready_after_subscribing(self):
        from watcher import notify
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d))
            st.subs = {"1": {"login": "me", "created_at": "2026-10-05T00:00:00Z", "queue": {}}}
            base = pr(n=7, files=["core/a.cpp"], created_at="2026-10-01T00:00:00Z", head_sha="h", analyzed_sha="h")
            active = {"1": {"issue": 1, "login": "me", "rules": m("paths: [core/]"), "errors": []}}
            st.index = {7: {**base, "draft": True}}
            notify.queue_matches(CFG, st, active)
            self.assertEqual(st.subs["1"]["queue"], {})
            st.index = {7: {**base, "draft": False, "ready_at": "2026-10-06T00:00:00Z"}}
            notify.queue_matches(CFG, st, active)
            self.assertIn("7", st.subs["1"]["queue"])
            st.subs["1"]["queue"] = {}
            st.index = {7: {**base, "draft": False, "ready_at": "2026-10-01T00:00:00Z"}}  # ready before subscribing
            notify.queue_matches(CFG, st, active)
            self.assertEqual(st.subs["1"]["queue"], {})


class SubscriptionLimitTest(unittest.TestCase):
    def test_limit_applies_to_strangers_only(self):
        from watcher import notify

        def issue(n, login, assoc):
            return {"number": n, "user": {"login": login}, "author_association": assoc,
                    "created_at": "2026-10-01T00:00:00Z", "body": "```yaml\nmatch: {paths: [core/]}\n```"}

        class FakeGitHub:
            def paginate(self, path, params=None):
                return [issue(n, "owner", "OWNER") for n in range(1, 6)] + \
                       [issue(n, "stranger", "NONE") for n in range(6, 11)]

        with tempfile.TemporaryDirectory() as d:
            active = notify.load_subscriptions(CFG, FakeGitHub(), State(Path(d)))
        valid = lambda login: sum(a["rules"] is not None for a in active.values() if a["login"] == login)
        self.assertEqual(valid("owner"), 5)
        self.assertEqual(valid("stranger"), notify.MAX_SUBSCRIPTIONS)


if __name__ == "__main__":
    unittest.main()
