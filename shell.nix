let
  pkgs = import <nixpkgs> {};
in
  pkgs.mkShell {
    buildInputs = with pkgs; [
      gettext
      (python311.withPackages (ps: with ps; [
        mpd2
        dbus-fast
        babel
        pytest
        pytest-asyncio
        mypy
        ruff
        build
      ]))
    ];
  }
