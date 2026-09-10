"""Tracker backend factory.

One place that turns ``cfg.tracker`` into a concrete ``Tracker`` (ports.py),
so the CLI and doctor build the same object. Backends are imported lazily so a
Linear fleet never imports the Jira module and vice versa.
"""

from __future__ import annotations

from issuefleet.httpx import urllib_transport


def build_tracker(cfg, transport=urllib_transport):
    if cfg.tracker == "jira":
        from issuefleet import jira

        return jira.JiraTracker(jira.client_from_config(cfg, transport))
    from issuefleet.linear import LinearTracker, client_from_config

    return LinearTracker(client_from_config(cfg, transport))
