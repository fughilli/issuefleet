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

    def __init__(
        self,
        state_dir: str | Path,
        stop_cb=None,
        config_path: str | Path | None = None,
        allow_add_project: bool = False,
        add_project_cb=None,
        project_results_cb=None,
    ):
        self.state_dir = Path(state_dir)
        self.runner = TmuxRunner(log_dir=self.state_dir / "logs")
        self.stop_cb = stop_cb
        # Add-project surface. Reads come from the config file on disk (the
        # persisted source of truth, like registry.json) so the view stays
        # independent of the tick thread's mutable cfg; the one write path is
        # delegated to add_project_cb (which enqueues on the reconciler).
        self.config_path = Path(config_path) if config_path else None
        self.allow_add_project = bool(allow_add_project and add_project_cb)
        self.add_project_cb = add_project_cb
        self.project_results_cb = project_results_cb

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

    # ---------------------------------------------------------- projects

    def projects(self) -> list[dict]:
        """The configured projects, read fresh from the config file on disk —
        the persisted truth, so a dashboard-added project shows up here once the
        tick thread has cloned it and written the entry. Empty (never raising)
        if there's no config file or it can't be read."""
        if self.config_path is None:
            return []
        from issuefleet import config as config_mod

        try:
            cfg = config_mod.load(self.config_path)
        except config_mod.ConfigError:
            return []
        out = []
        for p in cfg.projects:
            claim = (
                p.claim.strategy
                if p.claim.strategy == "agent"
                else f"{p.claim.strategy}={p.claim.value}"
            )
            out.append({
                "name": p.name,
                "linear_project": p.linear_project,
                "repo": str(p.repo),
                "git_url": p.git_url or "",
                "base_ref": p.base_ref,
                "claim": claim,
                "max_workers": p.max_workers,
            })
        return out

    def project_results(self) -> list[dict]:
        return list(self.project_results_cb()) if self.project_results_cb else []

    def add_project(self, spec: dict) -> str | None:
        """Validate a submitted project and, if it's well-formed, enqueue it for
        the tick thread to clone and persist. Returns None on success (queued),
        or a human-readable error string to show the operator. The heavy work
        (clone, config write) and its outcome land asynchronously in
        ``project_results``; this only rejects what we can see is wrong up front
        so the form can complain immediately."""
        if not self.allow_add_project:
            return "adding projects from the dashboard is disabled"
        from issuefleet import config as config_mod

        try:
            project = config_mod.parse_project(spec, "add-project form")
        except config_mod.ConfigError as e:
            return str(e)
        if any(p["name"] == project.name for p in self.projects()):
            return f"a project named {project.name!r} already exists"
        self.add_project_cb(spec)
        return None


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
button.primary { background: #14243a; color: #9ac4ff; border: 1px solid #26466a;
  padding: 8px 16px; border-radius: 6px; font: inherit; cursor: pointer; }
button.primary:hover { background: #183052; }
label { display: block; color: #8a97a8; font-size: 13px; }
input, select { width: 100%; max-width: 460px; margin-top: 4px; padding: 7px 9px;
  background: #0e141c; color: #d6dee8; border: 1px solid #23303d; border-radius: 5px;
  font: inherit; }
form p { margin: 0 0 12px; }
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
    nav = "<div class='nav'><a href='/projects'>manage projects →</a></div>"
    if not snaps:
        return _page(
            "issuefleet",
            banner + nav + "<p class='muted'>Fleet empty — no workers claimed.</p>",
        )
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
    # so reading a transcript is never yanked out from under you. Refresh to a
    # clean "/" so a one-shot ?stopped= banner doesn't stick on every reload.
    return _page("issuefleet", banner + nav + table + note).replace(
        "<head>", "<head><meta http-equiv='refresh' content='5; url=/'>", 1
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


_CLAIM_HELP = (
    "agent — claim only when the issue is delegated/@-mentioned to the bot; "
    "label / assignee / state — poll-claim by that field's value."
)


def render_projects(
    projects: list[dict],
    results: list[dict],
    allow_add: bool,
    error: str | None = None,
    added: str | None = None,
    form: dict | None = None,
) -> str:
    """The projects page: what the fleet manages now, recent add outcomes, and
    (unless disabled) a form to add one. No auto-refresh — a half-typed form
    must never be yanked out from under the operator."""
    form = form or {}
    nav = "<div class='nav'><a href='/'>← fleet</a></div>"

    banners = []
    if added:
        banners.append(
            f"<div class='banner'>Queued <b>{_h(added)}</b> — the daemon will clone it "
            "on the next tick; watch for the outcome below.</div>"
        )
    if error:
        banners.append(
            f"<div class='banner' style='background:#2a1616;border-color:#5a2b2b;"
            f"color:#ffb0b0'>Couldn't add project: {_h(error)}</div>"
        )

    if results:
        items = []
        for r in reversed(results):  # newest first
            cls = "ok" if r["ok"] else "dead"
            verb = "added" if r["ok"] else "rejected"
            items.append(
                f"<div class='ev'><div class='lbl'><span class='pill {cls}'>{verb}</span> "
                f"{_h(r['name'])}</div><pre>{_h(r['detail'])}</pre></div>"
            )
        results_card = f"<div class='card'><h2>Recent add attempts</h2>{''.join(items)}</div>"
    else:
        results_card = ""

    if projects:
        rows = "".join(
            "<tr>"
            f"<td>{_h(p['name'])}</td>"
            f"<td>{_h(p['linear_project'])}</td>"
            f"<td>{_h(p['repo'])}</td>"
            f"<td>{_h(p['claim'])}</td>"
            f"<td>{_h(p['base_ref'])}</td>"
            f"<td>{_h(p['max_workers']) if p['max_workers'] is not None else '<span class=muted>—</span>'}</td>"
            "</tr>"
            for p in projects
        )
        table = (
            "<table><thead><tr><th>Name</th><th>Linear project</th><th>Repo</th>"
            "<th>Claim</th><th>Base</th><th>Max</th></tr></thead>"
            f"<tbody>{rows}</tbody></table>"
        )
        projects_card = f"<div class='card'><h2>Projects ({len(projects)})</h2>{table}</div>"
    else:
        projects_card = (
            "<div class='card'><h2>Projects</h2>"
            "<p class='muted'>None visible (no config file backing this run, or it "
            "couldn't be read).</p></div>"
        )

    if not allow_add:
        form_card = (
            "<div class='card'><h2>Add a project</h2>"
            "<p class='muted'>Disabled ([dashboard] allow_add_project = false).</p></div>"
        )
    else:
        def val(k):
            return _h(form.get(k, ""))

        def opt(v, label):
            sel = " selected" if form.get("claim_strategy") == v else ""
            return f"<option value='{v}'{sel}>{label}</option>"

        form_card = (
            "<div class='card'><h2>Add a project</h2>"
            "<form method='post' action='/projects/add'>"
            f"<p><label>name<br><input name='name' value=\"{val('name')}\" "
            "placeholder='short-handle' required></label></p>"
            f"<p><label>linear_project<br><input name='linear_project' "
            f"value=\"{val('linear_project')}\" placeholder='Linear project name or UUID' required></label></p>"
            f"<p><label>repo<br><input name='repo' value=\"{val('repo')}\" "
            "placeholder='~/Projects/foo (clone the daemon owns)' required></label></p>"
            f"<p><label>git_url <span class='muted'>(clone from here if repo is absent)</span><br>"
            f"<input name='git_url' value=\"{val('git_url')}\" placeholder='https://github.com/owner/repo'></label></p>"
            f"<p><label>base_ref<br><input name='base_ref' value=\"{val('base_ref') or 'main'}\"></label></p>"
            "<p><label>claim strategy<br><select name='claim_strategy'>"
            + opt("agent", "agent (delegation/@-mention only)")
            + opt("label", "label")
            + opt("assignee", "assignee")
            + opt("state", "state")
            + "</select></label></p>"
            f"<p><label>claim value <span class='muted'>(label/user/state; leave blank for agent)</span><br>"
            f"<input name='claim_value' value=\"{val('claim_value')}\"></label></p>"
            f"<p><label>max_workers <span class='muted'>(optional per-project cap)</span><br>"
            f"<input name='max_workers' value=\"{val('max_workers')}\" placeholder='(global cap)'></label></p>"
            "<button type='submit' class='primary'>Add project</button>"
            f"<p class='muted' style='margin-top:10px'>{_h(_CLAIM_HELP)} The repo is cloned "
            "host-side and the entry written to the config file; it goes live on the next tick.</p>"
            "</form></div>"
        )

    return _page(
        "projects — issuefleet",
        nav + "<h2 style='margin-top:0'>Projects</h2>"
        + "".join(banners) + projects_card + results_card + form_card,
    )


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
                if path == "/projects":
                    return self._send(200, render_projects(
                        outer.view.projects(),
                        outer.view.project_results(),
                        outer.view.allow_add_project,
                        error=qs.get("error", [None])[0],
                        added=qs.get("added", [None])[0],
                    ))
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
                # Read the body first so the connection can be reused/closed
                # cleanly and so form posts (add-project) can parse it.
                length = int(self.headers.get("Content-Length", 0) or 0)
                body = self.rfile.read(length) if length else b""
                path = urllib.parse.urlparse(self.path).path
                if path == "/projects/add":
                    return self._add_project(body)
                route = _parse_worker_path(path)
                if route is None or route.sub != "stop":
                    return self._send(404, "not found", "text/plain")
                if outer.view.request_stop(route.key):
                    log.info("dashboard: stop requested for %s", route.key)
                    return self._redirect(f"/?stopped={urllib.parse.quote(route.key)}")
                return self._send(404, _page("not found",
                    f"<p class='muted'>No worker <b>{_h(route.key)}</b> to stop.</p>"))

            def _add_project(self, body: bytes):
                form = {
                    k: v[0].strip()
                    for k, v in urllib.parse.parse_qs(body.decode("utf-8", "replace")).items()
                }
                # Assemble the project table parse_project expects. Empty
                # optional fields are dropped so their defaults apply.
                spec: dict = {
                    "name": form.get("name", ""),
                    "linear_project": form.get("linear_project", ""),
                    "repo": form.get("repo", ""),
                }
                for opt_key in ("git_url", "base_ref", "max_workers"):
                    if form.get(opt_key):
                        spec[opt_key] = form[opt_key]
                strategy = form.get("claim_strategy", "agent")
                claim = {"strategy": strategy}
                if strategy != "agent":
                    claim["value"] = form.get("claim_value", "")
                spec["claim"] = claim
                err = outer.view.add_project(spec)
                if err is None:
                    log.info("dashboard: add-project queued for %r", spec["name"])
                    return self._redirect(
                        f"/projects?added={urllib.parse.quote(spec['name'])}"
                    )
                return self._redirect(f"/projects?error={urllib.parse.quote(err)}")

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
