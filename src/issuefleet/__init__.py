"""issuefleet: drain a Linear work queue into GitHub PRs with agent workers."""

__version__ = "0.1.0"

# Marker prefix embedded (as an HTML comment) in every comment the
# orchestrator posts. Serves double duty: outbound relay dedupe (don't post a
# message whose marker already exists) and inbound filtering (never re-ingest
# our own posts, even if the identity check breaks because someone reused the
# API key).
MARKER_PREFIX = "issuefleet:msg:"


def marker(msg_id: str) -> str:
    return f"<!-- {MARKER_PREFIX}{msg_id} -->"
