# The single declaration of the homelab stack's root directory, sourced by
# every bazel wrapper. $HOME/.issuefleet works on all platforms: on macOS it
# lives inside /Users, which Docker Desktop shares by default; on Linux it
# needs no sudo. Override by exporting ISSUEFLEET_ROOT before `bazel run`.
export ISSUEFLEET_ROOT="${ISSUEFLEET_ROOT:-$HOME/.issuefleet}"
