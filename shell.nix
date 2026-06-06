let
  pkgs = import <nixpkgs> {};
in
  pkgs.mkShell {
    buildInputs = with pkgs; [
      (python311.withPackages (ps: with ps; [
        mpd2
        dbus-fast
        pytest
        pytest-asyncio
        mypy
        ruff
        build
      ]))
    ];
  }
