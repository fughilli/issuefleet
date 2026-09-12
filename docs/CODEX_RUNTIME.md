# Codex workers

The fleet manager's API provider and each project's coding runtime are independent.
Codex workers use `codex exec --json` and resume the exact thread ID emitted by
Codex. Existing workers keep their persisted runtime and model when configuration
changes. Defaults and project overrides apply to newly provisioned workers.
The Codex home is also pinned for a conversation, so changing `codex_home` does
not strand existing workers in an empty session store.

## Install both runtimes in the worker image

The existing `claude-container` launcher still provides Docker isolation, mapped
user IDs, project overlays and linked-worktree mounts. IssueFleet builds the
matching base from pinned source, then adds Codex to provide both CLIs:

```sh
docker buildx bake --file deploy/worker/docker-bake.hcl --load worker
```

The Bake file pins `fughilli/claude-container` at commit
`4cf93d4a062563470f332902785c3b603d9b2c29`, whose launcher matches the verified
1.7.0 installation. The Dockerfile pins `@openai/codex@0.154.0`.
Use `--set worker.args.CODEX_VERSION=...` when deliberately upgrading the CLI,
and retest first-turn execution, resume and takeover. The launcher must support
`--mount` and `--skills-ignore-new` (verified with launcher 1.7.0).

```toml
[agent]
runtime = "codex"
model = "gpt-6-astra"
reasoning_effort = "high"
container_image = "issuefleet-worker:codex"
# Optional: override the dedicated home with an absolute path.
# codex_home = "/absolute/path/to/issuefleet/codex"
```

`container_image` sets the launcher's `CLAUDE_IMAGE` for headless workers and
interactive takeovers. Existing project overlays layer on top of this base.
Both CLIs run inside the container with headless permissions; the container is
the isolation boundary. Do not invoke the staged turnloop directly on a host
where it would expose unrelated files to a worker.

## Authenticate a dedicated Codex home

IssueFleet mounts only the configured Codex home, not your personal `~/.codex`.
This home stores worker credentials and conversations and must remain writable
across container restarts, release/adopt and takeover. It is shared by Codex
workers in the fleet, so use it only for this fleet's account and configuration.

Create it and sign in using a file-backed credential store. The container cannot
access your host's macOS Keychain:

```sh
install -d -m 700 "$HOME/.config/issuefleet/codex"
CODEX_HOME="$HOME/.config/issuefleet/codex" codex -c 'cli_auth_credentials_store="file"' login
chmod 600 "$HOME/.config/issuefleet/codex/auth.json"
CODEX_HOME="$HOME/.config/issuefleet/codex" codex login status
```

For API-key worker authentication, use the same dedicated home and pass the key
through stdin:

```sh
printenv OPENAI_API_KEY | CODEX_HOME="$HOME/.config/issuefleet/codex" codex -c 'cli_auth_credentials_store="file"' login --with-api-key
```

Neither command writes the key into IssueFleet's TOML or process arguments.
The fleet manager uses its separately resolved OpenAI API key; a Codex ChatGPT
login does not authenticate the manager's Responses API client. Tracker, forge
and Signal credentials remain on the orchestrator side of the mailbox.

The bundled Compose deployment mounts `ISSUEFLEET_CODEX_HOME` at the same
absolute path inside the daemon and on the host. `deploy/docker/env.sh` creates
the directory and defaults it to `~/.config/issuefleet/codex`; the daemon uses
that environment variable as its default Codex home. If you set `codex_home`
explicitly in TOML, set `ISSUEFLEET_CODEX_HOME` to the same absolute path before
starting Compose. Custom deployments must preserve this same-path mount because
the launcher issues bind mounts through the host's Docker daemon.

## Mix worker runtimes by project

Keep a global default and override it for individual projects:

```toml
[agent]
runtime = "claude"
container_image = "issuefleet-worker:codex"

[[projects]]
name = "backend"
linear_project = "Backend"
repo = "~/repos/backend"
claim = { strategy = "label", value = "agent" }

[projects.agent]
runtime = "codex"
model = "gpt-6-astra"
reasoning_effort = "high"
```

A runtime switch starts with that runtime's own defaults; a project does not
accidentally inherit the other runtime's model or arguments. Model, reasoning
effort and arguments for an existing conversation survive release/adopt.
Runtime arguments also apply to interactive takeover, so use options supported
by both interfaces. IssueFleet owns session selection and machine-output flags.

## Select a worker in a Linear description

Put the selection on the first nonblank line, before the task text:

```text
IssueFleet: worker=astra

Implement the requested change.
```

You can use a shorter case-insensitive first line instead:

```text
worker opus 5
```

Common forms such as `worker astra`, `Worker: Codex Astra`, `use worker opus
5`, and `fleet opus 5` are equivalent.

Built-in choices require no Linear label or profile configuration:

- `opus-5`: Claude Code with `claude-opus-5`
- `astra`: Codex with `gpt-6-astra` and high reasoning effort
- `claude`: Claude Code with its runtime-default model
- `codex`: Codex with its runtime-default model

`fleet=` is an alias for `worker=`. Use an explicit tuple for another model:

```text
IssueFleet: runtime=codex model=<model-id> effort=xhigh
```

Only an exact command occupying the first nonblank line is parsed. Ordinary
task prose and comments never change execution. An invalid directive fails
before IssueFleet creates a worktree or container. The selected tuple is
snapshotted for the worker, so an edit affects only a later claim.

## Optional worker-profile labels

Use a Linear label group when a visible, filterable picker is more useful than
the description directive. Linear permits one label from a group on an issue,
and IssueFleet also rejects multiple group members before claiming. Configure
by immutable IDs so renaming a label does not change what it runs:

```sh
issuefleet linear-labels
```

```toml
[agent]
runtime = "claude"
profile_label_group_id = "<Worker profile group UUID>"
container_image = "issuefleet-worker:codex"

[[agent.profiles]]
name = "codex-astra"
label_id = "<Codex Astra label UUID>"
runtime = "codex"
model = "gpt-6-astra"
reasoning_effort = "high"

[[agent.profiles]]
name = "codex-fast"
label_id = "<Codex Fast label UUID>"
runtime = "codex"
model = "<another supported Codex model>"
reasoning_effort = "medium"
```

Add **Codex Astra** or **Codex Fast** to a Linear issue before applying the
claim label, assignment, or delegation that starts it. With no description
directive or worker-profile label, the project/global default applies. When
both selectors are present they must resolve to the same settings. Profile
mappings require an explicit runtime and model. An unmapped label in the
configured group is treated as a configuration error instead of silently
running the fallback.

The selected tuple and its source are persisted in both the worker state and
registry. Description, label, and config edits affect only future workers.
`issuefleet status`, the dashboard, the Linear claim message, and the fleet
manager expose the exact selection. The manager provider/model stays under
`[fleet_manager]`; it is one fleet-wide orchestration loop and is not selected
per issue.

`issuefleet takeover KEY` runs `codex resume <recorded-thread-id>`. If a Codex
worker never recorded a thread ID, takeover fails clearly instead of selecting
another conversation. After an interrupted interactive session, IssueFleet
confirms the exact worker container has stopped before adopting the branch back.
If Docker cannot confirm termination, the released worktree stays intact for
recovery. Commit changes before exiting, as with Claude.

## Verify the setup

Run `issuefleet doctor`. It checks both runtime selections in a mixed fleet,
the dedicated Codex home and its `auth.json`, and every explicitly configured
base-image runtime using disposable containers without network or host mounts.
An unspecified image is reported as unverified. Base-image probes do not execute
project overlays, startup hooks, credentials or a billable model turn; a live
scratch-worker test is needed to verify account access and the complete launch.

Official references: [Codex authentication](https://learn.chatgpt.com/docs/auth)
and [non-interactive execution](https://learn.chatgpt.com/docs/non-interactive-mode).
