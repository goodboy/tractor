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

          # shared base toolchain for every dev-shell variant;
          # extra-tooling shells (eg. `docs`, below) extend it
          # so heavy/niche deps stay OUT of the `default` shell.
          basePkgs = [
            # XXX, ensure sh completions activate!
            pkgs.bashInteractive
            pkgs.bash-completion

            # XXX, on nix(os), use pkgs version to avoid
            # build/sys-sh-integration issues
            pkgs.ruff

            pkgs.uv
            pkgs.${cpython}# ?TODO^ how to set from `cpython` above?
          ];

          baseHook = ''
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
        in
        {
          default = pkgs.mkShell {
            packages = basePkgs;
            shellHook = baseHook;
          };

          # OPT-IN docs shell: `default` + the `d2` diagram
          # renderer, kept OUT of `default` so casual dev shells
          # don't pull it in. Enter with,
          #   >> nix develop .#docs
          # then build (the `.. d2::` directive auto-detects the
          # `d2` bin now on PATH; see `docs/_ext/d2diagrams.py`),
          #   >> uv run --group docs make -C docs html
          docs = pkgs.mkShell {
            packages = basePkgs ++ [ pkgs.d2 ];
            shellHook = baseHook + ''
              echo "[docs] d2 $(d2 --version) on PATH \
              — build via: uv run --group docs make -C docs html"
            '';
          };
        }
      );
    };
}
