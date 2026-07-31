# Task: migrate issuefleet to the unified config/data layout

You are an agent on the operator's Mac. Execute this migration end to end,
then report. Read the whole document before starting.

## Attribution

Any git commit you make during this task must be authored as the issuefleet
bot, not as yourself or the operator:

```sh
git -c user.name="issuefleet[bot]" \
    -c user.email="4440229+issuefleet[bot]@users.noreply.github.com" \
    commit ...
```

(That email maps to the GitHub App's bot identity, so the commit is listed
as the bot's work. This migration itself is mostly local-file surgery; the
rule applies to any repo change you end up committing.)

## Context

- Repo: `~/Projects/linear_dispatch` (github.com/fughilli/issuefleet).
  Pull latest main first; you need commit `358454f` or later.
- issuefleet is a daemon that runs coding-agent workers against Linear
  issues. Its layout just changed: **config + secrets live in
  `~/.config/issuefleet/`** (unchanged location), **all data moves under
  `~/.issuefleet/`** (worktrees, repo checkouts, state/registry/archives,
  worker claude credentials). Config data-paths use `${ISSUEFLEET_ROOT}`,
  which the daemon defaults to `~/.issuefleet`.
- The operator's shell is fish; run your commands via bash to be safe.
- Do NOT: delete any file under `~/.config/issuefleet` (only edit
  config.toml, after backing it up); touch anything in Linear or GitHub
  settings; leave two daemons running.

## Steps

1. **Quiesce.** `cd ~/Projects/linear_dispatch && git pull`.
   Check for a running daemon (`pgrep -fl "issuefleet.*run"`) and stop it
   (SIGTERM; it exits after its current tick). Then `bin/issuefleet status`:
   if any workers are listed, run `bin/issuefleet stop <KEY>` for each and
   confirm `status` shows an empty fleet. **Do not proceed with live
   workers** — moving the state dir would orphan the registry.

2. **Back up, then rewrite `~/.config/issuefleet/config.toml`.**
   `cp ~/.config/issuefleet/config.toml ~/.config/issuefleet/config.toml.bak`
   Rewrite it, preserving every key not mentioned here (webhook settings,
   claim strategies, launcher_args, github_app_id, all `*_file` credential
   paths):

   ```toml
   [daemon]
   state_dir = "${ISSUEFLEET_ROOT}/state"
   worktree_root = "${ISSUEFLEET_ROOT}/worktrees"

   [agent]
   container_config_dir = "${ISSUEFLEET_ROOT}/claude-config"
   # ...keep existing [agent] keys

   [[projects]]  # apply this pattern to EVERY project block:
   # repo    -> "${ISSUEFLEET_ROOT}/repos/<name>"
   # git_url -> the remote, e.g. "git@github.com:fughilli/issuefleet.git"
   #            (smoke: fughilli/issuefleet-smoke, splanc: fughilli/splanc).
   #            Only owner/name is parsed from it; actual clones/pushes go
   #            over HTTPS with the GitHub App's scoped token.
   ```

3. **Migrate state.** The old state dir is whatever `state_dir` was before
   (likely `~/tmp/issuefleet-smoke-state`). Copy its contents so worker
   archives survive:
   `mkdir -p ~/.issuefleet/state && cp -a <old_state_dir>/. ~/.issuefleet/state/`

4. **Seed the container-stack extras** (harmless for laptop mode too):
   - `mkdir -p ~/.issuefleet/claude-config`
   - `cp -a ~/.config/claude-container/config/. ~/.issuefleet/claude-config/`
   (No SSH key anywhere: pushes and clones use the GitHub App's scoped
   installation token over HTTPS.)

5. **Verify.** `bin/issuefleet doctor` must show 0 problems. Expect WARNs
   of the form "repo … missing — will be cloned from … on first run" (the
   daemon clones repos itself now; that's fine). One known pre-existing ✗
   may remain: the splanc GitHub API 404 (the GitHub App isn't installed
   on that repo) — report it, don't fix it.
   Then containerized: `bazel run //deploy/docker:doctor` — same
   expectations (a ⚠ about `claude` not on PATH in-container is normal).

6. **Clean up obsolete locations** — only after step 5 is green:
   `rm -rf ~/.issuefleet/config` (a dead location from an interim layout),
   and the old state/worktree dirs (e.g. `~/tmp/issuefleet-smoke-state`,
   `~/tmp/issuefleet-smoke-worktrees`).

7. **Do not restart the daemon** — leave that to the operator. Report:
   what you changed, doctor output from both runs, anything unexpected.
