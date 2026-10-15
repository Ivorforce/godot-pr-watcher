import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pathspec

from watcher.codeowners import CodeOwners, team_slug
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
        written = m("my_written_lines: [10%, 30]")
        self.assertEqual((written.my_written_lines_share, written.my_written_lines_count, written.my_lines), (0.1, 30, False))

    def test_errors(self):
        for text in ["match: {paths: [a]}\nnope: 1", "match: {my_lines: 0}", "match: {my_lines: 150%}",
                     "match: {my_lines: -3}", "match: {my_lines: lots}", "match: {my_lines: [20%, 30%]}",
                     "match: {my_lines: []}", "match: {paths: 3}", "match: {paths: [a]}\ndigest: monthly",
                     "include: [drafts]", "match: {paths: [a]}\ninclude: [everything]", "- a\n- b",
                     "match: {paths: [unclosed", "match: {paths: ['!core/']}", "match: {paths: [a], nope: 1}",
                     "match: {my_lines: 20%}\nignore: {my_lines: 40}", "match: {my_written_lines: 0}", "match: [a]", "paths: [a]"]:
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
        self.assertEqual(texts(r, pr(direct={"me": 4})), ["your lines 4 of 10"])
        self.assertEqual(texts(r, pr(direct={"me": 2}, transitive={"me": 2})), ["your lines 2 + approved 2 of 10"])
        self.assertEqual(texts(r, pr(transitive={"me": 3})), ["approved 3 of 10"])
        self.assertEqual(texts(r, pr(direct={"me": 9}, old_lines=0)), [])

    def test_my_lines_count(self):
        r = m("my_lines: 40")
        self.assertEqual(texts(r, pr(direct={"me": 39}, old_lines=2000)), [])
        self.assertEqual(texts(r, pr(direct={"me": 40}, old_lines=2000)), ["your lines 40 of 2000"])
        both = m("my_lines: [20%, 40]")
        self.assertTrue(texts(both, pr(direct={"me": 2}, old_lines=10)))      # share
        self.assertTrue(texts(both, pr(direct={"me": 40}, old_lines=2000)))   # count
        self.assertEqual(texts(both, pr(direct={"me": 39}, old_lines=2000)), [])

    def test_my_written_lines(self):
        r = m("my_written_lines: [30%, 40]")
        self.assertEqual(texts(r, pr(direct={"me": 2}, transitive={"me": 8})), [])
        self.assertEqual(texts(r, pr(direct={"me": 3}, transitive={"me": 8})), ["your lines 3 of 10"])
        both = m("my_lines: 50%, my_written_lines: 30%")
        self.assertEqual(match(both, pr(direct={"me": 3}, transitive={"me": 8}), "me", EXCLUDE), [("my_lines", "your lines 3 + approved 8 of 10")])
        self.assertEqual(match(both, pr(direct={"me": 3}), "me", EXCLUDE), [("my_written_lines", "your lines 3 of 10")])

    def test_lines_by(self):
        r = m("lines_by: {'@Ivor': [30%, 40]}, written_lines_by: {other: 50%}")
        self.assertEqual(r.lines_by, {"ivor": (0.3, 40)})
        self.assertEqual(texts(r, pr(direct={"ivor": 2})), [])
        self.assertEqual(texts(r, pr(direct={"ivor": 2}, transitive={"ivor": 1})), ["by ivor: lines 2 + approved 1 of 10"])
        self.assertEqual(texts(r, pr(transitive={"other": 9})), [])  # written only
        self.assertEqual(match(r, pr(direct={"ivor": 3, "other": 5}), "me", EXCLUDE), [("lines_by", "by ivor: lines 3 of 10")])
        self.assertEqual(texts(r, pr(direct={"other": 5})), ["by other: lines 5 of 10"])
        own = m("my_lines: 20%, lines_by: {ivor: 20%}")  # own and followed lines each show
        self.assertEqual(texts(own, pr(direct={"me": 2, "ivor": 2})), ["your lines 2 of 10", "by ivor: lines 2 of 10"])
        nested = rules("match:\n  all: {labels: [bug], lines_by: {ivor: 1}}")
        self.assertEqual(texts(nested, pr(labels=["bug"], direct={"ivor": 1})), ["by ivor: lines 1 of 10 + label `bug`"])
        for text in ["match: {lines_by: 20%}", "match: {lines_by: {}}", "match: {lines_by: {ivor: lots}}",
                     "match: {lines_by: [ivor]}", "match: {paths: [a]}\nignore: {lines_by: {ivor: 1}}",
                     "match: {lines_by: {" + ", ".join(f"p{i}: 1" for i in range(11)) + "}}"]:
            self.assertIsNone(parse_rules(f"```yaml\n{text}\n```")[0], text)
        notes = diagnose(m("lines_by: {ivor: 1, ghost: 1}, written_lines_by: {ghost: 1}"), [pr(transitive={"ivor": 1})], "me")
        self.assertEqual(len(notes), 1)
        self.assertIn("`ghost`", notes[0])

    def test_most_lines(self):
        r = m("most_lines: true")
        self.assertEqual(texts(r, pr(direct={"me": 1, "other": 1})), ["most lines: your lines 1 of 10"])
        self.assertEqual(texts(r, pr(direct={"me": 1}, transitive={"other": 2})), [])
        self.assertTrue(texts(r, pr(direct={"me": 1, "someone": 5, "x[bot]": 9})))  # author and bots don't count
        self.assertEqual(texts(r, pr(direct={"other": 1})), [])
        self.assertEqual(parse_rules("```yaml\nmatch: {most_lines: yes please}\n```")[0], None)
        self.assertEqual(parse_rules("```yaml\nmatch: {most_lines: false}\n```")[0], None)  # nothing to match

    def test_teams(self):
        owners = CodeOwners("# comment\n*.cpp @godotengine/buildsystem\n/core/ @godotengine/core @someone\n"
                            "/core/io/ @godotengine/network\n")
        self.assertEqual(owners.teams_of("core/io/file.cpp"), {"network"})  # last matching rule wins
        self.assertEqual(owners.teams_of("core/variant/variant.cpp"), {"core"})
        self.assertEqual(owners.teams_of("editor/x.cpp"), {"buildsystem"})
        self.assertEqual(owners.teams, {"buildsystem", "core", "network"})
        self.assertEqual([team_slug(t) for t in ("@godotengine/Core", "godotengine/core", "core")], ["core"] * 3)
        r = m("teams: ['@godotengine/core']")
        self.assertEqual(match(r, pr(), "me", EXCLUDE, owners), [("teams", "team `core` → `core/variant/variant.cpp`")])
        self.assertEqual(match(r, pr(files=["core/io/file.cpp"]), "me", EXCLUDE, owners), [])
        self.assertEqual(match(r, pr(), "me", EXCLUDE), [])  # CODEOWNERS unavailable
        ignoring = m("paths: [core/]", ignore="{teams: [network]}")
        self.assertEqual(match(ignoring, pr(files=["core/io/file.cpp"]), "me", EXCLUDE, owners), [])
        self.assertTrue(match(ignoring, pr(), "me", EXCLUDE, owners))
        notes = diagnose(m("teams: [core, nope]"), [pr()], "me", owners=owners)
        self.assertEqual(len(notes), 1)
        self.assertIn("`nope`", notes[0])

    def test_all(self):
        both = rules("match:\n  all:\n    - authors: [someone]\n      paths: [core/]")
        self.assertEqual(texts(both, pr()), ["`core/` → `core/variant/variant.cpp` + author someone"])
        self.assertEqual(texts(both, pr(files=["editor/x.cpp"])), [])
        self.assertEqual(texts(both, pr(author="other")), [])
        single = rules("match:\n  labels: [bug]\n  all: {authors: [someone], my_lines: 30%}")
        self.assertEqual(texts(single, pr(direct={"me": 4})), ["your lines 4 of 10 + author someone"])
        self.assertEqual(texts(single, pr(labels=["bug"])), ["label `bug`"])  # plain keys still OR
        backport = rules("match:\n  all:\n    - branches: [3.x]\n      labels: [bug]")
        self.assertEqual([k for k, _ in match(backport, pr(base_ref="3.x", default_branch=False, labels=["bug"]),
                                               "me", EXCLUDE)], ["branches"])

    def test_ignore_all(self):
        r = rules("match:\n  paths: [core/]\nignore:\n  all:\n    - authors: [someone]\n      paths: ['**/tests/']")
        self.assertEqual(texts(r, pr(files=["core/a.cpp", "core/tests/t.h"])), [])  # whole PR left out
        self.assertTrue(texts(r, pr(files=["core/a.cpp"])))
        self.assertTrue(texts(r, pr(author="other", files=["core/a.cpp", "core/tests/t.h"])))

    def test_all_errors(self):
        for text in ["match: {all: []}", "match: {all: [{}]}", "match: {all: [3]}", "match: {all: [{nope: 1}]}",
                     "match: {all: [{all: [{paths: [a]}]}]}", "match: {all: [{authors: [a], my_lines: lots}]}",
                     "match: {all: [" + ", ".join(["{authors: [a]}"] * 11) + "]}",
                     "match: {paths: [a]}\nignore: {all: [{my_lines: 20%}]}"]:
            r, errors = parse_rules(f"```yaml\n{text}\n```")
            self.assertIsNone(r, text)
            self.assertTrue(errors, text)
        self.assertIsNone(parse_rules("```yaml\nignore: {all: [{authors: [a]}]}\n```")[0])  # nothing to match

    def test_all_diagnose(self):
        r = rules("match:\n  all:\n    - authors: [ghost]\n      labels: [bgu]")
        self.assertEqual(len(diagnose(r, [pr(labels=["bug"])], "me")), 2)

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
        r = m("paths: [editor/, core/, servers/], labels: [bgu], authors: [ghost]")
        recs = [pr(files=["core/a.cpp"], labels=["bug"])]
        notes = diagnose(r, recs, "me", ["core/a.cpp", "editor/b.cpp"])  # editor/ exists, just no PR
        self.assertEqual(len(notes), 3)
        self.assertIn("servers/", notes[0])
        self.assertEqual(len(diagnose(r, recs, "me")), 2)  # repository files unknown: no path notes


class RenderTest(unittest.TestCase):
    def test_code_span(self):
        self.assertEqual(code_span("a`b\nc"), "`a'b c`")
        self.assertEqual(len(code_span("x" * 500)), 82)

    def test_done_prs_struck_through(self):
        from watcher.render import pr_line
        line = pr_line(CFG, pr(n=5, title="a ~~b [c]", state="merged"), ["x"])
        self.assertTrue(line.startswith("- ~~[a "))
        self.assertIn("~~ (merged) — [someone](https://github.com/someone)", line)
        self.assertNotIn("~~b", line)
        self.assertIn("\\[c\\]", line)  # brackets in titles can't break the link
        self.assertIn("<sub>PR 5 · x</sub>", line)
        self.assertTrue(pr_line(CFG, pr(n=5), ["x"]).startswith("- [t]("))

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
            st.notified = [{"at": old, "engaged": True, "kinds": ["my_lines", "paths"]},
                           {"at": old, "engaged": None, "kinds": ["my_lines"]},
                           {"at": new, "engaged": False, "kinds": ["my_lines"]}]
            st.save()
            st = State(Path(d))
            self.assertEqual(len(st.notified), 1)
            self.assertEqual(st.engagement_stats(), (1, 2))
            self.assertEqual(st.engagement_by_kind(), {"my_lines": (1, 2), "paths": (1, 1)})


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


class InvolvedTest(unittest.TestCase):
    def run_digest(self, include=None):
        from watcher import notify

        class FakeGitHub:
            posted = []

            def graphql(self, query, variables=None, attempts=4):
                return {"repository": {
                    "p7": {"body": "", "participants": {"nodes": [{"login": "Me"}]},
                           "assignees": {"nodes": []}, "reviewRequests": {"nodes": []}},
                    "p8": {"body": "cc @me", "participants": {"nodes": []},
                           "assignees": {"nodes": []}, "reviewRequests": {"nodes": []}},
                    "p9": {"body": "cc `@me` @meow", "participants": {"nodes": []}, "assignees": {"nodes": []},
                           "reviewRequests": {"nodes": [{"requestedReviewer": {}}]}}}}

            def rest(self, path, params=None, method="GET", body=None):
                self.posted.append(body["body"])
                return {"id": 100}

        gh = FakeGitHub()
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d))
            st.subs = {"1": {"login": "me", "created_at": "2026-10-01T00:00:00Z", "queue": {"7": [], "8": [], "9": []}}}
            st.index = {n: pr(n=n, files=["core/a.cpp"]) for n in (7, 8, 9)}
            rules_ = m("paths: [core/]", **({"include": include} if include else {}))
            notify.send_digests(CFG, gh, st, {"1": {"issue": 1, "login": "me", "rules": rules_, "errors": []}}, False)
            return gh.posted, {r["pr"]: r.get("skipped") for r in st.notified}

    def test_skips_involved(self):
        posted, recorded = self.run_digest()
        self.assertEqual(recorded, {7: "involved", 8: "involved", 9: None})
        self.assertIn("?n=9", posted[0])
        self.assertNotIn("?n=7", posted[0])

    def test_include_involved(self):
        posted, recorded = self.run_digest("[involved]")
        self.assertEqual(recorded, {7: None, 8: None, 9: None})


class UpdateDigestTest(unittest.TestCase):
    def test_done_prs_struck_through_later(self):
        from watcher import notify

        class FakeGitHub:
            calls = []

            def graphql(self, query, variables=None, attempts=4):
                return {"repository": {}}

            def rest(self, path, params=None, method="GET", body=None):
                self.calls.append((method, path, body["body"]))
                return {"id": 100}

        gh = FakeGitHub()
        with tempfile.TemporaryDirectory() as d:
            st = State(Path(d))
            st.subs = {"1": {"login": "me", "created_at": "2026-10-01T00:00:00Z", "queue": {"7": [], "8": []}}}
            st.index = {n: pr(n=n, title=f"old {n}", files=["core/a.cpp"]) for n in (7, 8)}
            active = {"1": {"issue": 1, "login": "me", "rules": m("paths: [core/]"), "errors": []}}
            notify.send_digests(CFG, gh, st, active, False)
            notify.update_digests(CFG, gh, st, False)
            self.assertEqual(len(gh.calls), 1)  # nothing changed yet

            st.index[7].update(state="closed", title="new 7")
            st.index[8].update(title="new 8")
            notify.update_digests(CFG, gh, st, False)
            method, path, body = gh.calls[-1]
            self.assertEqual((method, path), ("PATCH", "repos/example/w/issues/comments/100"))
            self.assertIn("~~[old 7]", body)
            self.assertIn("(closed)", body)
            self.assertIn("- [old 8]", body)
            self.assertNotIn("new 7", body)

            notify.update_digests(CFG, gh, st, False)
            self.assertEqual(len(gh.calls), 2)  # recorded, so not patched again
            st.save()
            self.assertEqual(State(Path(d)).notified[0]["digest"]["state"], "closed")


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


class KeepAliveTest(unittest.TestCase):
    def test_only_while_subscribed(self):
        from watcher import notify

        class FakeGitHub:
            calls = []

            def rest(self, path, params=None, method="GET", body=None):
                self.calls.append((method, path))

        gh = FakeGitHub()
        notify.keep_alive(CFG, gh, {})
        self.assertEqual(gh.calls, [])
        notify.keep_alive(CFG, gh, {"1": {}})
        self.assertEqual(len(gh.calls), 1)
        self.assertEqual(gh.calls[0][0], "PUT")
        self.assertTrue(gh.calls[0][1].endswith("/actions/workflows/watch.yml/enable"))


if __name__ == "__main__":
    unittest.main()
