"""Introspection web UI: a read-mostly HTTP dashboard for a running fleet.

Served as a thread inside the daemon (``issuefleet run``), like the webhook
listener. It shows every active worker, lets an operator read a worker's live
transcript and mailbox, and offers one mutating action — winding a worker
down. That action follows the same rule as webhooks: the request thread does
no fleet mutation itself. ``POST /worker/<KEY>/stop`` only enqueues the key on
the reconciler and wakes the tick; the single tick thread runs the real
``_wind_down``. So the web server never races the reconcile loop over the
registry or a git worktree, and it needs no credentials of its own.

Everything the UI reads it reads from the state dir (its own ``Registry`` +
``TmuxRunner`` instances, independent of the tick thread's mutable objects)
and from each worker's ``.agent`` dir. Bind loopback and put a *private*
tunnel in front — never a public Funnel, since Stop is a real control.
"""

from __future__ import annotations

import html
import json
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import logging
import time

from issuefleet.mailbox import Mailbox
from issuefleet.model import WorkerRecord
from issuefleet.registry import Registry
from issuefleet.runner import TmuxRunner

log = logging.getLogger("issuefleet.dashboard")


# --------------------------------------------------------------- snapshots


def worker_snapshot(rec: WorkerRecord, runner: TmuxRunner) -> dict:
    """One worker's live state, assembled from the registry record plus the
    worktree's ``.agent`` dir. The single source of truth for both the CLI
    ``status`` output and the web dashboard, so the two never drift."""
    agent_dir = Path(rec.worktree) / ".agent"
    turn_phase = None
    turns_taken = auto_turns = max_auto_turns = None
    try:
        state = json.loads((agent_dir / "state.json").read_text())
        turn_phase = state.get("phase")
        turns_taken = state.get("turns_taken", 0)
        auto_turns = state.get("auto_turns", 0)
        max_auto_turns = state.get("max_auto_turns")
    except (OSError, json.JSONDecodeError):
        pass

    mb = Mailbox(agent_dir / "mailbox")
    newest = max(
        (f.stat().st_mtime for f in (agent_dir / "logs").glob("turn-*")), default=None
    )
    last_activity_s = int(time.time() - newest) if newest else None

    return {
        "issue_key": rec.issue_key,
        "issue_title": rec.issue_title,
        "issue_url": rec.issue_url,
        "project": rec.project,
        "phase": rec.phase,
        "alive": runner.alive(rec),
        "turn_phase": turn_phase,
        "turns_taken": turns_taken,
        "auto_turns": auto_turns,
        "max_auto_turns": max_auto_turns,
        "pr_number": rec.pr_number,
        "pr_url": rec.pr_url,
        "restarts": rec.restarts,
        "last_activity_s": last_activity_s,
        "branch": rec.branch,
        "worktree": rec.worktree,
        "tmux_session": rec.tmux_session,
        "outbox_pending": len(mb.pending_outbox()),
        "inbox_pending": len(mb.pending_inbox()),
        "claim_origin": rec.claim_origin,
        "created_at": rec.created_at,
        "updated_at": rec.updated_at,
    }


def turn_files(agent_dir: Path) -> list[int]:
    """Sorted turn indices with a log on disk (turn-0001.jsonl -> 1)."""
    out = []
    for f in (agent_dir / "logs").glob("turn-*.jsonl"):
        try:
            out.append(int(f.stem.split("-")[1]))
        except (IndexError, ValueError):
            continue
    return sorted(out)


def _tool_result_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts)
    return json.dumps(content)


def _events_from(ev: dict) -> list[dict]:
    kind = ev.get("type")
    if kind == "system" and ev.get("subtype") == "init":
        return [{
            "kind": "system",
            "model": ev.get("model", "?"),
            "session_id": str(ev.get("session_id", ""))[:8],
        }]
    if kind == "assistant":
        out = []
        for block in ev.get("message", {}).get("content", []):
            btype = block.get("type")
            if btype == "text" and block.get("text", "").strip():
                out.append({"kind": "text", "text": block["text"]})
            elif btype == "tool_use":
                out.append({
                    "kind": "tool_use",
                    "name": block.get("name", "?"),
                    "input": block.get("input", {}),
                })
        return out
    if kind == "user":
        out = []
        for block in ev.get("message", {}).get("content", []):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                out.append({
                    "kind": "tool_result",
                    "text": _tool_result_text(block.get("content", "")),
                    "is_error": bool(block.get("is_error")),
                })
        return out
    if kind == "result":
        return [{
            "kind": "result",
            "is_error": bool(ev.get("is_error")),
            "duration_ms": ev.get("duration_ms"),
            "cost": ev.get("total_cost_usd"),
        }]
    return []


def parse_transcript(path: Path) -> list[dict]:
    """Turn one ``turn-NNNN.jsonl`` stream-json log into structured events for
    display. Tolerant of a partially written final line (a live turn) and of
    non-JSON crash output."""
    events: list[dict] = []
    try:
        text = path.read_text()
    except OSError:
        return events
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            events.append({"kind": "raw", "text": line})
            continue
        events.extend(_events_from(ev))
    return events


class FleetView:
    """Read-side of the dashboard: its own Registry + TmuxRunner over the
    state dir, so it never shares mutable state with the tick thread. The one
    write path — Stop — is delegated to ``stop_cb`` (which enqueues on the
    reconciler); the view itself mutates nothing."""

    def __init__(self, state_dir: str | Path, stop_cb=None):
        self.state_dir = Path(state_dir)
        self.runner = TmuxRunner(log_dir=self.state_dir / "logs")
        self.stop_cb = stop_cb

    def _registry(self) -> Registry:
        # Fresh read each request: registry.json is the source of truth and
        # the tick thread rewrites it. A short-lived instance sees the latest.
        return Registry(self.state_dir)

    def workers(self) -> list[dict]:
        return [worker_snapshot(rec, self.runner) for rec in self._registry().all()]

    def find(self, key: str) -> WorkerRecord | None:
        for rec in self._registry().all():
            if rec.issue_key.lower() == key.lower():
                return rec
        return None

    def snapshot(self, key: str) -> dict | None:
        rec = self.find(key)
        return worker_snapshot(rec, self.runner) if rec else None

    def mailbox(self, key: str) -> dict | None:
        rec = self.find(key)
        if rec is None:
            return None
        mb = Mailbox(Path(rec.worktree) / ".agent" / "mailbox")
        return {
            "inbox": [{"seq": m.seq, "kind": m.kind, "ts": m.ts, "payload": m.payload}
                      for m in mb.pending_inbox()],
            "outbox": [{"seq": m.seq, "kind": m.kind, "ts": m.ts, "payload": m.payload}
                       for m in mb.pending_outbox()],
        }

    def turns(self, key: str) -> list[int] | None:
        rec = self.find(key)
        if rec is None:
            return None
        return turn_files(Path(rec.worktree) / ".agent")

    def transcript(self, key: str, n: int) -> list[dict] | None:
        rec = self.find(key)
        if rec is None:
            return None
        path = Path(rec.worktree) / ".agent" / "logs" / f"turn-{n:04d}.jsonl"
        if not path.is_file():
            return None
        return parse_transcript(path)

    def raw_turn(self, key: str, n: int) -> str | None:
        rec = self.find(key)
        if rec is None:
            return None
        path = Path(rec.worktree) / ".agent" / "logs" / f"turn-{n:04d}.jsonl"
        try:
            return path.read_text()
        except OSError:
            return None

    def pane_log_tail(self, key: str, max_bytes: int = 20_000) -> str | None:
        rec = self.find(key)
        if rec is None:
            return None
        path = self.runner.log_path(rec)
        try:
            data = path.read_bytes()
        except OSError:
            return ""
        return data[-max_bytes:].decode("utf-8", "replace")

    def request_stop(self, key: str) -> bool:
        """Queue a wind-down for the tick thread. Returns False if there is no
        such worker or no stop channel wired up."""
        if self.stop_cb is None or self.find(key) is None:
            return False
        self.stop_cb(key)
        return True


# ------------------------------------------------------------------- HTML

_CSS = """
:root { color-scheme: dark; }
* { box-sizing: border-box; }
body { margin: 0; font: 15px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
  background: #0e1116; color: #d6dee8; }
a { color: #7cc4ff; text-decoration: none; } a:hover { text-decoration: underline; }
header { padding: 18px 24px; border-bottom: 1px solid #222b36; display: flex;
  align-items: baseline; gap: 16px; }
header h1 { margin: 0; font-size: 18px; letter-spacing: .5px; }
header .sub { color: #6b7686; font-size: 13px; }
main { padding: 24px; max-width: 1100px; margin: 0 auto; }
.banner { background: #1c2a1c; border: 1px solid #2f5030; color: #a8e0a0;
  padding: 10px 14px; border-radius: 6px; margin-bottom: 18px; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 9px 12px; border-bottom: 1px solid #1c242f;
  vertical-align: top; }
th { color: #6b7686; font-weight: 600; font-size: 12px; text-transform: uppercase;
  letter-spacing: .5px; }
tr:hover td { background: #141a22; }
.pill { display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 12px;
  border: 1px solid #2b3644; }
.ok { color: #7ee081; border-color: #2f5030; background: #12200f; }
.dead { color: #ff8f8f; border-color: #5a2b2b; background: #200f0f; }
.warn { color: #ffd479; border-color: #5a4b2b; background: #201c0f; }
.muted { color: #6b7686; }
.card { background: #12171f; border: 1px solid #1f2833; border-radius: 8px;
  padding: 16px 18px; margin-bottom: 18px; }
.card h2 { margin: 0 0 12px; font-size: 14px; text-transform: uppercase;
  letter-spacing: .5px; color: #8a97a8; }
dl { display: grid; grid-template-columns: max-content 1fr; gap: 4px 18px; margin: 0; }
dt { color: #6b7686; } dd { margin: 0; word-break: break-all; }
button.danger { background: #3a1414; color: #ff9a9a; border: 1px solid #6a2626;
  padding: 8px 16px; border-radius: 6px; font: inherit; cursor: pointer; }
button.danger:hover { background: #521818; }
.ev { border-left: 3px solid #2b3644; padding: 6px 0 6px 14px; margin: 10px 0; }
.ev .lbl { font-size: 11px; text-transform: uppercase; letter-spacing: .5px;
  color: #6b7686; margin-bottom: 3px; }
.ev.text { border-color: #3d6b9e; } .ev.tool { border-color: #9e7a3d; }
.ev.result { border-color: #4d9e4a; } .ev.tool_result { border-color: #4a5a6a; }
.ev.tool_result.err, .ev.result.err { border-color: #9e4a4a; }
pre { white-space: pre-wrap; word-break: break-word; margin: 0; font: inherit; }
.turns a { display: inline-block; padding: 4px 10px; margin: 0 6px 6px 0;
  background: #161d27; border: 1px solid #23303d; border-radius: 5px; }
.nav { margin-bottom: 16px; } .nav a { margin-right: 14px; }
"""


def _h(s) -> str:
    return html.escape("" if s is None else str(s))


def _page(title: str, body: str) -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        f"<title>{_h(title)}</title><style>{_CSS}</style></head><body>"
        "<header><h1>issuefleet</h1>"
        "<span class='sub'>fleet introspection</span></header>"
        f"<main>{body}</main></body></html>"
    )


def _alive_pill(snap: dict) -> str:
    if snap["phase"] == "crashed":
        return "<span class='pill dead'>crashed</span>"
    if snap["alive"]:
        return "<span class='pill ok'>alive</span>"
    return "<span class='pill dead'>dead</span>"


def _activity(snap: dict) -> str:
    s = snap["last_activity_s"]
    if s is None:
        return "<span class='muted'>no turns yet</span>"
    return f"{s}s ago"


def render_index(snaps: list[dict], stopped: str | None = None) -> str:
    banner = ""
    if stopped:
        banner = (
            f"<div class='banner'>Stop requested for <b>{_h(stopped)}</b> — it will "
            "wind down on the next reconcile tick. Refresh in a moment.</div>"
        )
    if not snaps:
        return _page("issuefleet", banner + "<p class='muted'>Fleet empty — no workers claimed.</p>")
    rows = []
    for s in snaps:
        turn = (
            f"{_h(s['turn_phase'])} · turn {_h(s['turns_taken'])} · "
            f"auto {_h(s['auto_turns'])}/{_h(s['max_auto_turns'])}"
            if s["turn_phase"] else "<span class='muted'>no state</span>"
        )
        pr = (
            f"<a href='{_h(s['pr_url'])}'>#{_h(s['pr_number'])}</a>"
            if s["pr_number"] else "<span class='muted'>—</span>"
        )
        rows.append(
            "<tr>"
            f"<td><a href='/worker/{_h(s['issue_key'])}'>{_h(s['issue_key'])}</a><br>"
            f"<span class='muted'>{_h(s['issue_title'])}</span></td>"
            f"<td>{_h(s['project'])}</td>"
            f"<td>{_alive_pill(s)}</td>"
            f"<td>{turn}</td>"
            f"<td>{pr}</td>"
            f"<td>{_activity(s)}</td>"
            f"<td>{_h(s['inbox_pending'])}/{_h(s['outbox_pending'])}</td>"
            "</tr>"
        )
    table = (
        "<table><thead><tr><th>Issue</th><th>Project</th><th>Session</th>"
        "<th>Agent</th><th>PR</th><th>Last activity</th><th>In/Out</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )
    note = "<p class='muted' style='margin-top:16px'>Auto-refreshing every 5s.</p>"
    # Meta-refresh keeps the list live without JS; individual pages don't refresh
    # so reading a transcript is never yanked out from under you.
    return _page("issuefleet", banner + table + note).replace(
        "<head>", "<head><meta http-equiv='refresh' content='5'>", 1
    )


def _mailbox_html(mailbox: dict | None) -> str:
    if not mailbox:
        return ""
    def block(name, msgs):
        if not msgs:
            return f"<p class='muted'>{name}: empty</p>"
        items = "".join(
            f"<div class='ev'><div class='lbl'>{_h(m['kind'])} · seq {_h(m['seq'])} · "
            f"{_h(m['ts'])}</div><pre>{_h(json.dumps(m['payload'], indent=2))}</pre></div>"
            for m in msgs
        )
        return f"<h2>{name} ({len(msgs)})</h2>{items}"
    return (
        "<div class='card'>"
        + block("Inbox (pending)", mailbox["inbox"])
        + block("Outbox (pending relay)", mailbox["outbox"])
        + "</div>"
    )


def render_worker(snap: dict, mailbox: dict | None, turns: list[int], pane_tail: str) -> str:
    kv = {
        "Title": snap["issue_title"],
        "Linear": f"<a href='{_h(snap['issue_url'])}'>{_h(snap['issue_url'])}</a>",
        "Project": snap["project"],
        "Session": _alive_pill(snap) + f" · phase {_h(snap['phase'])} · claim {_h(snap['claim_origin'])}",
        "Agent": (
            f"{_h(snap['turn_phase'])} · turn {_h(snap['turns_taken'])} · "
            f"auto {_h(snap['auto_turns'])}/{_h(snap['max_auto_turns'])}"
            if snap["turn_phase"] else "no state"
        ),
        "PR": (
            f"<a href='{_h(snap['pr_url'])}'>#{_h(snap['pr_number'])}</a>"
            if snap["pr_number"] else "—"
        ),
        "Restarts": snap["restarts"],
        "Last activity": _activity(snap),
        "Branch": snap["branch"],
        "Worktree": snap["worktree"],
        "tmux": f"tmux attach -t {_h(snap['tmux_session'])}",
    }
    dl = "".join(f"<dt>{_h(k)}</dt><dd>{v}</dd>" for k, v in kv.items())
    status_card = f"<div class='card'><h2>Status</h2><dl>{dl}</dl></div>"

    if turns:
        links = "".join(
            f"<a href='/worker/{_h(snap['issue_key'])}/turn/{n}'>turn {n}</a>" for n in turns
        )
        turns_card = f"<div class='card'><h2>Transcript ({len(turns)} turns)</h2><div class='turns'>{links}</div></div>"
    else:
        turns_card = "<div class='card'><h2>Transcript</h2><p class='muted'>No turns logged yet.</p></div>"

    pane_card = (
        "<div class='card'><h2>Pane log (tail)</h2>"
        f"<pre>{_h(pane_tail) or '<span class=muted>empty</span>'}</pre></div>"
    )

    key = _h(snap["issue_key"])
    confirm = (
        f"Stop worker {snap['issue_key']}? This winds it down: the container is killed "
        "and the worktree removed. The branch and an archived transcript are kept."
    )
    stop_card = (
        "<div class='card'><h2>Lifecycle</h2>"
        f"<form method='post' action='/worker/{key}/stop' "
        f"onsubmit=\"return confirm('{_h(confirm)}')\">"
        "<button type='submit' class='danger'>Stop worker</button>"
        "</form>"
        "<p class='muted' style='margin-top:10px'>Enqueues a wind-down for the reconcile "
        "loop; the container is stopped, the worktree removed, the branch and transcript kept.</p>"
        "</div>"
    )

    nav = f"<div class='nav'><a href='/'>← fleet</a></div>"
    return _page(
        f"{snap['issue_key']} — issuefleet",
        nav + f"<h2 style='margin-top:0'>{key}</h2>"
        + status_card + _mailbox_html(mailbox) + turns_card + pane_card + stop_card,
    )


def render_transcript(key: str, n: int, events: list[dict]) -> str:
    nav = (
        f"<div class='nav'><a href='/'>← fleet</a>"
        f"<a href='/worker/{_h(key)}'>← {_h(key)}</a>"
        f"<a href='/worker/{_h(key)}/raw/{n}'>raw jsonl</a></div>"
    )
    if not events:
        return _page(f"{key} turn {n}", nav + "<p class='muted'>No events (empty or unreadable log).</p>")
    blocks = []
    for ev in events:
        k = ev["kind"]
        if k == "system":
            blocks.append(
                f"<div class='ev'><div class='lbl'>session</div>"
                f"model {_h(ev['model'])} · {_h(ev['session_id'])}</div>"
            )
        elif k == "text":
            blocks.append(
                f"<div class='ev text'><div class='lbl'>assistant</div>"
                f"<pre>{_h(ev['text'])}</pre></div>"
            )
        elif k == "tool_use":
            blocks.append(
                f"<div class='ev tool'><div class='lbl'>→ {_h(ev['name'])}</div>"
                f"<pre>{_h(json.dumps(ev['input'], indent=2))}</pre></div>"
            )
        elif k == "tool_result":
            cls = "tool_result err" if ev["is_error"] else "tool_result"
            text = ev["text"]
            if len(text) > 4000:
                text = text[:4000] + "\n… (truncated)"
            blocks.append(
                f"<div class='ev {cls}'><div class='lbl'>result{' (error)' if ev['is_error'] else ''}</div>"
                f"<pre>{_h(text)}</pre></div>"
            )
        elif k == "result":
            cls = "result err" if ev["is_error"] else "result"
            bits = ["✗ turn errored" if ev["is_error"] else "✓ turn complete"]
            if ev.get("duration_ms"):
                bits.append(f"{ev['duration_ms'] / 1000:.0f}s")
            if ev.get("cost") is not None:
                bits.append(f"${ev['cost']:.2f}")
            blocks.append(
                f"<div class='ev {cls}'><div class='lbl'>result</div>{_h(' · '.join(bits))}</div>"
            )
        elif k == "raw":
            blocks.append(f"<div class='ev'><pre>{_h(ev['text'])}</pre></div>")
    return _page(f"{key} turn {n}", nav + f"<h2 style='margin-top:0'>{_h(key)} · turn {n}</h2>" + "".join(blocks))


# ----------------------------------------------------------------- server


@dataclass
class _Route:
    key: str
    n: int | None = None
    sub: str | None = None


def _parse_worker_path(path: str) -> _Route | None:
    """/worker/<KEY>[/turn/<N> | /raw/<N> | /stop]."""
    parts = [p for p in path.split("/") if p]
    if not parts or parts[0] != "worker" or len(parts) < 2:
        return None
    key = urllib.parse.unquote(parts[1])
    if len(parts) == 2:
        return _Route(key=key)
    if len(parts) == 3 and parts[2] == "stop":
        return _Route(key=key, sub="stop")
    if len(parts) == 4 and parts[2] in ("turn", "raw"):
        try:
            return _Route(key=key, n=int(parts[3]), sub=parts[2])
        except ValueError:
            return None
    return None


class DashboardServer:
    """Threaded HTTP dashboard over a FleetView. Mirrors WebhookServer's
    start/stop/port surface so the daemon manages both the same way."""

    def __init__(self, bind: str, port: int, view: FleetView):
        self.view = view
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                log.debug("http: " + fmt, *args)

            def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8") -> None:
                data = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _redirect(self, location: str) -> None:
                self.send_response(303)
                self.send_header("Location", location)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                path, qs = parsed.path, urllib.parse.parse_qs(parsed.query)
                if path == "/healthz":
                    return self._send(200, "ok", "text/plain")
                if path == "/":
                    return self._send(200, render_index(outer.view.workers(), qs.get("stopped", [None])[0]))
                if path == "/api/workers":
                    return self._send(200, json.dumps(outer.view.workers(), indent=2), "application/json")
                route = _parse_worker_path(path)
                if route is None:
                    return self._send(404, _page("not found", "<p class='muted'>Not found. <a href='/'>Fleet</a></p>"))
                return self._get_worker(route)

            def _get_worker(self, route: _Route):
                snap = outer.view.snapshot(route.key)
                if snap is None:
                    return self._send(404, _page("not found",
                        f"<p class='muted'>No worker <b>{_h(route.key)}</b>. <a href='/'>Fleet</a></p>"))
                if route.sub == "raw" and route.n is not None:
                    raw = outer.view.raw_turn(route.key, route.n)
                    if raw is None:
                        return self._send(404, "no such turn", "text/plain")
                    return self._send(200, raw, "text/plain; charset=utf-8")
                if route.sub == "turn" and route.n is not None:
                    events = outer.view.transcript(route.key, route.n)
                    if events is None:
                        return self._send(404, _page("not found", "<p class='muted'>No such turn.</p>"))
                    return self._send(200, render_transcript(route.key, route.n, events))
                if route.sub is None:
                    return self._send(200, render_worker(
                        snap,
                        outer.view.mailbox(route.key),
                        outer.view.turns(route.key) or [],
                        outer.view.pane_log_tail(route.key) or "",
                    ))
                return self._send(404, _page("not found", "<p class='muted'>Not found.</p>"))

            def do_POST(self):
                # Drain any body so the connection can be reused/closed cleanly.
                length = int(self.headers.get("Content-Length", 0) or 0)
                if length:
                    self.rfile.read(length)
                route = _parse_worker_path(urllib.parse.urlparse(self.path).path)
                if route is None or route.sub != "stop":
                    return self._send(404, "not found", "text/plain")
                if outer.view.request_stop(route.key):
                    log.info("dashboard: stop requested for %s", route.key)
                    return self._redirect(f"/?stopped={urllib.parse.quote(route.key)}")
                return self._send(404, _page("not found",
                    f"<p class='muted'>No worker <b>{_h(route.key)}</b> to stop.</p>"))

        self._server = ThreadingHTTPServer((bind, port), Handler)
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        return self._server.server_address[1]

    def start(self) -> "DashboardServer":
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="issuefleet-dashboard", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
