"""Code that runs *inside* the worker container, staged into
``<worktree>/.agent/bin/`` at claim time (never committed to the target repo).

It is stdlib-only and self-contained: the whole ``issuefleet`` package is
staged alongside, so these modules use normal absolute imports. Nothing here
may ever talk to Linear or GitHub — the mailbox is the only channel out.
"""
