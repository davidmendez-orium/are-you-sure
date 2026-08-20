#!/usr/bin/env python3
"""
The dashboard as a local web page: ``dashboard --serve``.

Bound to **127.0.0.1 only**, deliberately. The record stores both the challenged
message and its revision so a verdict can be re-derived later, which means this
serves session content — it has no business on a network interface.

Stdlib only (``http.server``), so it inherits the plugin's no-dependency rule.
Everything from the database is escaped on the way into the page: the stored text
is model output, and model output is not trusted markup.
"""

from __future__ import annotations

import html
import json
import socket
import sys
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ays_db  # noqa: E402
import dashboard  # noqa: E402

CSS = """
:root { --bg:#0d1117; --panel:#161b22; --line:#30363d; --text:#e6edf3; --dim:#8b949e;
  --improved:#3fb950; --hedged:#58a6ff; --unchanged:#d29922; --ignored:#f85149 }
* { box-sizing:border-box }
body { margin:0; padding:32px 20px 64px; background:var(--bg); color:var(--text);
  font:14px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace }
main { max-width:900px; margin:0 auto }
h1 { font-size:19px; margin:0 0 4px }
h2 { font-size:12px; text-transform:uppercase; letter-spacing:.12em; color:var(--dim);
  margin:26px 0 10px }
.dim { color:var(--dim); font-size:12.5px }
.panel { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:18px 20px }
.big { font-size:44px; font-weight:700; line-height:1; margin-right:14px }
table { width:100%; border-collapse:collapse }
td { padding:5px 0; vertical-align:baseline; color:var(--dim) }
td.n, td.p { text-align:right; width:60px; font-variant-numeric:tabular-nums }
td.bar { width:180px; padding-left:16px }
.track { background:#21262d; border-radius:3px; height:7px; overflow:hidden }
.fill { height:100%; border-radius:3px; background:var(--dim) }
.tag { display:inline-block; padding:1px 8px; border-radius:20px; font-size:11px;
  border:1px solid var(--line); color:var(--dim) }
.row { border-top:1px solid var(--line); padding:14px 0 }
.row:first-child { border-top:0 }
.head { display:flex; gap:12px; align-items:center; flex-wrap:wrap }
b { color:var(--text); font-weight:600 }
summary { cursor:pointer; color:var(--hedged); font-size:12.5px; margin-top:9px }
pre { white-space:pre-wrap; background:var(--bg); border:1px solid var(--line);
  border-radius:6px; padding:11px 13px; margin:8px 0 0; font-size:12.5px; color:var(--dim) }
form { display:flex; gap:8px; margin-top:10px; flex-wrap:wrap }
input { flex:1; min-width:180px; padding:6px 10px; font:inherit; font-size:12.5px;
  background:var(--bg); border:1px solid var(--line); color:var(--text); border-radius:6px }
button { padding:6px 12px; font:inherit; font-size:12.5px; cursor:pointer; border-radius:6px;
  background:#21262d; border:1px solid var(--line); color:var(--text) }
button:hover { border-color:currentColor }
footer { margin-top:32px; padding-top:14px; border-top:1px solid var(--line);
  color:var(--dim); font-size:12px }
"""

POLL = """
let seen = null;
async function poll() {
  try {
    const r = await fetch('api?slim=1');
    const d = await r.json();
    const sig = d.rows.map(x => x.id + ':' + (x.verdict||'') + ':' + (x.human_rating||'')).join(',');
    if (seen !== null && sig !== seen) location.reload();
    seen = sig;
  } catch (e) {}
}
poll(); setInterval(poll, 4000);
"""

VERDICT_BLURB = {
    "improved": "proof arrived — a citation, or a command that ran",
    "hedged": "claim withdrawn or labelled, no new evidence",
    "unchanged": "went looking, but the claim still stands unearned",
    "ignored": "nothing earned, nothing withdrawn",
}


def esc(value) -> str:
    return html.escape("" if value is None else str(value))


def pct(n: int, total: int) -> str:
    return f"{100 * n / total:.1f}%" if total else "—"


def rules_of(row: dict) -> str:
    try:
        return ", ".join(json.loads(row["rules"])) or "—"
    except (ValueError, TypeError):
        return "—"


def page(rows: list[dict]) -> str:
    resolved = [r for r in rows if r["verdict"]]
    n = len(resolved)
    pending = len(rows) - n
    wins = sum(1 for r in resolved if r["verdict"] in dashboard.WIN)

    parts = [
        "<!doctype html><html><head><meta charset='utf-8'>",
        "<title>Are You Sure? — dashboard</title>",
        f"<style>{CSS}</style></head><body><main>",
        "<h1>Are you sure about that?</h1>",
        f"<p class='dim'>{len(rows)} challenge{'s' if len(rows) != 1 else ''} recorded"
        f" · {n} scored · {pending} still open</p>",
    ]

    if not rows:
        parts.append(
            "<div class='panel dim'>No challenges recorded yet. The hook writes a row "
            "every time it blocks a stop, and scores the revision when the turn "
            "finishes.</div>")
    else:
        tone = ("--improved" if wins / n >= 0.6 else "--unchanged" if wins / n >= 0.3
                else "--ignored") if n else "--dim"
        parts.append("<div class='panel'>")
        parts.append(f"<span class='big' style='color:var({tone})'>{pct(wins, n)}</span>"
                     f"improved the answer <span class='tag'>{wins} of {n} scored</span>")

        if n:
            parts.append("<h2>Breakdown</h2><table>")
            for verdict in ays_db.VERDICTS:
                c = sum(1 for r in resolved if r["verdict"] == verdict)
                width = 100 * c / n if n else 0
                parts.append(
                    f"<tr><td style='width:96px'><span class='tag' style='color:"
                    f"var(--{verdict})'>{verdict}</span></td>"
                    f"<td>{VERDICT_BLURB[verdict]}</td>"
                    f"<td class='n'>{c}</td><td class='p'>{pct(c, n)}</td>"
                    f"<td class='bar'><div class='track'><div class='fill' style='width:"
                    f"{width:.1f}%;background:var(--{verdict})'></div></div></td></tr>")
            parts.append("</table>")

            parts.append("<h2>What changed</h2><table>")
            for label, key in (
                ("evidence arrived (a citation or a command run)", "evidence_gained"),
                ("the claim was retracted outright", "claim_retracted"),
                ("an honest label was added", "label_added"),
            ):
                c = sum(1 for r in resolved if r[key])
                parts.append(f"<tr><td class='dim'>{label}</td><td class='n'>{c}</td>"
                             f"<td class='p'>{pct(c, n)}</td></tr>")
            went = sum(1 for r in resolved if (r["tools_since"] or 0) > 0)
            parts.append(f"<tr><td class='dim'>went back to the codebase after being "
                         f"challenged</td><td class='n'>{went}</td>"
                         f"<td class='p'>{pct(went, n)}</td></tr></table>")

            rated = [r for r in resolved if r["human_rating"]]
            if rated:
                agree = sum(1 for r in rated
                            if (r["human_rating"] == "improvement")
                            == (r["verdict"] in dashboard.WIN))
                parts.append(
                    f"<p class='dim'><b>{len(rated)} of {n}</b> human-rated · the measured "
                    f"verdict agrees <b>{pct(agree, len(rated))}</b>. Where they disagree the "
                    "human is right and the signals need work.</p>")
            else:
                parts.append("<p class='dim'>Nothing human-rated yet, so the measured "
                             "verdict is <b>unaudited</b> — rate a few below.</p>")
        parts.append("</div>")

        parts.append("<h2>Challenges</h2><div class='panel'>")
        for r in rows:
            verdict = r["verdict"] or "open"
            tag = f"<span class='tag' style='color:var(--{verdict})'>{verdict}</span>" if r["verdict"] else \
                "<span class='tag'>open</span>"
            parts.append("<div class='row'><div class='head'>"
                         f"<span class='dim'>#{r['id']}</span>{tag}"
                         f"<span class='dim'>{esc(r['ts'][:16].replace('T', ' '))}</span>"
                         f"<span class='dim'>caught: {esc(rules_of(r))}</span></div>")
            if r["verdict"]:
                parts.append(
                    "<div class='dim' style='margin-top:7px'>"
                    f"chars <b>{r['before_chars']}→{r['after_chars']}</b> · "
                    f"citations <b>{r['before_cites']}→{r['after_cites']}</b> · "
                    f"ran a command <b>{bool(r['before_exec'])}→{bool(r['after_exec'])}</b> · "
                    f"tools after <b>{r['tools_since']}</b></div>")
            parts.append(
                "<details><summary>before / after</summary>"
                f"<pre>{esc(r.get('before_text'))}</pre>"
                f"<pre>{esc(r.get('after_text')) or '(no revision recorded yet)'}</pre></details>")
            if r["verdict"]:
                if r["human_rating"]:
                    note = f" — {esc(r['note'])}" if r["note"] else ""
                    parts.append(f"<p class='dim'>your rating: <b>{esc(r['human_rating'])}</b>"
                                 f"{note}</p>")
                else:
                    parts.append(
                        f"<form method='post' action='rate'>"
                        f"<input type='hidden' name='id' value='{r['id']}'>"
                        "<input type='text' name='note' placeholder='what actually changed "
                        "(optional)'>"
                        "<button class='yes' name='rating' value='improvement'>improvement"
                        "</button>"
                        "<button class='no' name='rating' value='no-improvement'>"
                        "no improvement</button></form>")
            parts.append("</div>")
        parts.append("</div>")

    parts.append(f"<footer>{esc(ays_db.db_path())} · localhost only · auto-refreshes when "
                 "the record changes</footer>")
    parts.append(f"</main><script>{POLL}</script></body></html>")
    return "".join(parts)


def read_rows(slim: bool = False) -> list[dict]:
    conn = ays_db.connect()
    if conn is None:
        return []
    try:
        return dashboard.fetch(conn, slim=slim)["rows"]
    finally:
        conn.close()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # quiet; this runs in a terminal the user is using
        pass

    def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        route = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        if route == "/":
            self._send(page(read_rows()).encode(), "text/html; charset=utf-8")
        elif route == "/api":
            slim = "slim" in urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._send(json.dumps({"rows": read_rows(slim)}, default=str).encode(),
                       "application/json")
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:
        if urllib.parse.urlparse(self.path).path.rstrip("/") != "/rate":
            self._send(b"not found", "text/plain", 404)
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            ays_db.rate(int(form.get("id", ["0"])[0]),
                        form.get("rating", [""])[0],
                        form.get("note", [""])[0])
        except (ValueError, TypeError, OSError):
            pass
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Content-Length", "0")
        self.end_headers()


def free_port(preferred: int) -> int:
    for candidate in (preferred, *range(preferred + 1, preferred + 12)):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", candidate))
                return candidate
            except OSError:
                continue
    return 0  # let the OS choose


def serve(port: int = 8787, open_browser: bool = True) -> int:
    chosen = free_port(port)
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", chosen), Handler)
    except OSError as exc:
        print(f"could not start the dashboard: {exc}")
        return 1
    url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    if chosen != port:
        print(f"port {port} was busy")
    print(f"are-you-sure dashboard → {url}")
    print(f"reading {ays_db.db_path()}")
    print("Ctrl-C to stop.")
    if open_browser:
        try:
            import webbrowser

            webbrowser.open(url)
        except Exception:  # noqa: BLE001
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
    return 0
