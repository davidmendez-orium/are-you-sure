#!/usr/bin/env python3
"""Behaviour tests for the are-you-sure hook.

Runs the hook as a subprocess exactly as Claude Code does — JSON on stdin, JSON on
stdout — so what is under test is the real contract, not an importable subset of it.

    python3 plugins/are-you-sure/tests/test_are_you_sure.py
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "hooks" / "are_you_sure.py"


def transcript(rows: list[dict], directory: Path) -> str:
    path = directory / "transcript.jsonl"
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    return str(path)


def user_prompt(text: str = "do the thing") -> dict:
    return {"type": "user", "message": {"content": [{"type": "text", "text": text}]}}


def tool_result(name: str = "Bash") -> dict:
    return {"type": "user", "message": {"content": [{"type": "tool_result", "content": "ok"}]}}


def tool_use(name: str, **inputs) -> dict:
    return {
        "type": "assistant",
        "message": {"content": [{"type": "tool_use", "name": name, "input": inputs}]},
    }


def assistant_text(text: str) -> dict:
    return {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}}


class HookCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def run_hook(self, payload: dict, **env_overrides) -> dict | None:
        env = {
            "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            "HOME": str(self.tmp),
            "ARE_YOU_SURE_STATE_DIR": str(self.tmp / "state"),
            "ARE_YOU_SURE_LOG": "off",
        }
        env.update({k: str(v) for k, v in env_overrides.items()})
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"hook must always exit 0; stderr={proc.stderr}")
        out = proc.stdout.strip()
        return json.loads(out) if out else None

    def stop_payload(self, message: str, rows: list[dict], **extra) -> dict:
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-1",
            "prompt_id": "prompt-1",
            "transcript_path": transcript(rows, self.tmp),
            "last_assistant_message": message,
            "stop_hook_active": False,
        }
        payload.update(extra)
        return payload

    def assertBlocked(self, result: dict | None) -> str:
        self.assertIsNotNone(result, "expected a block decision, got no output")
        spec = result["hookSpecificOutput"]
        self.assertEqual(spec["decision"], "block")
        self.assertIn("YOU SURE ABOUT THAT?", spec["reason"])
        return spec["reason"]

    def assertAllowed(self, result: dict | None) -> None:
        if result is None:
            return
        decision = result.get("hookSpecificOutput", {}).get("decision")
        self.assertNotEqual(decision, "block", f"expected allow, got a block: {result}")


# Long enough to clear the conversational-length floor.
PADDING = (
    " The change touches the checkout path and the surrounding helpers, so I went "
    "through the call sites one at a time to be sure nothing else depended on the "
    "old signature before finishing up the edit and tidying the imports."
)


class TestPromptSubmit(HookCase):
    def test_injects_the_contract(self) -> None:
        result = self.run_hook({"hook_event_name": "UserPromptSubmit", "prompt_text": "hi"})
        spec = result["hookSpecificOutput"]
        self.assertEqual(spec["hookEventName"], "UserPromptSubmit")
        self.assertIn("Evidence contract", spec["additionalContext"])
        self.assertIn("INFERRED", spec["additionalContext"])

    def test_mode_off_injects_nothing(self) -> None:
        result = self.run_hook(
            {"hook_event_name": "UserPromptSubmit", "prompt_text": "hi"},
            ARE_YOU_SURE_MODE="off",
        )
        self.assertIsNone(result)


class TestVerificationClaims(HookCase):
    def test_claimed_verification_without_running_anything_is_blocked(self) -> None:
        rows = [user_prompt(), tool_use("Read", file_path="/app/checkout.ts"), tool_result()]
        result = self.run_hook(self.stop_payload(
            "I verified the fix works and all tests pass now." + PADDING, rows,
        ))
        reason = self.assertBlocked(result)
        self.assertIn("ran no test, build, or command", reason)

    def test_claimed_verification_with_a_real_test_run_is_allowed(self) -> None:
        rows = [
            user_prompt(),
            tool_use("Bash", command="npm test -- checkout.spec.ts"),
            tool_result(),
        ]
        result = self.run_hook(self.stop_payload(
            "I verified the fix works: `npm test` reports 14 passed, 0 failed." + PADDING,
            rows,
        ))
        self.assertAllowed(result)

    def test_lenient_mode_still_catches_it(self) -> None:
        rows = [user_prompt(), tool_use("Read", file_path="/app/x.ts"), tool_result()]
        result = self.run_hook(
            self.stop_payload("I have confirmed this works correctly." + PADDING, rows),
            ARE_YOU_SURE_MODE="lenient",
        )
        self.assertBlocked(result)


class TestUncitedClaims(HookCase):
    def test_conclusion_with_no_reads_and_no_citation_is_blocked(self) -> None:
        rows = [user_prompt()]
        result = self.run_hook(self.stop_payload(
            "The root cause is a stale cache key, and nothing else references that "
            "helper so the change is safe to land." + PADDING,
            rows,
        ))
        reason = self.assertBlocked(result)
        self.assertIn("root cause is", reason)

    def test_conclusion_with_a_file_line_citation_is_allowed(self) -> None:
        rows = [user_prompt(), tool_use("Grep", pattern="cacheKey"), tool_result()]
        result = self.run_hook(self.stop_payload(
            "The root cause is a stale cache key built at src/cache/key.ts:31, where the "
            "locale is dropped from the tuple." + PADDING,
            rows,
        ))
        self.assertAllowed(result)

    def test_lenient_mode_lets_uncited_conclusions_through(self) -> None:
        rows = [user_prompt()]
        result = self.run_hook(
            self.stop_payload("The root cause is a stale cache key." + PADDING, rows),
            ARE_YOU_SURE_MODE="lenient",
        )
        self.assertAllowed(result)


class TestStrictOnlyRules(HookCase):
    # Carries an absolute and a citation, but no bare conclusion — so in heuristic
    # mode there is nothing to catch, and only the strict rule should fire.
    ABSOLUTE = (
        "The locale normaliser at src/i18n/locale.ts:18 runs in all cases, so the tuple "
        "always ends up lowercased before it reaches the cache." + PADDING
    )

    def test_absolutes_pass_in_heuristic_mode(self) -> None:
        rows = [user_prompt(), tool_use("Grep", pattern="helper"), tool_result()]
        self.assertAllowed(self.run_hook(self.stop_payload(self.ABSOLUTE, rows)))

    def test_absolutes_are_blocked_in_strict_mode(self) -> None:
        rows = [user_prompt(), tool_use("Grep", pattern="helper"), tool_result()]
        reason = self.assertBlocked(self.run_hook(
            self.stop_payload(self.ABSOLUTE, rows), ARE_YOU_SURE_MODE="strict",
        ))
        self.assertIn("exhaustive claim", reason)

    def test_vague_counts_are_blocked_in_strict_mode(self) -> None:
        rows = [user_prompt(), tool_use("Grep", pattern="foo"), tool_result()]
        reason = self.assertBlocked(self.run_hook(
            self.stop_payload(
                "I updated several call sites at src/a.ts:10 and the rest followed." + PADDING,
                rows,
            ),
            ARE_YOU_SURE_MODE="strict",
        ))
        self.assertIn("estimates a count", reason)


class TestNeverTrapsTheSession(HookCase):
    def test_a_question_to_the_user_is_never_blocked(self) -> None:
        rows = [user_prompt()]
        result = self.run_hook(self.stop_payload(
            "The root cause is a stale cache key and nothing else references it." + PADDING
            + "\n\nDo you want me to land the fix, or write the regression test first?",
            rows,
        ))
        self.assertAllowed(result)

    def test_ask_user_question_tool_use_is_never_blocked(self) -> None:
        rows = [user_prompt(), tool_use("AskUserQuestion"), tool_result()]
        result = self.run_hook(self.stop_payload(
            "The root cause is a stale cache key and nothing else references it." + PADDING,
            rows,
        ))
        self.assertAllowed(result)

    def test_only_one_challenge_per_turn(self) -> None:
        rows = [user_prompt()]
        payload = self.stop_payload(
            "The root cause is a stale cache key and nothing else references it." + PADDING,
            rows,
        )
        self.assertBlocked(self.run_hook(payload))
        self.assertAllowed(self.run_hook(payload))

    def test_a_new_turn_gets_a_fresh_challenge(self) -> None:
        rows = [user_prompt()]
        message = "The root cause is a stale cache key and nothing else references it." + PADDING
        self.assertBlocked(self.run_hook(self.stop_payload(message, rows)))
        self.assertBlocked(self.run_hook(
            self.stop_payload(message, rows, prompt_id="prompt-2"),
        ))

    def test_session_budget_is_respected(self) -> None:
        rows = [user_prompt()]
        message = "The root cause is a stale cache key and nothing else references it." + PADDING
        self.assertBlocked(self.run_hook(
            self.stop_payload(message, rows), ARE_YOU_SURE_MAX_PER_SESSION=1,
        ))
        self.assertAllowed(self.run_hook(
            self.stop_payload(message, rows, prompt_id="prompt-2"),
            ARE_YOU_SURE_MAX_PER_SESSION=1,
        ))

    def test_short_conversational_replies_are_left_alone(self) -> None:
        result = self.run_hook(self.stop_payload("Done — it works now.", [user_prompt()]))
        self.assertAllowed(result)


class TestRobustness(HookCase):
    def test_malformed_stdin_fails_open(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(HOOK)],
            input="not json at all",
            capture_output=True,
            text=True,
            env={"HOME": str(self.tmp), "ARE_YOU_SURE_LOG": "off"},
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")

    def test_missing_transcript_fails_open_not_shut(self) -> None:
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-2",
            "prompt_id": "p-1",
            "transcript_path": "/nonexistent/path.jsonl",
            "last_assistant_message": "I verified it works and all tests pass." + PADDING,
        }
        # No transcript means no provable execution, so the claim is still challenged —
        # the hook degrades to suspicion, never to silent approval.
        self.assertBlocked(self.run_hook(payload))

    def test_falls_back_to_the_transcript_when_the_message_is_absent(self) -> None:
        rows = [
            user_prompt(),
            tool_use("Read", file_path="/app/x.ts"),
            tool_result(),
            assistant_text("I verified the fix works and all tests pass." + PADDING),
        ]
        payload = {
            "hook_event_name": "Stop",
            "session_id": "sess-3",
            "prompt_id": "p-1",
            "transcript_path": transcript(rows, self.tmp),
        }
        self.assertBlocked(self.run_hook(payload))

    def test_tool_results_do_not_end_the_turn(self) -> None:
        # The execution sits behind two tool_result rows; those arrive as type=user and
        # must not be mistaken for the human's next prompt.
        rows = [
            user_prompt(),
            tool_use("Bash", command="pytest tests/"),
            tool_result(),
            tool_use("Read", file_path="/app/x.ts"),
            tool_result(),
        ]
        result = self.run_hook(self.stop_payload(
            "I verified the fix works — pytest reports 8 passed." + PADDING, rows,
        ))
        self.assertAllowed(result)

    def test_subagent_stop_is_handled_and_labelled(self) -> None:
        rows = [user_prompt()]
        payload = self.stop_payload(
            "The root cause is a stale cache key and nothing else references it." + PADDING,
            rows,
            hook_event_name="SubagentStop",
        )
        result = self.run_hook(payload)
        self.assertBlocked(result)
        self.assertEqual(result["hookSpecificOutput"]["hookEventName"], "SubagentStop")

    def test_quoted_material_is_not_treated_as_the_agents_own_claim(self) -> None:
        rows = [user_prompt(), tool_use("Read", file_path="/app/x.ts"), tool_result()]
        result = self.run_hook(self.stop_payload(
            "Here is what the ticket says:\n\n> I verified the fix works and all tests "
            "pass.\n\nI have not run anything yet, so that is UNVERIFIED for now." + PADDING,
            rows,
        ))
        self.assertAllowed(result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
