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

> `/srv/issuefleet/worktrees`, `/srv/issuefleet/repos`, and
> `/srv/issuefleet/claude-config` are mounted at identical absolute paths
> on the host and in the daemon container, and the config.toml must use
> those paths.

Break the invariant and workers get empty or wrong bind mounts, with no
error at launch time.

## Host preparation

```sh
sudo mkdir -p /srv/issuefleet/{worktrees,repos,claude-config,state,config,ssh,bin}

# 1. Target repos: clone (SSH remote) under /srv/issuefleet/repos/<name>
git clone git@github.com:you/yourrepo /srv/issuefleet/repos/yourrepo

# 2. The launcher: copy the claude-container script (it is bind-mounted,
#    not baked in, so launcher upgrades don't need an image rebuild)
cp ~/Projects/claude-container/bin/claude-container /srv/issuefleet/bin/

# 3. Claude credentials for workers: seed the shared config dir (same
#    content as ~/.config/claude-container/config on your Mac — the OAuth
#    credentials + settings.json with bypassPermissions)
cp -r ~/.config/claude-container/config/* /srv/issuefleet/claude-config/

# 4. Push key: a deploy key or user key authorized for the target repos
cp <your-key> /srv/issuefleet/ssh/id_ed25519
ssh-keyscan github.com > /srv/issuefleet/ssh/known_hosts
chmod 700 /srv/issuefleet/ssh && chmod 600 /srv/issuefleet/ssh/id_ed25519

# 5. Config + secrets under /srv/issuefleet/config (mounted at /etc/issuefleet):
#    config.toml, linear.key, github_app.pem, *_webhook.secret — chmod 600.
```

`config.toml` differences from a laptop setup:

```toml
[daemon]
state_dir = "/srv/issuefleet/state"
worktree_root = "/srv/issuefleet/worktrees"      # same-path invariant
[credentials]
linear_api_key_file = "/etc/issuefleet/linear.key"
github_app_key_file = "/etc/issuefleet/github_app.pem"
[agent]
container_config_dir = "/srv/issuefleet/claude-config"   # same-path invariant
[webhooks]
enabled = true                                    # bind stays 127.0.0.1
github_secret_file = "/etc/issuefleet/github_webhook.secret"
linear_secret_file = "/etc/issuefleet/linear_webhook.secret"
[[projects]]
repo = "/srv/issuefleet/repos/yourrepo"
# ...
```

## Running it (bazel targets)

```sh
bazel run //deploy/docker:image     # docker build -> ghcr.io/fughilli/issuefleet:dev
bazel run //deploy/docker:up       # compose up -d --build (daemon + funnel)
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
docker compose exec issuefleet bin/issuefleet --config /etc/issuefleet/config.toml doctor
docker compose exec issuefleet bin/issuefleet --config /etc/issuefleet/config.toml status
docker compose exec -it issuefleet tmux attach -t issuefleet-<proj>-<KEY>   # watch a worker
docker compose logs -f issuefleet                                           # daemon log
```

Autostart is `restart: unless-stopped` + Docker's own boot enablement
(`systemctl enable docker`). Restarting the daemon container never kills
workers' *containers* — but note the tmux caveat below.

## Known edges (read before relying on it)

- **UNPROVEN as a whole**: this compose stack was authored, not yet run.
  The individually risky seams: claude-container running *inside* a
  container (it needs bash + docker CLI, both provided; it computes
  USER_UID from `id -u` — root in this image, so workers run as root and
  write root-owned files into the worktrees), and Funnel serve-config
  templating. Run `doctor` first; it validates most of the chain.
- **tmux lives inside the daemon container**, so unlike the laptop setup,
  restarting the *daemon container* kills worker sessions (their docker
  containers die with the pty). The crash-restart path re-adopts and
  restarts them (bounded by max_restarts), so this is degraded, not
  broken — but prefer `docker compose restart tailscale` / config reloads
  over restarting the issuefleet service while workers are mid-task.
- The launcher's linked-worktree `.git` mount only works because repos and
  worktrees share the `/srv/issuefleet` prefix visible to the host daemon.
  Don't relocate one without the other.
