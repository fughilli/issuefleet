{
  description = "issuefleet dev shell — Bazel (via bazelisk), Python 3.11, tmux";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-25.05";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};
      in
      {
        # The runtime needs only Python 3.11+ stdlib; this shell exists so a
        # host without Homebrew can still run the daemon, the tests (via
        # bazelisk), and the tmux-based worker runner.
        devShells.default = pkgs.mkShell {
          packages = with pkgs; [
            bazelisk
            python311
            tmux
            git
          ];
        };
      });
}
