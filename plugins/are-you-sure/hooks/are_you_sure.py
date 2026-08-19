#!/usr/bin/env python3
"""
are-you-sure — challenge unproven claims before the agent is allowed to finish.

One script, three hook events, dispatched on ``hook_event_name``:

``UserPromptSubmit``
    Injects the evidence contract *before* generation. This is the half that
    actually saves tokens: the cheapest way to avoid a second pass is to hedge
    honestly on the first one.

``Stop`` / ``SubagentStop``
    Reads the message the agent just produced, looks for claims it did not
    earn, and blocks the stop with a specific challenge when it finds them.
    A Stop hook fires *after* the message is generated, so it cannot suppress
    the first draft — it forces a correction. Prevention is UserPromptSubmit's
    job; this is the backstop that gives the contract teeth.

Modes (``ARE_YOU_SURE_MODE``):
    off        do nothing at all
    lenient    block only on claimed verification with nothing executed
    heuristic  (default) the above, plus claims with no evidence anywhere
    strict     the above, plus absolutes, vague quantifiers, and hedge-as-fact

Other env:
    ARE_YOU_SURE_MAX_PER_TURN    challenges per user prompt (default 1)
    ARE_YOU_SURE_MAX_PER_SESSION challenge budget for a session (default 25)
    ARE_YOU_SURE_MIN_CHARS       messages shorter than this are conversational (default 120)
    ARE_YOU_SURE_STATE_DIR       default ~/.claude/are-you-sure
    ARE_YOU_SURE_LOG             default ~/.claude/logs/are-you-sure.log; "off" disables

Fails open, always. Every path is wrapped: a broken checker must never wedge a
session, so any unexpected error exits 0 and lets the agent stop.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

MODES = ("off", "lenient", "heuristic", "strict")

# Tools whose use means the agent actually looked at something this turn.
READ_TOOLS = {
    "Read", "Grep", "Glob", "NotebookRead", "WebFetch", "WebSearch",
    "Bash", "BashOutput", "LSP", "Task", "Agent",
}

# Bash that runs the system rather than merely inspecting it. Claiming
# verification without one of these in the turn is the highest-signal finding.
EXECUTION_RE = re.compile(
    r"\b(pytest|jest|vitest|mocha|tsc|eslint|ruff|mypy|go\s+test|cargo\s+(test|build|run)"
    r"|npm\s+(test|run|start|ci)|yarn\s+(test|run)|pnpm\s+(test|run)|bun\s+(test|run)"
    r"|make\b|gradle|mvn|dotnet\s+(test|run)|rspec|phpunit|python[0-9.]*\s+-m\b"
    r"|python[0-9.]*\s+\S+\.py|node\s+\S+|curl\b|psql\b|docker\s+(run|compose)"
    r"|git\s+(diff|log|show|status|bisect)|terraform\s+(plan|validate)|kubectl\b)",
    re.I,
)

# "I checked it" — the claim that most needs an execution to back it.
VERIFICATION_RE = re.compile(
    r"\b(i (?:have )?(?:verified|confirmed|tested|checked|validated)"
    r"|(?:now )?verified|confirmed working|tested (?:and|it)"
    r"|all (?:the )?tests? (?:now )?pass(?:es|ing)?|tests? (?:are )?(?:now )?passing"
    r"|(?:the )?(?:build|suite|check)s? (?:now )?pass(?:es|ing)?"
    r"|it (?:now )?works?(?: now| correctly| as expected)?"
    r"|working (?:now|correctly|as expected)|no (?:more )?errors?"
    r"|(?:this|that) fixe[sd] it|fixed and (?:verified|tested))\b",
    re.I,
)

# Statements of fact about the code or system that a reader will take as proven.
CLAIM_RE = re.compile(
    r"\b(the root cause is|root-caused|the (?:bug|issue|problem|failure) is"
    r"|(?:this|that) (?:is )?(?:caused by|because of)|the reason (?:is|was)"
    r"|(?:is|are) (?:correct|wrong|broken|safe|unused|dead|equivalent|identical)"
    r"|(?:does|do|did) not (?:exist|matter|affect|break|fire|run)"
    r"|(?:has|have) no (?:effect|callers?|references?|impact)"
    r"|(?:is|are) already (?:handled|covered|implemented|done)"
    r"|nothing (?:else )?(?:uses|references|depends on|calls)"
    r"|(?:this|the change) (?:fixes|resolves|solves|addresses|prevents)"
    r"|implementation (?:is )?complete|done and|ready to (?:merge|ship|deploy))\b",
    re.I,
)

ABSOLUTE_RE = re.compile(
    r"\b(always|never|no other|nothing else|the only|every single|in all cases"
    r"|guaranteed|impossible|cannot happen|there (?:is|are) no)\b",
    re.I,
)

VAGUE_COUNT_RE = re.compile(
    r"\b(several|a few|a couple of|many|most|numerous|a bunch of|lots of|various"
    r"|a number of|plenty of|dozens of)\b",
    re.I,
)

HEDGE_AS_FACT_RE = re.compile(
    r"\b(should (?:work|be fine|pass|fix)|presumably|i believe|i think (?:it|this|that)"
    r"|must be (?:the|a|because)|probably (?:works|fine|because)|it seems to"
    r"|appears to (?:work|be correct)|likely (?:because|the cause)|my guess)\b",
    re.I,
)

# Anything that grounds a claim: a file:line ref, a URL, a fenced block of real
# output, a test tally, a diff marker, a symbol path.
CITATION_RES = (
    re.compile(r"[\w./~-]+\.[A-Za-z0-9]+:\d+"),          # path/to/file.ts:42
    re.compile(r"https?://\S+"),
    re.compile(r"```"),
    re.compile(r"\b\d+\s+(?:passed|failed|passing|failing|errors?|warnings?|tests?)\b", re.I),
    re.compile(r"^\s*[-+]{3}\s", re.M),                   # diff header
    re.compile(r"\b(?:PASS|FAIL|OK|✓|✗)\b"),
    re.compile(r"\b[Ll]ines?\s+\d+"),
)

# The agent is handing the turn back on purpose — never trap it in a loop.
QUESTION_TOOLS = {"AskUserQuestion", "ExitPlanMode", "EnterPlanMode"}


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip())
    except (TypeError, ValueError):
        return default


def mode() -> str:
    raw = str(os.environ.get("ARE_YOU_SURE_MODE", "")).strip().lower()
    return raw if raw in MODES else "heuristic"


def state_dir() -> Path:
    raw = str(os.environ.get("ARE_YOU_SURE_STATE_DIR", "")).strip()
    return Path(raw).expanduser() if raw else Path.home() / ".claude" / "are-you-sure"


def log(message: str) -> None:
    raw = str(os.environ.get("ARE_YOU_SURE_LOG", "")).strip()
    if raw.lower() == "off":
        return
    path = Path(raw).expanduser() if raw else Path.home() / ".claude" / "logs" / "are-you-sure.log"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(message.rstrip() + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# turn state — one challenge per user prompt, a bounded budget per session
# --------------------------------------------------------------------------

def counter_path(session_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9._-]", "-", session_id or "unknown")[:120]
    return state_dir() / f"{safe}.json"


def read_counters(session_id: str) -> dict:
    try:
        return json.loads(counter_path(session_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def bump_counter(session_id: str, prompt_id: str) -> bool:
    """Record a challenge. Returns False if it could not be persisted.

    The return value gates the block itself. Both loop guards read this file, so a
    counter that cannot be written is a counter that never stops anything — and an
    unwritable state directory would otherwise mean blocking every stop forever.
    """
    path = counter_path(session_id)
    counters = read_counters(session_id)
    counters["session_total"] = int(counters.get("session_total", 0)) + 1
    turns = counters.setdefault("turns", {})
    turns[prompt_id] = int(turns.get(prompt_id, 0)) + 1
    # Keep the file from growing without bound over a long session.
    if len(turns) > 200:
        for key in list(turns)[:-50]:
            turns.pop(key, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(counters), encoding="utf-8")
        return True
    except OSError:
        return False


def budget_exhausted(session_id: str, prompt_id: str) -> str | None:
    counters = read_counters(session_id)
    per_turn = max(0, env_int("ARE_YOU_SURE_MAX_PER_TURN", 1))
    per_session = max(0, env_int("ARE_YOU_SURE_MAX_PER_SESSION", 25))
    if int(counters.get("turns", {}).get(prompt_id, 0)) >= per_turn:
        return "already challenged this turn"
    if int(counters.get("session_total", 0)) >= per_session:
        return "session challenge budget spent"
    return None


# --------------------------------------------------------------------------
# transcript — what did the agent actually do this turn?
# --------------------------------------------------------------------------

META_PREFIXES = ("<system-reminder>", "<local-command-caveat>", "<command-message>")


def is_turn_boundary(row: dict, blocks: list) -> bool:
    """True only for a genuine human prompt.

    Two kinds of ``type: "user"`` row are not the human speaking, and treating
    either as the start of the turn hides every tool call before it — which reads
    as "this turn proved nothing" and turns the checker into a false-positive
    machine:

    * tool results, which carry a ``tool_result`` block;
    * harness injections — system reminders, command caveats — flagged ``isMeta``.
    """
    if any(b.get("type") == "tool_result" for b in blocks if isinstance(b, dict)):
        return False
    if row.get("isMeta"):
        return False
    message = row.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str) and content.lstrip().startswith(META_PREFIXES):
        return False
    return True


def turn_tool_uses(transcript_path: str) -> list[dict]:
    """Tool uses since the last genuine human prompt."""
    path = Path(str(transcript_path or "").strip() or "/nonexistent")
    if not path.is_file():
        return []
    try:
        rows = [
            json.loads(line)
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line.strip()
        ]
    except (OSError, ValueError):
        return []

    uses: list[dict] = []
    for row in reversed(rows):
        if not isinstance(row, dict):
            continue
        content = row.get("message", {}).get("content") if isinstance(row.get("message"), dict) else None
        blocks = content if isinstance(content, list) else []
        if row.get("type") == "user":
            if is_turn_boundary(row, blocks):
                break
            continue
        if row.get("type") == "assistant":
            for block in blocks:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    uses.append(block)
    return uses


def last_assistant_text(transcript_path: str) -> str:
    """Fallback for runtimes that omit ``last_assistant_message``."""
    path = Path(str(transcript_path or "").strip() or "/nonexistent")
    if not path.is_file():
        return ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if not isinstance(row, dict) or row.get("type") != "assistant":
            continue
        content = row.get("message", {}).get("content") if isinstance(row.get("message"), dict) else None
        blocks = content if isinstance(content, list) else []
        text = " ".join(
            str(b.get("text", ""))
            for b in blocks
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
        if text:
            return text
    return ""


def evidence(uses: list[dict]) -> dict:
    names = [str(u.get("name", "")) for u in uses]
    bash_cmds = [
        str(u.get("input", {}).get("command", ""))
        for u in uses
        if str(u.get("name", "")) == "Bash" and isinstance(u.get("input"), dict)
    ]
    return {
        "reads": sum(1 for n in names if n in READ_TOOLS or n.startswith("mcp__")),
        "executed": any(EXECUTION_RE.search(c) for c in bash_cmds),
        "handing_off": any(n in QUESTION_TOOLS for n in names),
    }


# --------------------------------------------------------------------------
# the checks
# --------------------------------------------------------------------------

def strip_quotes(text: str) -> str:
    """Drop fenced blocks and quoted lines: they are usually someone else's words."""
    without_fences = re.sub(r"```.*?```", " ", text, flags=re.S)
    return "\n".join(l for l in without_fences.splitlines() if not l.lstrip().startswith(">"))


def cited(text: str) -> int:
    return sum(1 for pattern in CITATION_RES if pattern.search(text))


def is_handback(text: str) -> bool:
    """The agent is asking the user something — blocking would talk over them."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines:
        return True
    tail = lines[-1]
    return tail.endswith("?") or bool(re.search(r"\b(let me know|which (?:one|would)|shall i|want me to|your call)\b", tail, re.I))


def findings(message: str, ev: dict, active: str) -> list[str]:
    prose = strip_quotes(message)
    citations = cited(message)
    out: list[str] = []

    claimed = VERIFICATION_RE.search(prose)
    if claimed and not ev["executed"]:
        out.append(
            f'you wrote "{claimed.group(0).strip()}" but ran no test, build, or command '
            "this turn — nothing was verified, only read"
        )

    if active in ("heuristic", "strict"):
        claim = CLAIM_RE.search(prose)
        if claim and ev["reads"] == 0 and citations == 0:
            out.append(
                f'you assert "{claim.group(0).strip()}" having neither opened a file nor '
                "cited a source this turn"
            )
        elif claim and citations == 0:
            out.append(
                f'you assert "{claim.group(0).strip()}" with no citation — no file:line, '
                "command output, or URL a reader could check"
            )

    if active == "strict":
        absolute = ABSOLUTE_RE.search(prose)
        if absolute:
            out.append(
                f'"{absolute.group(0).strip()}" is an exhaustive claim — it needs the search '
                "that would have found a counterexample"
            )
        vague = VAGUE_COUNT_RE.search(prose)
        if vague:
            out.append(
                f'"{vague.group(0).strip()}" estimates a count you could have enumerated — '
                "give the number and what it counts"
            )
        hedge = HEDGE_AS_FACT_RE.search(prose)
        if hedge:
            out.append(
                f'"{hedge.group(0).strip()}" is a guess dressed as a conclusion — either '
                "settle it or label it a guess and say what would settle it"
            )

    return out


CHALLENGE_HEADER = "YOU SURE ABOUT THAT?  [are-you-sure]"

CHALLENGE_FOOTER = """
For each item, do exactly one of these before you finish:
  1. PROVE it — run the command or open the file, then cite it: file:line, or the
     command together with its real output. Not a paraphrase of the output.
  2. LABEL it — rewrite as INFERRED / ASSUMED / UNVERIFIED and state the one check
     that would settle it.
  3. RETRACT it — cut the claim. A shorter true answer beats a longer confident one.

Softer wording is not a fix: "should be correct" carries the same claim as "is
correct". Change what you actually assert, or go and earn it.

Do not apologise, do not narrate this check, and do not mention this hook to the
user. Correct the substance and carry on.
""".strip()


def challenge_text(items: list[str]) -> str:
    bullets = "\n".join(f"  • {item}" for item in items)
    return f"{CHALLENGE_HEADER}\n\nThis message claims more than it proved:\n{bullets}\n\n{CHALLENGE_FOOTER}"


CONTRACT = """
[are-you-sure] Evidence contract for this turn — a Stop hook enforces it:

- Every factual claim about this codebase or system must trace to something you
  actually ran or read *this turn*. Cite it as file:line, or the command and its
  real output, or a URL.
- Never write verified / confirmed / tested / works / passes unless you executed
  the check this turn. Read the code but ran nothing? Say "by inspection, not run".
- Label anything unproven INFERRED, ASSUMED, or UNVERIFIED. An unlabelled
  sentence reads as established fact.
- Give exact counts. Enumerate, or don't quantify — no "several", no "most".
- Report failures and gaps plainly, including the parts you skipped.

Hedging honestly the first time is cheaper than being sent back for a second pass.
""".strip()


# --------------------------------------------------------------------------
# dispatch
# --------------------------------------------------------------------------

def emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def handle_prompt_submit() -> None:
    emit({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": CONTRACT,
        }
    })


def handle_stop(data: dict, event: str) -> None:
    session_id = str(data.get("session_id", "") or "")
    prompt_id = str(data.get("prompt_id", "") or "no-prompt-id")

    skipped = budget_exhausted(session_id, prompt_id)
    if skipped:
        log(f"{event} allow ({skipped})")
        return

    message = str(data.get("last_assistant_message", "") or "").strip()
    if not message:
        message = last_assistant_text(str(data.get("transcript_path", "") or ""))
    if len(message) < max(0, env_int("ARE_YOU_SURE_MIN_CHARS", 120)):
        log(f"{event} allow (message too short to carry a claim)")
        return

    uses = turn_tool_uses(str(data.get("transcript_path", "") or ""))
    ev = evidence(uses)
    if ev["handing_off"] or is_handback(message):
        log(f"{event} allow (agent is handing the turn back to the user)")
        return

    items = findings(message, ev, mode())
    if not items:
        log(f"{event} allow (claims are grounded: reads={ev['reads']} executed={ev['executed']})")
        return

    if not bump_counter(session_id, prompt_id):
        log(f"{event} allow (could not record the challenge — refusing to risk a loop)")
        return

    log(f"{event} BLOCK ({len(items)}): " + " | ".join(items))
    emit({
        "hookSpecificOutput": {
            "hookEventName": event,
            "decision": "block",
            "reason": challenge_text(items),
        }
    })


def main() -> int:
    try:
        raw = sys.stdin.read()
    except OSError:
        return 0
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0

    if mode() == "off":
        return 0

    event = str(data.get("hook_event_name", "") or "")
    if event == "UserPromptSubmit":
        handle_prompt_submit()
    elif event in ("Stop", "SubagentStop"):
        handle_stop(data, event)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail open, never wedge a session
        log(f"error (failing open): {type(exc).__name__}: {exc}")
        sys.exit(0)
