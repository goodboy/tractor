{ pkgs ? import <nixpkgs> {} }:
let
  nativeBuildInputs = with pkgs; [
    stdenv.cc.cc.lib
    uv
  ];

in
pkgs.mkShell {
  inherit nativeBuildInputs;

  LD_LIBRARY_PATH = pkgs.lib.makeLibraryPath nativeBuildInputs;

  shellHook = ''
    set -e
    uv venv .venv --python=3.12
  '';
}
