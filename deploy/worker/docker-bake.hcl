group "default" {
  targets = ["worker"]
}

# This source contains the exact 1.7.0 launcher verified with IssueFleet.
# Build its Ubuntu base and entrypoint together instead of relying on a
# locally cached image tag that may not exist in a public registry.
target "claude-base" {
  context = "https://github.com/fughilli/claude-container.git#4cf93d4a062563470f332902785c3b603d9b2c29:claude-code"
}

target "worker" {
  context    = "."
  dockerfile = "deploy/worker/Dockerfile"
  contexts = {
    claude_base = "target:claude-base"
  }
  tags = ["issuefleet-worker:codex"]
}
