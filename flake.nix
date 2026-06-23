{
  description = "bean-sync: LLM-assisted beancount transaction ingestion";

  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";
    flake-utils.url = "github:numtide/flake-utils";
  };

  outputs = { self, nixpkgs, flake-utils }:
    flake-utils.lib.eachDefaultSystem (system:
      let
        pkgs = nixpkgs.legacyPackages.${system};

        pythonEnv = pkgs.python3.withPackages (ps: with ps; [
          beancount
          requests
          secretstorage
          typer
          loguru
          pyyaml
          litellm
          pydantic
          questionary
          rich
          playwright
          playwright-stealth
          dbus-python
        ]);

        beanSync = pkgs.writeShellScriptBin "bean-sync" ''
          export PYTHONPATH="''${PYTHONPATH:+$PYTHONPATH:}$(pwd)"
          exec ${pythonEnv}/bin/python -m beancountio "$@"
        '';
      in {
        devShells.default = pkgs.mkShell {
          packages = [
            pythonEnv
            beanSync
            pkgs.beancount
            pkgs.beanquery
            pkgs.beancount-language-server
            pkgs.playwright-driver.browsers
          ];

          shellHook = ''
            alias check="bean-check main.bean"
            alias query="bean-query main.bean"
            export PLAYWRIGHT_BROWSERS_PATH=${pkgs.playwright-driver.browsers}
            export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
          '';
        };
      }
    );
}
