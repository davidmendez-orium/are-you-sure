#!/usr/bin/env python3
"""
The dashboard: did being challenged actually make the answers better?

    are_you_sure.py dashboard [--limit N] [--rules] [--json] [--db PATH]

Reads the SQLite record written by the hook. Prints nothing it cannot count, and
says "no data yet" rather than rendering an empty frame that looks like a result.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ays_db  # noqa: E402

BAR = "█"
VERDICT_LABEL = {
    "improved": "proof arrived — a citation, or a command that ran",
    "hedged": "claim withdrawn or labelled, no new evidence",
    "unchanged": "went looking, but the claim still stands unearned",
    "ignored": "nothing earned, nothing withdrawn",
}
WIN = ("improved", "hedged")


def bar(n: int, total: int, width: int = 22) -> str:
    if total <= 0:
        return ""
    return BAR * max(1, round(width * n / total)) if n else ""


def pct(n: int, total: int) -> str:
    return f"{100 * n / total:>5.1f}%" if total else "    — "


SLIM_COLUMNS = "c.id, o.verdict, o.human_rating"

FULL_COLUMNS = (
    "c.id, c.ts, c.mode, c.rules, c.findings, c.before_text, c.before_chars,"
    " c.before_cites, c.before_exec, o.verdict, o.after_text, o.after_chars,"
    " o.after_cites, o.after_exec, o.tools_since, o.evidence_gained,"
    " o.claim_retracted, o.label_added, o.human_rating, o.note, o.findings_after"
)


def fetch(conn: sqlite3.Connection, slim: bool = False) -> dict:
    """`slim` is for the browser's refresh poll, which only compares ids and
    verdicts — no reason to ship both message texts every few seconds."""
    rows = conn.execute(
        f"SELECT {SLIM_COLUMNS if slim else FULL_COLUMNS}"
        " FROM challenges c LEFT JOIN outcomes o ON o.challenge_id = c.id"
        " ORDER BY c.id DESC"
    ).fetchall()
    return {"rows": [dict(r) for r in rows]}


def render(rows: list[dict], limit: int, show_rules: bool) -> str:
    out: list[str] = []
    total = len(rows)
    resolved = [r for r in rows if r["verdict"]]
    pending = total - len(resolved)

    out.append("ARE YOU SURE? — did the challenge improve the answer?")
    out.append("")

    if not total:
        out.append("  No challenges recorded yet.")
        out.append("")
        out.append("  The hook writes a row every time it blocks a stop, and scores the")
        out.append("  revision when the turn finishes. Nothing to show until it fires.")
        out.append(f"  Database: {ays_db.db_path()}")
        return "\n".join(out)

    counts = {v: sum(1 for r in resolved if r["verdict"] == v) for v in ays_db.VERDICTS}
    wins = sum(counts[v] for v in WIN)
    n = len(resolved)

    out.append(f"  {total} challenge{'s' if total != 1 else ''} recorded"
               f" · {n} scored · {pending} still open")
    out.append("")

    if n:
        out.append(f"  IMPROVED THE ANSWER   {pct(wins, n)}   ({wins} of {n} scored)")
        out.append("")
        for verdict in ays_db.VERDICTS:
            c = counts[verdict]
            out.append(f"    {verdict:<10} {VERDICT_LABEL[verdict]:<50} "
                       f"{c:>4}  {pct(c, n)} {bar(c, n)}")
        out.append("")

        signals = [
            ("evidence arrived (a citation or a command run)", "evidence_gained"),
            ("the claim was retracted outright", "claim_retracted"),
            ("an honest label was added", "label_added"),
        ]
        out.append("  WHAT CHANGED")
        for label, key in signals:
            c = sum(1 for r in resolved if r[key])
            out.append(f"    {label:<61} {c:>4}  {pct(c, n)}")
        went = sum(1 for r in resolved if (r["tools_since"] or 0) > 0)
        out.append(f"    {'went back to the codebase after being challenged':<61} "
                   f"{went:>4}  {pct(went, n)}")
        out.append("")

        rated = [r for r in resolved if r["human_rating"]]
        if rated:
            agree = sum(
                1 for r in rated
                if (r["human_rating"] == "improvement") == (r["verdict"] in WIN)
            )
            out.append(f"  HUMAN RATINGS   {len(rated)} of {n} scored"
                       f" · measured verdict agrees {pct(agree, len(rated)).strip()}")
            out.append("    Where they disagree, the human is right and the signals need work.")
        else:
            out.append("  HUMAN RATINGS   none yet — the measured verdict is unaudited")
            out.append("    Rate one:  /are-you-sure rate <id> improvement|no-improvement [note]")
        out.append("")

    if show_rules:
        tally: dict[str, list[int]] = {}
        for r in rows:
            try:
                for rule in json.loads(r["rules"]):
                    slot = tally.setdefault(rule, [0, 0])
                    slot[0] += 1
                    if r["verdict"] in WIN:
                        slot[1] += 1
            except (ValueError, TypeError):
                continue
        if tally:
            out.append(f"  BY RULE{'':<55}fired   win rate")
            for rule, (fired, won) in sorted(tally.items(), key=lambda kv: -kv[1][0]):
                out.append(f"    {rule:<58}{fired:>5}   {pct(won, fired)}")
            out.append("")

    out.append(f"  RECENT (newest first, up to {limit})")
    for r in rows[:limit]:
        state = r["verdict"] or "open"
        rating = f" · human: {r['human_rating']}" if r["human_rating"] else ""
        try:
            rules = ", ".join(json.loads(r["rules"]))
        except (ValueError, TypeError):
            rules = "?"
        out.append(f"    #{r['id']:<4} {r['ts'][:16].replace('T', ' ')}  {state:<10}{rating}".rstrip())
        out.append(f"          caught: {rules}")
        if r["verdict"]:
            out.append(
                f"          chars {r['before_chars']}→{r['after_chars']}"
                f" · citations {r['before_cites']}→{r['after_cites']}"
                f" · ran a command {bool(r['before_exec'])}→{bool(r['after_exec'])}"
                f" · tools after {r['tools_since']}"
            )
        if r["note"]:
            out.append(f"          note: {r['note']}")
    out.append("")
    out.append(f"  {ays_db.db_path()}")
    return "\n".join(out)


def main(argv: list[str]) -> int:
    limit, show_rules, as_json = 10, False, False
    do_serve, port, open_browser = False, 8787, True
    for i, arg in enumerate(argv):
        if arg == "--limit" and i + 1 < len(argv):
            try:
                limit = max(1, int(argv[i + 1]))
            except ValueError:
                pass
        elif arg == "--rules":
            show_rules = True
        elif arg == "--json":
            as_json = True
        elif arg == "--serve":
            do_serve = True
        elif arg == "--no-open":
            open_browser = False
        elif arg == "--port" and i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
        elif arg == "--db" and i + 1 < len(argv):
            import os

            os.environ["ARE_YOU_SURE_DB"] = argv[i + 1]

    if do_serve:
        import ays_serve

        return ays_serve.serve(port=port, open_browser=open_browser)

    conn = ays_db.connect()
    if conn is None:
        print(f"Could not open {ays_db.db_path()}")
        return 1
    try:
        data = fetch(conn)
    finally:
        conn.close()

    if as_json:
        print(json.dumps(data, indent=2))
        return 0
    print(render(data["rows"], limit, show_rules))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
