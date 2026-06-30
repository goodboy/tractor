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
  TMPDIR = "/tmp";
}
