# An "impure" template thx to `pyproject.nix`,
# https://pyproject-nix.github.io/pyproject.nix/templates.html#impure
# https://github.com/pyproject-nix/pyproject.nix/blob/master/templates/impure/flake.nix
{
  description = "An impure overlay using `uv` with Nix(OS)";

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

            packages = with pkgs; [
              # XXX, ensure sh completions activate!
              bashInteractive
              bash-completion

              # on nixos, use pkg(s)
              ruff
              pypkgs.ruff

              uv
              python313  # ?TODO^ how to set from `cpython` above?
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
              uv sync --dev --all-extras --no-group lint

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
