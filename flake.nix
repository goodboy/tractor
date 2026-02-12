# An "impure" template thx to `pyproject.nix`,
# https://pyproject-nix.github.io/pyproject.nix/templates.html#impure
# https://github.com/pyproject-nix/pyproject.nix/blob/master/templates/impure/flake.nix
{
  description = "An impure overlay (w dev-shell) using `uv`";

  inputs = {
    nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";
  };

  outputs =
    { nixpkgs, ... }:
    let
      inherit (nixpkgs) lib;
      forAllSystems = lib.genAttrs lib.systems.flakeExposed;
    in
    {
      devShells = forAllSystems (
        system:
        let
          pkgs = nixpkgs.legacyPackages.${system};

          # XXX NOTE XXX, for now we overlay specific pkgs via
          # a major-version-pinned-`cpython`
          cpython = "python313";
          venv_dir = "py313";
          pypkgs = pkgs."${cpython}Packages";
        in
        {
          default = pkgs.mkShell {

            packages = [
              # XXX, ensure sh completions activate!
              pkgs.bashInteractive
              pkgs.bash-completion

              # XXX, on nix(os), use pkgs version to avoid
              # build/sys-sh-integration issues
              pkgs.ruff

              pkgs.uv
              pkgs.${cpython}# ?TODO^ how to set from `cpython` above?
            ];

            shellHook = ''
              # unmask to debug **this** dev-shell-hook
              # set -e

              # link-in c++ stdlib for various AOT-ext-pkgs (numpy, etc.)
              LD_LIBRARY_PATH="${pkgs.stdenv.cc.cc.lib}/lib:$LD_LIBRARY_PATH"

              export LD_LIBRARY_PATH

              # RUNTIME-SETTINGS
              # ------ uv ------
              # - always use the ./py313/ venv-subdir
              # - sync env with all extras
              export UV_PROJECT_ENVIRONMENT=${venv_dir}
              uv sync --dev --all-extras

              # ------ TIPS ------
              # NOTE, to launch the py-venv installed `xonsh` (like @goodboy)
              # run the `nix develop` cmd with,
              # >> nix develop -c uv run xonsh
            '';
          };
        }
      );
    };
}
