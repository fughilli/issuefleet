# Homelab deployment: issuefleet + Tailscale Funnel, autostarting

`docker compose up -d` runs two services that restart on boot (with Docker's
daemon enabled at boot): the **tailscale** sidecar joins your tailnet as
machine `issuefleet` and Funnels `https://issuefleet.<tailnet>.ts.net/webhook/*`
to the daemon's webhook listener; the **issuefleet** daemon runs the
reconcile loop and launches agent containers. They share one network
namespace, so from outside they behave as a single unit.

## The load-bearing constraint: sibling containers, same paths

The daemon starts workers by invoking `claude-container`, which calls
`docker run` — against the **host's** docker daemon via the mounted socket.
Workers are therefore *siblings* of the daemon container, and every path
the launcher bind-mounts into them (worktree, the repo's `.git`, the claude
config dir) is resolved **by the host**. Hence the invariant:

> `<ROOT>/{worktrees,repos,claude-config,state}` are mounted at identical
> absolute paths on the host and in the daemon container, and the
> config.toml must use those paths.

The same applies to any checkout the daemon does *not* own — a `repo` that
points at your own working tree rather than `<ROOT>/repos/...`. Those live
under `$ISSUEFLEET_PROJECTS` (`~/Projects` by default), which is same-path
mounted and passed into the daemon's environment for the same reason. Write
it as `${ISSUEFLEET_PROJECTS}/yourrepo` in config.toml, never `~/yourrepo`:
`~` is `/root` inside the container and would miss the mount entirely.

The root is declared ONCE, in `deploy/docker/env.sh`, as
**`$HOME/.issuefleet` on every platform** (inside Docker Desktop's shared
`/Users` on macOS; no sudo anywhere). Every bazel wrapper exports it,
compose mounts `$ROOT:$ROOT` and passes `ISSUEFLEET_ROOT` into the
daemon's environment, and config paths expand env vars — so the same
`config.toml` runs on a laptop and in the container unchanged. Override
by exporting `ISSUEFLEET_ROOT` before `bazel run`. Break the invariant and
workers get empty or wrong bind mounts, with no error at launch time.

## Host preparation

**`~/.issuefleet` is data only** (repos, worktrees, state/archives, worker
claude credentials) — created automatically by any bazel target, with the
launcher seeded from PATH. **Config + secrets stay in `~/.config/issuefleet`**,
the exact same files a laptop setup uses (mounted at the container's home
path, so `~/...` credential paths resolve identically in both worlds).
What you must provide by hand:

```sh
# 1. Config + secrets: ~/.config/issuefleet/{config.toml,linear.key,
#    github_app.pem,*_webhook.secret} — if you ran the laptop setup, you
#    already have all of these.

# 2. Claude credentials for workers: seed the shared config dir (same
#    content as ~/.config/claude-container/config — OAuth credentials +
#    settings.json with bypassPermissions). Never copied automatically.
cp -r ~/.config/claude-container/config/* ~/.issuefleet/claude-config/

```

No SSH key: clones, pushes, and branch deletion all use the GitHub App's
scoped installation token over HTTPS — the operator's keys never enter the
container, and with branch protection on the base ref the bot is PR-only.

One `config.toml` serves laptop and container. Data paths use
`${ISSUEFLEET_ROOT}` (the daemon defaults it to `~/.issuefleet` when the
variable is unset, so this works on a laptop too); credential paths are
plain `~/...` defaults — no changes needed:

```toml
[daemon]
state_dir = "${ISSUEFLEET_ROOT}/state"
worktree_root = "${ISSUEFLEET_ROOT}/worktrees"   # same-path invariant
[agent]
container_config_dir = "${ISSUEFLEET_ROOT}/claude-config"   # same-path invariant
[webhooks]
enabled = true                                    # bind stays 127.0.0.1
[[projects]]
repo = "${ISSUEFLEET_ROOT}/repos/yourrepo"
git_url = "git@github.com:you/yourrepo.git"   # daemon clones it on first run
# ...
```

## Running it (bazel targets)

```sh
bazel run //deploy/docker:image     # docker build -> ghcr.io/fughilli/issuefleet:dev
bazel run //deploy/docker:up       # compose up -d --build (daemon + funnel)
bazel run //deploy/docker:doctor   # doctor inside the stack (exec, or one-shot if daemon is down)
bazel run //deploy/docker:down     # compose down (workers survive: they're siblings)
```

CI (`.github/workflows/ci.yml`) tests every push/PR and publishes
multi-arch (amd64+arm64) images to **ghcr.io/fughilli/issuefleet**
(`:latest` + `:<sha>`) on pushes to main — so the homelab can skip local
builds entirely: `docker compose pull issuefleet && docker compose up -d`.
The GHCR package may need to be made public once (repo → Packages →
settings) for an anonymous homelab pull, or `docker login ghcr.io` there.

## Tailscale / Funnel

```sh
cd deploy/docker
echo 'TS_AUTHKEY=tskey-auth-…' > .env     # create at login.tailscale.com/admin/settings/keys
docker compose up -d
docker compose exec tailscale tailscale funnel status
```

Funnel must be allowed for your tailnet (the admin console prompts once).
Your public webhook base is `https://issuefleet.<tailnet>.ts.net` — put
`…/webhook/github` in the GitHub App's webhook settings and
`…/webhook/linear` in the Linear OAuth app's webhook settings. Only the two
`/webhook/*` paths are exposed; both are HMAC-verified on top.

## Operating it

```sh
bazel run //deploy/docker:status    # fleet: phase, turns, PR, liveness, last activity
bazel run //deploy/docker:logs      # follow the reconcile-loop log (ticks/claims/relays)
bazel run //deploy/docker:doctor    # health checks
bazel run //deploy/docker:attach -- FUG-14   # a worker's live tmux (detach: Ctrl-b d)
docker compose logs -f issuefleet                                           # daemon log
```

Autostart is `restart: unless-stopped` + Docker's own boot enablement
(`systemctl enable docker`). Restarting the daemon container never kills
workers' *containers* — but note the tmux caveat below.

## Known edges (read before relying on it)

- **The daemon must not run as root** (found live: the launcher
  propagates the daemon's uid to workers, and claude refuses
  bypassPermissions as root — every turn fails instantly). The compose
  file therefore runs the service as your uid (`user:` fed by env.sh,
  which also grants the docker socket's group). `doctor` fails loudly on
  both conditions; always run it after changing the stack.
- **tmux lives inside the daemon container**, so unlike the laptop setup,
  restarting the *daemon container* kills worker sessions (their docker
  containers die with the pty). The crash-restart path re-adopts and
  restarts them (bounded by max_restarts), so this is degraded, not
  broken — but prefer `docker compose restart tailscale` / config reloads
  over restarting the issuefleet service while workers are mid-task.
- The launcher's linked-worktree `.git` mount only works because repos and
  worktrees are visible at the same path to both the daemon container and
  the host daemon — under ROOT, or under `$ISSUEFLEET_PROJECTS`. Don't
  relocate one without the other. The failure is quiet: the launcher
  resolves the worktree's `.git` pointer in the daemon container and, if it
  doesn't resolve, prints "git won't work in the container" and starts the
  worker anyway, without the mount.
- **Git ownership: the mount SHAPE matters, not just the path.** A checkout
  under `$ISSUEFLEET_PROJECTS` belongs to the host user, while both this
  container and the workers it launches run as root — normally git's
  "dubious ownership" refusal. What saves the workers is that Docker
  Desktop presents a bind-mount *root* as root-owned, and the launcher
  mounts the worktree as its own mount root (`-v $WORKSPACE_DIR:/workspace`):
  git checks the directory it starts from, passes, then follows the gitdir
  pointer into the host-owned `.git` without re-checking. The daemon has no
  such luck — it reaches the checkout through the *parent* tree mount, where
  the repo dir keeps its real uid — hence the `safe.directory` entry in
  `GIT_CONFIG_*` in docker-compose.yml. Measured, not assumed: same repo,
  parent-tree mount `rc=128`, launcher-shaped mount `rc=0`.
  Two corollaries. Running this container as the host uid (to "fix" the
  mismatch) makes it *worse*: uid 501 can't connect to the root-owned
  docker socket, and it turns the root-owned `/workspace` into the dubious
  one. And this rests on Docker Desktop's ownership synthesis — on a Linux
  host the uids are real, so re-test rather than assume it carries over.
- **Don't run the laptop-mode daemon and this stack against the same
  Linear projects simultaneously** — they have separate registries and
  will double-claim issues. Stop `bin/issuefleet run` before `:up`.
