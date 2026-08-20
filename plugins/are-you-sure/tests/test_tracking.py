#!/usr/bin/env python3
"""Tests for the challenge record, the measured verdict, and the dashboard.

The full block → revise → score cycle is driven through the hook as a subprocess,
so what's covered is the sequence the harness actually produces: a stop that gets
blocked, then a second stop carrying the revision.

    python3 plugins/are-you-sure/tests/test_tracking.py
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent / "hooks"
HOOK = HOOKS / "are_you_sure.py"

sys.path.insert(0, str(HOOKS))
import ays_db  # noqa: E402

CLAIM = (
    "The root cause is a stale cache key and nothing else references that helper, so "
    "this is safe to land. I went through the call sites one at a time before finishing."
)


def rows(path: Path, table: str) -> list[dict]:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table} ORDER BY 1")]
    finally:
        conn.close()


class Cycle(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.db = self.tmp / "challenges.db"
        self.addCleanup(self._tmp.cleanup)

    def transcript(self, tool_rows: list[dict]) -> str:
        path = self.tmp / "t.jsonl"
        base = [{"type": "user", "userType": "external",
                 "message": {"role": "user", "content": "go"}}]
        path.write_text("\n".join(json.dumps(r) for r in base + tool_rows), encoding="utf-8")
        return str(path)

    def stop(self, message: str, tool_rows: list[dict], prompt_id: str = "p1") -> dict | None:
        payload = {
            "hook_event_name": "Stop", "session_id": "s1", "prompt_id": prompt_id,
            "cwd": "/repo", "transcript_path": self.transcript(tool_rows),
            "last_assistant_message": message,
        }
        proc = subprocess.run(
            [sys.executable, str(HOOK)], input=json.dumps(payload),
            capture_output=True, text=True, timeout=30,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.tmp),
                 "ARE_YOU_SURE_STATE_DIR": str(self.tmp / "state"),
                 "ARE_YOU_SURE_DB": str(self.db), "ARE_YOU_SURE_LOG": "off"},
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return json.loads(proc.stdout) if proc.stdout.strip() else None

    def cycle(self, revision: str, before: list[dict], after: list[dict]) -> dict:
        """Block on CLAIM, then hand back `revision` as the second stop."""
        self.assertIsNotNone(self.stop(CLAIM, before), "expected the first stop to block")
        self.assertIsNone(self.stop(revision, after), "second stop must be allowed")
        outcomes = rows(self.db, "outcomes")
        self.assertEqual(len(outcomes), 1, "expected exactly one scored outcome")
        return outcomes[0]


def tool(name: str, **inputs) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": name, "input": inputs}]}}


def result() -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


class TestRecording(Cycle):
    def test_a_block_records_the_challenge_with_its_rule_and_phrase(self) -> None:
        self.stop(CLAIM, [])
        recorded = rows(self.db, "challenges")
        self.assertEqual(len(recorded), 1)
        row = recorded[0]
        self.assertEqual(json.loads(row["rules"]), ["uncited-conclusion"])
        self.assertEqual(json.loads(row["findings"])[0]["phrase"], "The root cause is")
        self.assertEqual(row["before_text"], CLAIM)
        self.assertEqual(row["cwd"], "/repo")
        self.assertEqual(rows(self.db, "outcomes"), [], "not scored until the revision")

    def test_an_allowed_stop_records_nothing(self) -> None:
        self.stop("Short and harmless.", [])
        self.assertFalse(self.db.exists() and rows(self.db, "challenges"))


class TestVerdicts(Cycle):
    def test_proof_arriving_scores_improved(self) -> None:
        out = self.cycle(
            "The stale key is built at src/cache/key.ts:31, where the locale is dropped. "
            "`npm test` reports 14 passed, 0 failed.",
            before=[],
            after=[tool("Bash", command="npm test"), result()],
        )
        self.assertEqual(out["verdict"], "improved")
        self.assertTrue(out["evidence_gained"])
        self.assertTrue(out["claim_retracted"])
        self.assertEqual(out["tools_since"], 1)

    def test_an_honest_label_without_new_evidence_scores_hedged(self) -> None:
        out = self.cycle(
            "INFERRED, not yet run: the stale cache key looks like the likely cause, but "
            "I have not verified it. Running the checkout suite would settle it.",
            before=[], after=[],
        )
        self.assertEqual(out["verdict"], "hedged")
        self.assertTrue(out["label_added"])
        self.assertFalse(out["evidence_gained"])

    def test_restating_the_claim_scores_ignored(self) -> None:
        out = self.cycle(CLAIM, before=[], after=[])
        self.assertEqual(out["verdict"], "ignored")
        self.assertFalse(out["evidence_gained"])
        self.assertFalse(out["claim_retracted"])
        self.assertTrue(json.loads(out["findings_after"]), "the finding should still fire")

    def test_softer_wording_alone_does_not_count_as_improvement(self) -> None:
        # The README promises this: "should be correct" carries the same claim.
        out = self.cycle(
            "The root cause is probably a stale cache key, and I think nothing else "
            "references that helper, so this should be safe to land in my view.",
            before=[], after=[],
        )
        self.assertNotIn(out["verdict"], ("improved", "hedged"))

    def test_doing_the_work_but_leaving_the_claim_uncited_scores_unchanged(self) -> None:
        # It went and ran the suite — real work, credited — but the sentence on the
        # page still asserts a root cause the reader cannot check.
        out = self.cycle(
            "I ran the checkout suite. The root cause is a stale cache key, and nothing "
            "else references that helper, so this is still safe to land as written.",
            before=[],
            after=[tool("Bash", command="npm test -- checkout"), result()],
        )
        self.assertEqual(out["verdict"], "unchanged")
        self.assertTrue(out["evidence_gained"])
        self.assertTrue(json.loads(out["findings_after"]))

    def test_citing_the_source_clears_the_check(self) -> None:
        out = self.cycle(
            "The root cause is a stale cache key built at src/cache/key.ts:31, where the "
            "locale is dropped from the tuple before it reaches the cache.",
            before=[],
            after=[tool("Read", file_path="/app/cache.ts"), result()],
        )
        self.assertEqual(out["verdict"], "improved")
        self.assertTrue(out["evidence_gained"])


class TestVerdictTable(unittest.TestCase):
    """The rule table on its own, so the precedence is pinned independently."""

    def verdict(self, **kw) -> str:
        base = {"findings_after": [], "evidence_gained": False,
                "claim_retracted": False, "label_added": False}
        return ays_db.verdict_for(**{**base, **kw})

    def test_cleared_with_evidence_is_improved(self) -> None:
        self.assertEqual(self.verdict(evidence_gained=True), "improved")

    def test_cleared_by_retraction_alone_is_hedged_not_improved(self) -> None:
        # Dropping the phrase is not proof. Filing it as improved would leave the
        # hedged bucket near-empty and misreport how the wins were earned.
        self.assertEqual(self.verdict(claim_retracted=True), "hedged")

    def test_evidence_outranks_retraction(self) -> None:
        self.assertEqual(
            self.verdict(evidence_gained=True, claim_retracted=True), "improved")

    def test_evidence_outranks_a_label(self) -> None:
        self.assertEqual(self.verdict(evidence_gained=True, label_added=True), "improved")

    def test_cleared_by_label_alone_is_hedged(self) -> None:
        self.assertEqual(self.verdict(label_added=True), "hedged")

    def test_still_firing_but_something_changed_is_unchanged(self) -> None:
        self.assertEqual(self.verdict(findings_after=["x"], label_added=True), "unchanged")

    def test_still_firing_and_nothing_changed_is_ignored(self) -> None:
        self.assertEqual(self.verdict(findings_after=["x"]), "ignored")

    def test_a_bare_clear_with_no_signal_is_not_a_win(self) -> None:
        # Nothing measurable changed, so the checks clearing is not evidence of a
        # better answer — most often the claim was simply reworded around the regex.
        self.assertEqual(self.verdict(), "ignored")


class TestRating(Cycle):
    def rate(self, *args: str) -> str:
        proc = subprocess.run(
            [sys.executable, str(HOOK), "rate", *args], capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.tmp),
                 "ARE_YOU_SURE_DB": str(self.db), "ARE_YOU_SURE_LOG": "off"}, timeout=30,
        )
        return proc.stdout.strip()

    def test_a_human_rating_is_stored(self) -> None:
        self.cycle(CLAIM, before=[], after=[])
        self.assertIn("rated no-improvement", self.rate("1", "no-improvement", "just reworded"))
        out = rows(self.db, "outcomes")[0]
        self.assertEqual(out["human_rating"], "no-improvement")
        self.assertEqual(out["note"], "just reworded")

    def test_an_unknown_rating_is_refused(self) -> None:
        self.cycle(CLAIM, before=[], after=[])
        self.assertIn("must be one of", self.rate("1", "great"))

    def test_rating_an_unscored_challenge_is_refused(self) -> None:
        self.stop(CLAIM, [])
        self.assertIn("no resolved challenge #1", self.rate("1", "improvement"))


class TestDashboard(Cycle):
    def dashboard(self, *args: str) -> str:
        proc = subprocess.run(
            [sys.executable, str(HOOK), "dashboard", *args], capture_output=True, text=True,
            env={"PATH": "/usr/bin:/bin", "HOME": str(self.tmp),
                 "ARE_YOU_SURE_DB": str(self.db), "ARE_YOU_SURE_LOG": "off"}, timeout=30,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        return proc.stdout

    def test_an_empty_database_says_so_rather_than_showing_zeroes(self) -> None:
        out = self.dashboard()
        self.assertIn("No challenges recorded yet", out)
        self.assertNotIn("IMPROVED THE ANSWER", out)

    def test_it_reports_the_improvement_rate(self) -> None:
        self.cycle(
            "Built at src/cache/key.ts:31; `npm test` reports 14 passed.",
            before=[], after=[tool("Bash", command="npm test"), result()],
        )
        out = self.dashboard("--rules")
        self.assertIn("IMPROVED THE ANSWER", out)
        self.assertIn("100.0%", out)
        self.assertIn("uncited-conclusion", out)
        self.assertIn("evidence arrived", out)

    def test_an_open_challenge_is_counted_separately_from_a_scored_one(self) -> None:
        self.stop(CLAIM, [])
        out = self.dashboard()
        self.assertIn("1 still open", out)
        self.assertIn("0 scored", out)

    def test_json_output_is_machine_readable(self) -> None:
        self.cycle(CLAIM, before=[], after=[])
        data = json.loads(self.dashboard("--json"))
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["rows"][0]["verdict"], "ignored")


class TestNeverBreaksTheHook(Cycle):
    def test_an_unwritable_database_still_lets_the_block_happen(self) -> None:
        # Telemetry is optional; enforcement is not. A dead recorder must not
        # silently disarm the checker.
        readonly = self.tmp / "ro"
        readonly.mkdir()
        readonly.chmod(0o500)
        self.addCleanup(readonly.chmod, 0o700)
        self.db = readonly / "nope" / "challenges.db"
        self.assertIsNotNone(self.stop(CLAIM, []), "the block must still be issued")




class TestServe(Cycle):
    """The --serve page, driven over real HTTP against a real socket."""

    def start(self):
        import threading
        import os
        sys.path.insert(0, str(HOOKS))
        os.environ["ARE_YOU_SURE_DB"] = str(self.db)
        import importlib
        import ays_db as _db
        import ays_serve
        importlib.reload(_db)
        importlib.reload(ays_serve)
        from http.server import ThreadingHTTPServer
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), ays_serve.Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(httpd.shutdown)
        self.addCleanup(httpd.server_close)
        return f"http://127.0.0.1:{httpd.server_address[1]}"

    def get(self, base: str, path: str = "/") -> tuple[int, str]:
        import urllib.request
        with urllib.request.urlopen(base + path, timeout=10) as r:
            return r.status, r.read().decode()

    def test_it_binds_to_localhost_only(self) -> None:
        # The record holds session content; it must not reach a network interface.
        src = (HOOKS / "ays_serve.py").read_text()
        self.assertIn('("127.0.0.1", chosen)', src)
        self.assertNotIn('("0.0.0.0"', src)

    def test_the_page_renders_the_rate_and_the_rows(self) -> None:
        base = self.start()
        self.cycle(
            "Built at src/cache/key.ts:31; `npm test` reports 14 passed.",
            before=[], after=[tool("Bash", command="npm test"), result()],
        )
        status, body = self.get(base)
        self.assertEqual(status, 200)
        self.assertIn("improved the answer", body)
        self.assertIn("100.0%", body)
        self.assertIn("uncited-conclusion", body)
        self.assertIn("unaudited", body)

    def test_an_empty_record_says_so(self) -> None:
        status, body = self.get(self.start())
        self.assertEqual(status, 200)
        self.assertIn("No challenges recorded yet", body)

    def test_stored_text_is_escaped_not_injected(self) -> None:
        base = self.start()
        self.assertIsNotNone(self.stop(CLAIM, []))
        self.assertIsNone(self.stop(
            "<script>alert('xss')</script> The root cause is a stale cache key and "
            "nothing else references that helper, so this is safe to land here.", []))
        _, body = self.get(base)
        self.assertIn("&lt;script&gt;", body)
        self.assertNotIn("<script>alert", body)

    def test_rating_from_the_page_persists_and_redirects(self) -> None:
        import urllib.request
        import urllib.parse
        base = self.start()
        self.cycle(CLAIM, before=[], after=[])
        data = urllib.parse.urlencode(
            {"id": "1", "rating": "no-improvement", "note": "reworded only"}).encode()
        req = urllib.request.Request(base + "/rate", data=data, method="POST")
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)      # redirect followed to /
        out = rows(self.db, "outcomes")[0]
        self.assertEqual(out["human_rating"], "no-improvement")
        self.assertEqual(out["note"], "reworded only")
        _, body = self.get(base)
        self.assertIn("your rating", body)

    def test_the_api_route_serves_json(self) -> None:
        base = self.start()
        self.cycle(CLAIM, before=[], after=[])
        status, body = self.get(base, "/api")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["rows"][0]["verdict"], "ignored")

    def test_an_unknown_route_is_a_404(self) -> None:
        import urllib.error
        base = self.start()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get(base, "/nope")
        self.assertEqual(caught.exception.code, 404)

if __name__ == "__main__":
    unittest.main(verbosity=2)
