#!/usr/bin/env python3
"""
SQLite record of every challenge and what the agent did about it.

A challenge is written when the hook blocks. Its outcome is written when the same
turn stops again — that second stop *is* the revision, so the pair is the whole
experiment: the message that claimed too much, and the message that replaced it.

**The verdict is measured, not asked for.** Nothing here consults a model about
whether its own rewrite was better; that self-grade is precisely the unearned
claim this plugin exists to catch. The four signals are countable:

    evidence_gained    citations went up, or a command ran that had not run before
    claim_retracted    the exact phrase that triggered the challenge is gone
    label_added        an INFERRED / ASSUMED / UNVERIFIED / not-run label appeared
    findings_after     re-running the checks on the revision — did they clear?

``human_rating`` stays NULL until a person fills it in, and the dashboard reports
how often the measured verdict and the human agree. Where they disagree, the
human is right and the signals need work.

Every function swallows its own errors. Telemetry must never be able to break the
hook it is measuring.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS challenges (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    session_id    TEXT    NOT NULL,
    prompt_id     TEXT    NOT NULL,
    event         TEXT    NOT NULL,
    mode          TEXT    NOT NULL,
    cwd           TEXT,
    rules         TEXT    NOT NULL,
    findings      TEXT    NOT NULL,
    before_text   TEXT    NOT NULL,
    before_chars  INTEGER NOT NULL,
    before_cites  INTEGER NOT NULL,
    before_reads  INTEGER NOT NULL,
    before_exec   INTEGER NOT NULL,
    before_tools  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS outcomes (
    challenge_id    INTEGER PRIMARY KEY REFERENCES challenges(id),
    ts              TEXT    NOT NULL,
    after_text      TEXT    NOT NULL,
    after_chars     INTEGER NOT NULL,
    after_cites     INTEGER NOT NULL,
    after_reads     INTEGER NOT NULL,
    after_exec      INTEGER NOT NULL,
    tools_since     INTEGER NOT NULL,
    findings_after  TEXT    NOT NULL,
    evidence_gained INTEGER NOT NULL,
    claim_retracted INTEGER NOT NULL,
    label_added     INTEGER NOT NULL,
    verdict         TEXT    NOT NULL,
    human_rating    TEXT,
    note            TEXT
);

CREATE INDEX IF NOT EXISTS idx_open ON challenges (session_id, prompt_id);
"""

VERDICTS = ("improved", "hedged", "unchanged", "ignored")
HUMAN_RATINGS = ("improvement", "no-improvement")


def db_path() -> Path:
    raw = str(os.environ.get("ARE_YOU_SURE_DB", "")).strip()
    if raw:
        return Path(raw).expanduser()
    state = str(os.environ.get("ARE_YOU_SURE_STATE_DIR", "")).strip()
    base = Path(state).expanduser() if state else Path.home() / ".claude" / "are-you-sure"
    return base / "challenges.db"


def connect() -> sqlite3.Connection | None:
    try:
        path = db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(path), timeout=5)
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA)
        return conn
    except (sqlite3.Error, OSError):
        return None


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record_challenge(
    *, session_id: str, prompt_id: str, event: str, mode: str, cwd: str,
    rules: list[str], findings: list[str], text: str,
    cites: int, reads: int, executed: bool, tools: int,
) -> int | None:
    conn = connect()
    if conn is None:
        return None
    try:
        with conn:
            cur = conn.execute(
                "INSERT INTO challenges (ts, session_id, prompt_id, event, mode, cwd,"
                " rules, findings, before_text, before_chars, before_cites, before_reads,"
                " before_exec, before_tools) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (now(), session_id, prompt_id, event, mode, cwd,
                 json.dumps(rules), json.dumps(findings), text, len(text),
                 cites, reads, int(executed), tools),
            )
        return int(cur.lastrowid)
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def open_challenge(session_id: str, prompt_id: str) -> sqlite3.Row | None:
    """The most recent challenge for this turn that has no outcome yet."""
    conn = connect()
    if conn is None:
        return None
    try:
        return conn.execute(
            "SELECT c.* FROM challenges c LEFT JOIN outcomes o ON o.challenge_id = c.id"
            " WHERE c.session_id = ? AND c.prompt_id = ? AND o.challenge_id IS NULL"
            " ORDER BY c.id DESC LIMIT 1",
            (session_id, prompt_id),
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def verdict_for(*, findings_after: list[str], evidence_gained: bool,
                claim_retracted: bool, label_added: bool) -> str:
    """Measured, in priority order. **Evidence is the only thing that earns
    ``improved``.**

    ``improved``   the checks clear and proof arrived — a citation appeared, or a
                   command ran that had not run before.
    ``hedged``     the checks clear because the claim was withdrawn or labelled,
                   with no new evidence. Weaker, but truer, so it counts as a win.
    ``unchanged``  the checks still fire, though something did change. The bucket
                   worth reading by hand.
    ``ignored``    the checks still fire and nothing was earned or withdrawn.

    Retraction sits under ``hedged`` rather than ``improved`` deliberately. Almost
    every honest hedge also deletes the phrase that triggered the challenge, so
    crediting retraction as proof would quietly file most hedges as evidence and
    leave the ``hedged`` bucket permanently near-empty — the headline win rate
    would be right while its breakdown lied about how the wins were earned.
    """
    cleared = not findings_after
    if cleared and evidence_gained:
        return "improved"
    if cleared and (label_added or claim_retracted):
        return "hedged"
    if evidence_gained or claim_retracted or label_added:
        return "unchanged"
    return "ignored"


def record_outcome(
    *, challenge_id: int, text: str, cites: int, reads: int, executed: bool,
    tools_since: int, findings_after: list[str], evidence_gained: bool,
    claim_retracted: bool, label_added: bool,
) -> str | None:
    verdict = verdict_for(
        findings_after=findings_after, evidence_gained=evidence_gained,
        claim_retracted=claim_retracted, label_added=label_added,
    )
    conn = connect()
    if conn is None:
        return None
    try:
        with conn:
            conn.execute(
                "INSERT OR REPLACE INTO outcomes (challenge_id, ts, after_text,"
                " after_chars, after_cites, after_reads, after_exec, tools_since,"
                " findings_after, evidence_gained, claim_retracted, label_added, verdict)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (challenge_id, now(), text, len(text), cites, reads, int(executed),
                 tools_since, json.dumps(findings_after), int(evidence_gained),
                 int(claim_retracted), int(label_added), verdict),
            )
        return verdict
    except sqlite3.Error:
        return None
    finally:
        conn.close()


def rate(challenge_id: int, rating: str, note: str = "") -> str:
    if rating not in HUMAN_RATINGS:
        return f"rating must be one of {', '.join(HUMAN_RATINGS)}"
    conn = connect()
    if conn is None:
        return "could not open the database"
    try:
        with conn:
            cur = conn.execute(
                "UPDATE outcomes SET human_rating = ?, note = ? WHERE challenge_id = ?",
                (rating, note or None, challenge_id),
            )
        if not cur.rowcount:
            return f"no resolved challenge #{challenge_id} to rate"
        return f"challenge #{challenge_id} rated {rating}"
    except sqlite3.Error as exc:
        return f"could not record the rating: {exc}"
    finally:
        conn.close()
