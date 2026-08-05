# issuefleet

A generic, restart-safe daemon that drains a **Linear** work queue into
**GitHub pull requests** using a fleet of autonomous coding agents — one
issue ⇒ one branch ⇒ one git worktree ⇒ one container. Label an issue,
watch an agent claim it, discuss its plan in the issue thread, review its
PR, merge; the worker is torn down and the next issue in the queue gets its
slot. Works for any (Linear project → GitHub repo) pair, several at once,
configured declaratively.

## The load-bearing idea: credentials never enter the agents

Agent containers run with permission prompts disabled. Handing such a
sandbox your Linear key and a GitHub token would be a bad trade, so **all
credentials live host-side, in the orchestrator process; agent containers
get none.**

```
                 HOST (credentialed)                          CONTAINERS (no credentials)
┌───────────────────────────────────────────────┐      ┌──────────────────────────────────┐
│  issuefleet daemon (reconcile loop, poll 60s) │      │ tmux: issuefleet-<proj>-<KEY>    │
│                                               │      │  └─ claude-container             │
│   Linear GraphQL ◄──── relay ────┐            │      │      └─ turnloop → claude -p     │
│   GitHub REST    ◄── (dedup'd) ──┤            │      │           │                      │
│   git push (app │token, HTTPS) ◄─┤            │      │           ▼ agentctl             │
│                                  │            │      │   <worktree>/.agent/mailbox/     │
│   registry.json (durable fleet state)         │◄─────┼── outbox/ status|question|ready| │
│                                               │      │           file_issue             │
│   inbox writes ──────────────────────────────►│──────┼─► inbox/  reply|pr_feedback|...  │
└───────────────────────────────────────────────┘      └──────────────────────────────────┘
```

A worker's **only** channel to the world is a filesystem mailbox inside its
worktree: it writes `status` / `question` / `ready` / `file_issue` JSON
messages to an outbox; the orchestrator polls those and performs the
credentialed act (post a Linear comment, push the branch, open the PR, or
**file a new Linear issue**). Inbound Linear comments and PR review feedback
are written to the worker's inbox and injected into its next turn. Agents
commit freely — a linked worktree's objects land in the shared `.git` on the
host — but nothing leaves the machine until the orchestrator pushes it.

**Acknowledging input.** Every piece of user input is acknowledged so the
sender is never left wondering whether it landed: the orchestrator drops a
👀 into the agent session the instant it routes a prompt or comment to a
worker (before any turn runs, so busy/restarting workers still ack
immediately), the worker emits ⚙️ when it starts taking turns on it, and ✅
when it settles. In personal-key/comment mode the 👀 falls back to a deduped
comment; the ⚙️/✅ are session-only to avoid thread spam.

**Authoring issues.** Delegate (or @-mention) the bot on an issue such as
"turn the WORKLOG backlog into tickets"; the worker reads the source, then
calls `agentctl file-issue --title … --description-file …` once per ticket.
The orchestrator files each on Linear — in the delegated issue's team and
project by default (`--team` / `--project` / `--no-project` to steer),
optionally with `--priority 0-4` and repeatable `--label` — and hands the new
key/url back to the worker so it can summarize what it filed. You then
review/assign the tickets. Creation is deduped by a marker embedded in the
new issue's description, so a crash mid-relay can't file a duplicate.

Relaying is at-least-once with explicit dedupe: every posted comment embeds
an HTML-comment marker (`<!-- issuefleet:msg:<id> -->`); before posting, the
relay checks for the marker, so a crash between "posted" and "acked" cannot
double-post. The same marker keeps the orchestrator from re-ingesting its
own comments — deliberately *not* an API-user identity check, because with a
personal (non-bot) key the operator *is* that user and an identity filter
would silently eat their replies to the agent.

## Setup

1. **Linear API key** — create a personal API key at
   <https://linear.app/settings/api> (consider a dedicated bot account so
   claims are attributable). Then either `export LINEAR_API_KEY=...` or:
   ```sh
   mkdir -p ~/.config/issuefleet
   printf '%s' 'lin_api_...' > ~/.config/issuefleet/linear.key
   chmod 600 ~/.config/issuefleet/linear.key
   ```
2. **GitHub credential** — preferably the GitHub App (see Bot identities);
   fallback: a fine-grained PAT with **Contents: RW** and **Pull requests:
   RW** in `~/.config/issuefleet/github.key` (chmod 600). Clones, pushes,
   and PRs all use this credential over HTTPS — deliberately never an SSH
   key, which would carry the operator's full push rights. With branch
   protection on the base ref, the bot is PR-only by construction (GitHub
   has no "non-default branches only" push permission; protection or a
   ruleset is the enforcement). Secrets never go in the config file — the
   parser rejects them.
3. **The claim label** — create a label (default suggestion: `agent`) in the
   Linear team, or pick another claim strategy (below).
4. **Config** — write `~/.config/issuefleet/config.toml` (schema below,
   worked example in `examples/fleet.toml`).
5. **Verify** — `bin/issuefleet doctor`. It is safe and side-effect-free,
   tells you exactly what is missing, and prints exactly which issues would
   be claimed. Run it until it is clean; then try `bin/issuefleet once
   --dry-run`, then `run`.

The CLI is stdlib-only Python 3.11+: run `bin/issuefleet` directly, or
hermetically via `bazel run //:issuefleet --`. A Nix devshell (`nix develop`)
provides bazelisk/python/tmux on hosts that want it.

## Bot identities (optional, recommended)

**GitHub App (preferred).** PRs open as `yourapp[bot]`, auth uses
short-lived installation tokens instead of a long-lived PAT, and the app's
own webhook covers every installed repo (no per-repo webhook setup).

One-command setup via GitHub's app-manifest flow (no token required):

```sh
bin/issuefleet github-app-setup --webhook-url https://<tunnel>/webhook/github
# add --org <org> to create it under an org instead of your user account
```

It serves a localhost page, you click **Create GitHub App** once, and the
manifest conversion hands back everything: the private key lands in
`github_app_key_file`, the webhook secret in `[webhooks]
github_secret_file`, and it prints the `github_app_id` line for your config
plus the **install** link (installing it on the target repos is the one
remaining click). Permissions and event subscriptions are baked into the
manifest (*Contents/Pull requests: RW*; issue-comment/PR/review events).

With `github_auth = "auto"` the daemon switches to app auth as soon as the
id + key exist; installations are discovered per repo owner (pin
`github_app_installation_id` to skip discovery). `doctor` shows the
resolved `slug[bot]` and installation list.

App JWTs are RS256-signed via the `openssl` CLI (stdlib Python can't sign
RSA; openssl ships with macOS/Linux, and doctor checks for it).

*Simpler fallback:* a machine-user account with a fine-grained PAT in
`github_token_file` — pure config, no app registration, but a long-lived
credential and per-repo webhooks.

**Linear agent (agents platform).** Instead of a personal API key, install
issuefleet as a first-class Linear agent: it appears as an app user
(consumes **no seat**), can be **@-mentioned and delegated issues** — both
of which claim the issue regardless of the poll-side claim rule — and its
status/question/PR updates render as native agent-session activities
(thought / elicitation / response) instead of comments. Setup:

1. Create an OAuth app at <https://linear.app/settings/api/applications/new>.
   Enable webhooks on it, tick **Agent session events** (plus Comments if
   you want comment-driven wake-ups), set the webhook URL to your tunnel
   (below), and note the client id/secret and the webhook signing secret.
2. Configure `[credentials] linear_oauth_client_id`, put the client secret
   in `linear_oauth_client_secret_file`, the signing secret in
   `[webhooks] linear_secret_file`, and set `[webhooks] enabled = true`.
3. Run `bin/issuefleet linear-oauth` **once** as a workspace admin to install
   the agent app into the workspace (the `actor=app` OAuth flow on localhost).
4. Set `[credentials] linear_auth = "client_credentials"`. The daemon then
   mints its own **app-actor token** via the client_credentials grant from the
   client id/secret — valid ~30 days, no browser, and automatically refetched
   when it nears expiry or a call returns 401 (Linear's app tokens carry no
   refresh token, so re-minting *is* the refresh). No `linear.key` to rotate.

   > The older path — `linear-oauth` writing a token to `linear_api_key_file`
   > and `linear_auth = "auto"` picking it up by its `lin_oauth_` prefix —
   > still works, but that authorization-code token expires in **~24 hours**
   > and issuefleet keeps no refresh token, so the daemon would go dark daily.
   > Use `client_credentials` for anything long-running.

Webhooks are **strongly recommended** for the agent platform: Linear wants
an activity within 10 seconds of a delegation, and issuefleet acks
immediately from the webhook thread, then the claim proceeds on the woken
tick. But webhooks stay an *accelerator*, not the source of truth — if the
tunnel is down, delegation is still poll-claimable (delegation is
assignment, which is pollable), and the worker recovers its agent session
by **polling** for it (`find_agent_session`) and emitting a catch-up
activity, so the session view goes live from the next tick instead of
hanging at "waiting… → agent didn't start." You lose the sub-10-second ack
and pay poll latency, nothing more. A session prompt ("reply to the agent")
is webhook-only (not pollable), routed straight into the worker's inbox.

## Webhooks (push instead of polling)

With `[webhooks] enabled = true`, the daemon runs a listener
(default `127.0.0.1:8787`) and any verified event **wakes the reconcile
loop immediately** instead of waiting out the poll interval. Webhooks are
an accelerator only — polling remains the source of truth, so lost or
replayed deliveries cost nothing.

- `POST /webhook/github` — with a GitHub App, configure the webhook once on
  the app (covers all installed repos); otherwise add a repo webhook
  (Settings → Webhooks) for *Issue comments, Pull request reviews, Pull
  request review comments, Pull requests*, content type JSON, with a
  secret. Verified via `X-Hub-Signature-256` (HMAC-SHA256).
- `POST /webhook/linear` — the OAuth app's webhook (or a workspace webhook);
  verified via `Linear-Signature` plus a 60-second timestamp replay guard.

**Expose it through a tunnel, never directly**: point a Cloudflare Tunnel /
Tailscale Funnel (or ngrok for experiments) at `localhost:8787` and give
that HTTPS URL to GitHub/Linear. The listener binds loopback by default and
answers GET with a health probe for tunnel checks.

## Introspection dashboard (web UI)

With `[dashboard] enabled = true` (the default), the running daemon also
serves a web UI at `http://127.0.0.1:8788/` — the same information as
`issuefleet status`, but browsable:

- **Fleet list** (`/`): every worker with its session liveness, agent phase,
  turn/auto-turn counters, PR link, last activity, and pending mailbox
  counts. Auto-refreshes every 5s.
- **Worker detail** (`/worker/<KEY>`): full status, the pending inbox/outbox,
  the list of turns, and a tail of the captured pane log.
- **Transcript** (`/worker/<KEY>/turn/<N>`): the turn's stream-json log
  rendered as assistant text, tool calls, tool results, and the cost/duration
  summary (raw JSONL one click away).
- **JSON API**: `/api/workers` for scripting.

**Stopping a worker** is the one mutating action. It is `POST`-only and
gated behind a browser confirmation dialog. Clicking Stop doesn't touch the
fleet from the web thread — it enqueues the wind-down for the reconcile loop
(the single writer), which stops the container, removes the worktree, and
keeps the branch + an archived transcript, exactly like `issuefleet stop`.
(For a poll-claimed issue whose claim rule still matches, the next tick will
re-claim it — remove the label / advance the state to make a stop stick, same
as the CLI.)

The dashboard is a **control plane**, so treat it like one: it binds loopback
by default; reach it remotely over the tailnet with `tailscale serve`, never
a public Funnel. Disable it entirely with `[dashboard] enabled = false`.

## Fleet manager (Signal chat) — optional

The **fleet manager** is a host-side singleton that gives you a chat interface
to the whole fleet over a Signal group, fronted by a
[sigbot](https://github.com/fughilli/sigbot) service. It runs alongside the
reconcile loop in the same `issuefleet run` daemon (nothing containerized — it
needs broad, credentialed, cross-project reach, exactly the powers the
architecture keeps *out* of workers), and does three things each tick:

- **Answers you.** With an Anthropic key it is itself an agent: your message goes
  to a tool loop that can inspect the fleet (`list_workers`, `list_open_issues`,
  `get_issue`, `pending_escalations`) and act (`file_goal`, `create_issue`,
  `update_issue`, `reply_to_worker`), then replies in plain English. So "what's
  going on in Splanc?" gets *answered*; "make the HITL tests faster" gets filed as
  a goal; an answer to a blocked worker gets delivered. It works out which from
  the message, not from prefixes. Goals land on a dedicated top-level board
  (`board_project` / `board_team`); with `assign_goals = true` they're assigned to
  the fleet's own identity, so under the `agent` claim strategy a worker picks
  them up automatically. Beyond the goals board it manages **every** board it
  knows: `create_issue` files a ticket directly onto a named project (Splanc,
  IssueFleet, ...) — optionally assigning it to the fleet to be worked — and
  `update_issue` edits any issue's title, description, priority, or workflow state
  ("move FUG-30 to Done", "bump the Splanc caching ticket to urgent").

  **Without a key it degrades to a dispatch table** — `goal:`-prefixed and bare
  messages are filed as issues, `FUG-12:`-prefixed ones are relayed to that
  worker, and there is no way to *ask* it anything (a question becomes a ticket).
  That fallback is also what runs if the API call fails, so a flaky key never
  drops a message.
- **Unblocks or escalates workers.** When a worker calls `agentctl ask`, the
  manager triages the question against the worker's ticket and the top-level
  board. The **advisor** decides: `conservative` (default) always escalates;
  `claude` asks the model whether the context clearly answers it. An answerable
  question is delivered straight into the worker's mailbox (waking it with no
  human in the loop); anything else is forwarded to the group and tracked as
  pending. Your reply — either plain (routed to the oldest pending question) or
  prefixed `FUG-12:` (routed to that issue) — is delivered to the worker the
  same way.
- **Reports progress.** Every `report_interval_s` it posts a fleet summary
  (active workers, PRs, what's awaiting you).

Setup:

1. Stand up a sigbot service for one Signal group and mint an API key; write it
   to `~/.config/issuefleet/sigbot.key` (chmod 600) or set
   `$ISSUEFLEET_SIGBOT_API_KEY`. Set `base_url` to the service's URL — nothing
   validates it offline (the sigbot calls are live-only, so `doctor` can't
   catch a placeholder), and under the compose stack the daemon shares the
   tailscale sidecar's network namespace, so `127.0.0.1` there is *not* your
   host. The client comes from the build: `bazel run //:issuefleet` gets
   `sigbot-client` from the pinned requirements lock, and the deploy image
   `pip install`s it. Only the bare `bin/issuefleet` wrapper — deliberately
   stdlib-only — needs it installed by hand.
2. Create the top-level board as a Linear project and set `board_project` /
   `board_team`. To have recorded goals *worked* by the fleet, also list that
   project under `[[projects]]` with `claim.strategy = "agent"`.
3. Set `[fleet_manager] enabled = true`. Provide an `ANTHROPIC_API_KEY` (or
   `~/.config/issuefleet/anthropic.key`) to get the agentic manager above — the
   same key also enables `advisor = "claude"` for worker-question triage. Both
   fall back to their conservative, non-LLM behaviour without it.
4. `issuefleet doctor` verifies the key, the client, and the advisor; once the
   daemon is up, `issuefleet fleet` shows the Signal cursor and any pending
   escalations.

Escalations and answers travel over the worker mailbox, not Linear comments, so
they're immune to the app-identity comment filter and need no Linear round-trip.
State (Signal cursor, seen questions, pending escalations) persists in
`fleet_manager.json`. The first run baselines **both** sides of that history so
it isn't replayed as live work: the Signal cursor jumps to the newest message,
so the group's backlog doesn't become goals, and every already-*archived* worker
question is marked seen, so a resolved `agentctl ask` from days ago doesn't
resurface as a fresh escalation. Questions still sitting in a worker's
`pending_outbox` are untouched by the baseline — nobody has drained those, so
they're genuinely unanswered and escalate on that first tick.

## Security gate (credential scanning on `ready`)

The [load-bearing idea](#the-load-bearing-idea-credentials-never-enter-the-agents)
keeps *the operator's* credentials out of the containers — but nothing stops an
agent from committing a secret it generated, pasted, or scraped from its own
environment into its branch. The one outbox action that carries such content
*into the repository* is `ready` (it force-pushes the branch and opens the PR),
so the orchestrator scans the diff a `ready` would push **before** the push,
host-side, and can reject it.

The scan is deterministic and stdlib-only (`security.py`): it inspects only the
*added* lines of the branch's diff (`base...HEAD`) for known credential shapes —
private-key blocks, AWS/GitHub/Anthropic/OpenAI/Linear/Slack/Google/Stripe/
Tailscale tokens, JWTs, and secret-shaped assignments — plus newly-added
sensitive files (`.env`, `id_rsa`/`id_ed25519`, `*.pem`, `credentials.json`, …).
Patterns are narrow (a match is almost certainly a real secret) so a
false-positive block is rare; when one happens it's recoverable.

On a hit in the default `block` mode, nothing is pushed: the worker is woken via
its mailbox with a **redacted** rationale (the secret is never echoed into the
note, the logs, or the archived receipt — that would just relocate the leak),
naming the file and rule and telling it to scrub the secret from the branch
history and resubmit, or to justify a false positive via `agentctl ask`. `warn`
logs the finding and delivers the note but still pushes; `off` disables scanning.

`deep_scan = "claude"` layers an optional LLM pass on top (reusing the fleet
manager's Anthropic key) that can catch secrets the regexes miss. It is additive
only — every deterministic finding stands regardless — and an API failure
degrades to the deterministic result rather than clearing a hit or wedging a
submission.

## Configuration

```toml
[daemon]
poll_interval_s = 60                       # reconcile tick interval
max_workers = 4                            # global concurrency cap
state_dir = "~/.local/state/issuefleet"    # registry, worker archives, logs
worktree_root = "~/worktrees"              # worktrees live OUTSIDE the repos

[credentials]                              # lookup locations, never secrets
linear_api_key_env = "LINEAR_API_KEY"
linear_api_key_file = "~/.config/issuefleet/linear.key"
github_token_env = ["GITHUB_TOKEN", "GH_TOKEN"]   # checked in order
github_token_file = "~/.config/issuefleet/github.key"
github_auth = "auto"                       # auto | token (PAT) | app (GitHub App)
github_app_id = ""                         # App ID; with the key file, auto=app
github_app_key_file = "~/.config/issuefleet/github_app.pem"
# github_app_installation_id = 12345678    # optional; default: discover per owner
linear_auth = "auto"                       # auto | api_key (raw) | oauth (Bearer)
linear_oauth_client_id = ""                # Linear agent install (see Bot identities)
linear_oauth_client_secret_file = "~/.config/issuefleet/linear_oauth_client.secret"
linear_oauth_redirect_port = 9779

[webhooks]
enabled = false                            # true = push wake-ups + agent sessions
bind = "127.0.0.1"                         # keep loopback; tunnel in front
port = 8787
github_secret_file = "~/.config/issuefleet/github_webhook.secret"
linear_secret_file = "~/.config/issuefleet/linear_webhook.secret"

[dashboard]
enabled = true                             # introspection web UI (below)
bind = "127.0.0.1"                         # keep loopback; private tunnel in front
port = 8788

[fleet_manager]                            # Signal <-> fleet bridge (below); off by default
enabled = false
base_url = "http://sigbot-host:8100"       # the sigbot service (one Signal group)
api_key_file = "~/.config/issuefleet/sigbot.key"   # minted in the sigbot dashboard
board_project = "Fleet"                    # top-level board where goals are recorded
board_team = "FUG"                         # the board's Linear team (name, key, or UUID)
poll_interval_s = 60                       # Signal poll cadence
report_interval_s = 3600                   # progress reports to the group; 0 = off
assign_goals = true                        # assign filed goals to the fleet so they auto-claim
advisor = "conservative"                   # conservative (always escalate) | claude (LLM triage)

[security]                                 # scan each `ready` diff for leaked credentials; on by default
mode = "block"                             # block (reject the ready) | warn (log + notify, still push) | off
deep_scan = "off"                          # off | claude (extra LLM pass; reuses the Anthropic key)

[agent]
max_auto_turns = 50          # self-driven turns without human contact (the runaway brake)
max_restarts = 3             # crash restarts before giving up
claude_args = []             # extra flags for every `claude -p` turn
claude_container = "claude-container"      # launcher binary
# Untracked workspace-local state copied (copy-if-missing) from the parent
# checkout into each fresh worktree — e.g. .claude/settings.local.json,
# which a fresh worktree otherwise lacks. Git-excluded in the worktree.
copy_from_repo = [".claude", ".claude-container-overlay"]
# Host-side flags passed to claude-container before the in-container
# command. --skills-ignore-new (launcher > 1.6.12) launches with only
# already-accepted skills instead of prompting for undecided ones; set to
# [] for older launchers (doctor verifies the launcher knows each flag).
launcher_args = ["--skills-ignore-new"]
# container_config_dir = "~/.config/claude-container/config"  # default: launcher's shared dir

[[projects]]                 # one block per (Linear project -> GitHub repo) pair
name = "splanc"              # short handle used in paths, sessions, logs
linear_project = "Splanc"    # Linear project name (or UUID if names collide)
repo = "~/Projects/splanc"   # local main checkout; `origin` must point at GitHub
git_url = "git@github.com:you/splanc.git"  # daemon clones `repo` from here if missing
base_ref = "main"            # branch agents fork from and PRs target
claim = { strategy = "label", value = "agent" }
branch_template = "agent/{key}-{slug}"     # {key}=fug-12, {slug} from the title
state_in_progress = "In Progress"          # workflow state set on claim
state_done = "Done"                        # set when the PR merges
delete_remote_branch = true                # after merge
# max_workers = 2            # optional per-project cap within the global cap
```

Claim strategies (`claim.strategy` / `claim.value`):

| strategy | claims when | un-claims when |
| --- | --- | --- |
| `label` | issue carries the label `value` | label removed, or issue closed |
| `assignee` | issue assigned to Linear user id `value` | assignee changed, or issue closed |
| `state` | issue is in workflow state `value` | issue closed (claiming itself moves the state, so state changes can't un-claim) |
| `agent` | issue assigned (delegated) to the agent app user — polled, so it works even with webhooks down; @-mentions claim via webhook | un-assigned, or issue closed |
| *(agent session)* | issue delegated to / @-mentions the Linear agent (works under any strategy) | issue closed |

With the Linear agent installed, `strategy = "agent"` is the recommended
setup: assigning the bot *is* the claim gesture, and no label can
accidentally trigger a worker.

Queue order is Linear priority (Urgent → Low, "no priority" last), then age.
When the fleet is full, eligible issues wait; `doctor` shows the order.

## Worker lifecycle

| phase (agent-side) | meaning | leaves when |
| --- | --- | --- |
| fresh | claimed; worktree + `.agent/` provisioned, no turn yet | first turn starts (full issue brief as prompt) |
| running | taking self-driven turns, committing, posting `status` | asks a question / declares `ready` / budget trips |
| waiting | asked a question via `agentctl ask`; session idles | a human replies on the Linear issue |
| ready | declared `ready`; orchestrator pushed branch, opened PR | PR feedback arrives (back to running) or PR merges |
| idle | declared done via `agentctl idle` (or parked by the loop after two no-progress turns) | any human reply or feedback |
| budget-idle | `max_auto_turns` without human contact; posted a status and idling | any human reply (resets the budget clock) |
| crashed (host-side) | session died `max_restarts`+1 times; reported on the issue | operator intervention (worktree kept for inspection) |

**Merge conflicts** are watched on the same PR poll. When a submitted PR
stops merging cleanly (GitHub reports `mergeable: false` because other work
landed on the base), the daemon does the one thing the credential-less worker
container cannot — a host-side `fetch` refreshing `origin/<base_ref>` in the
shared clone — then wakes the agent with a `merge_conflict` message telling it
to `git rebase origin/<base_ref>`, resolve, and re-run `agentctl ready`. The
worker rebases with no network of its own. The nudge fires once per conflict
episode (re-armed when the PR next reads mergeable), so a stuck-dirty PR isn't
nagged every tick, but a fresh conflict after a resolution is caught.

Teardown (merge, un-claim, or `stop`): signal the agent via a `shutdown`
mailbox message → archive the mailbox + turn transcripts to
`<state_dir>/archive/<project>-<KEY>-<timestamp>/` (the transcript outlives
the branch) → kill the tmux session (the `--rm` container exits with it) →
remove the worktree → on merge only: set the issue's done state and delete
the branch.

## Watching, steering, stopping

```sh
bin/issuefleet status            # fleet: phase, turns, PR, liveness, pending messages
bin/issuefleet attach FUG-12     # the worker's live tmux session (take over freely)
bin/issuefleet logs FUG-12 -f    # tail its captured output
bin/issuefleet stop FUG-12       # wind one worker down by hand
bin/issuefleet once              # single reconcile tick (cron-friendly)
bin/issuefleet once --dry-run    # print every action a tick would take; mutate nothing
```

Or point a browser at the daemon's **introspection dashboard**
(`http://127.0.0.1:8788/`) for the same view plus per-turn transcripts and a
confirm-gated Stop button — see [Introspection dashboard](#introspection-dashboard-web-ui).

Two log layers per worker:

- **Live activity** (`attach` / `logs -f`): the turn loop streams each turn
  as `claude -p --output-format stream-json` and prints one compact line per
  event to its pane — assistant text, `→ ToolName args`, `✓ turn complete
  42s $0.31`. This is the "is it stuck?" view; `status` also shows a
  last-activity age from the turn-log mtimes.
- **Raw transcripts**: the full stream-json of turn N is kept at
  `<worktree>/.agent/logs/turn-NNNN.jsonl` and archived host-side at
  teardown.

Steering happens in Linear: comment on a claimed issue — plain language, no
@-mention needed — and the comment is injected into the agent's next turn on
the next tick. A reply wakes an idle agent (waiting on a question, idling
after its PR, or budget-idled); comments accumulated between ticks are
batched into one turn. Remove the label (or close the issue)
to un-claim. Stopping the daemon never stops the agents — they live in
detached tmux sessions; a restarted daemon re-adopts the fleet from the
registry.

## Running persistently

**macOS (launchd)** — edit paths in `deploy/com.issuefleet.daemon.plist`, then:

```sh
cp deploy/com.issuefleet.daemon.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.issuefleet.daemon.plist
```

**Linux (systemd user unit)** — edit paths in `deploy/issuefleet.service`, then:

```sh
cp deploy/issuefleet.service ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now issuefleet
```

Both ship with the daemon's stdout going to `<state_dir>/daemon.log`.
Alternatively, run `issuefleet once` from cron — every command is
restart-safe and idempotent.

**Homelab (containerized, autostarting):** `deploy/docker/` holds a compose
stack — the daemon plus a Tailscale sidecar that Funnels
`https://issuefleet.<tailnet>.ts.net/webhook/*` to the listener, with
`restart: unless-stopped`. Workers are launched as *sibling* containers via
the mounted docker socket, which imposes a same-path mount invariant —
read `deploy/docker/README.md` before using it.

## Known edges (honestly)

- **Merge detection lags** by up to `poll_interval_s`, as does everything
  else the loop observes.
- **Cost.** N agents running continuously is real money; `max_auto_turns`
  is the only brake. There is no wall-clock or token budget yet.
- **Mid-turn stop.** Teardown signals the agent, but a worker cut off
  mid-turn loses that turn's uncommitted work (its commits and mailbox are
  archived first).
- **`.agent/` exclusion is per-repo, not per-worktree.** The brief suggested
  `.git/worktrees/<name>/info/exclude`, but git (verified on 2.43) never
  reads that file; only `$GIT_COMMON_DIR/info/exclude` works. issuefleet
  appends `.agent/` there — uncommitted local state, invisible to
  collaborators, but shared by all worktrees of that repo.
- **Crashed workers hold their claim** (deliberately, so the issue isn't
  re-claimed into the same failure). Free the issue by removing/re-adding
  the label after inspecting the kept worktree.
- **Agent branches are force-pushed, so a diverged branch loses a side.**
  The daemon pushes `agent/*` with a plain `--force` (a lease needs a
  remote-tracking ref, which pushing to an explicit token URL doesn't
  create). Startup now fetches and *fast-forwards* a worker's branch onto
  its remote tip, so pushing to an agent's branch while it's stopped is
  safe. But if BOTH sides advanced, the branch is left alone and the agent
  is only *told* — nothing stops it committing on and force-pushing its own
  side. Reconcile before letting it resume, or expect to lose yours.
- **One Linear workspace per config.** All projects share the one API key.
- **Launcher prompts.** claude-container's interactive confirmations block a
  headless worker. Skill approval needs launcher > 1.6.12, where worktrees
  share the parent repo's skill choices (keyed on the resolved main working
  tree) and `--skills-ignore-new` (passed by default via `launcher_args`)
  skips prompting for skills added after approval — accept those once from
  the parent checkout. A worker stuck at any prompt is visible (and
  answerable) via `issuefleet attach <KEY>`.
- **`state` claim strategy can't detect "operator changed their mind"** —
  only closure un-claims (see the strategy table); same for session claims.
- **Agent-session relays have no dedupe probe** (activities can't be
  searched like comments): a crash between emit and ack can duplicate a
  thought/elicitation. Cosmetic, unlike a duplicated comment.
- **Session prompts before a worker exists are dropped** (logged): if you
  reply to the agent in the seconds between delegation and the claim
  completing, re-send after the worker's first activity appears. The
  initial delegation itself is never lost — it stays queued until claimed.
- **Contents:RW can push any *unprotected* branch.** The scoped token
  keeps the bot off other repos and expires hourly, but "PR-only" on the
  target repo comes from branch protection/rulesets on the base ref — set
  that up; the app has no bypass.
- **The Linear agents API surface is new** and was implemented from
  Linear's docs without a live workspace to test against; the OAuth flow,
  activity mutations, and webhook payload parsing are unit-tested but
  unproven live (see WORKLOG).

## Development

```sh
bazelisk test //tests:all     # hermetic Python 3.11 toolchain, no system deps
bazelisk run //:issuefleet -- doctor
bazelisk run //:requirements  # regenerate requirements_lock.txt from requirements.in
nix develop                   # optional devshell: bazelisk, python, tmux
```

`bazelisk run //:issuefleet` is the most self-sufficient way to run the daemon:
it brings its own Python 3.11 *and* the one non-stdlib runtime dependency
(`sigbot-client`, for the fleet manager), so it works on a host with neither.
The core stays stdlib-only — that dep hangs off `//src/issuefleet:cli` alone,
never the library, so `bin/issuefleet` and the test graph are untouched. Edit
`requirements.in` and re-run `//:requirements` to move the pin;
`//:requirements_test` fails if the lock drifts.

Layout: `src/issuefleet/` (core: mailbox, turns, reconcile, clients, ports),
`src/issuefleet/agent_runtime/` (the code staged into each worktree's
`.agent/bin/` — stdlib-only, version-matched to the orchestrator by
construction), `tests/` (everything runs offline; gitops/tmux tests use real
git and tmux with local fixtures). The manual end-to-end procedure is
`docs/SMOKE_TEST.md`. `WORKLOG.md` carries session-to-session state.

### Shared Bazel cache across worktrees

`.bazelrc` puts the disk and repository caches in per-tree directories
(`.bazel-disk-cache`, `.bazel-repo-cache`), so a fresh worktree — every
issuefleet worker gets one — would otherwise build from cold. `tools/bazel` (a
Bazelisk wrapper) fixes that: it points both caches at a single location shared
by all worktrees of the repo and injects it via a generated `--bazelrc`, which
overrides the per-tree defaults. It resolves the shared root as:

1. `$BAZEL_SHARED_CACHE_DIR` if set — the hook for a shared volume mounted into
   an isolated environment. A worker container's `.git` points at a host path
   it cannot see, so mount a per-project cache dir into each worker and export
   this variable (e.g. via `launcher_args`) to make the fleet share too.
2. otherwise `<git-common-dir>/bazel-cache` — every linked worktree shares one
   `.git`, so co-located worktrees (a developer's own `~/worktrees` checkouts)
   share automatically, with no setup and nothing committed.

If neither resolves (not a git checkout, or the common dir is unreachable) the
wrapper is a no-op and the per-tree caches from `.bazelrc` apply.
