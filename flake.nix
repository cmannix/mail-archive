{
  description = "A provider-independent archive of email in Maildir, indexed by notmuch";

  inputs.nixpkgs.url = "github:nixos/nixpkgs/nixos-unstable";

  outputs = {
    self,
    nixpkgs,
  }: let
    systems = ["x86_64-linux" "aarch64-linux" "aarch64-darwin"];
    forAll = f: nixpkgs.lib.genAttrs systems (system: f nixpkgs.legacyPackages.${system});
  in {
    packages = forAll (pkgs: {
      default = pkgs.python3Packages.buildPythonApplication {
        pname = "mail-archive";
        version = "0.1.0";
        pyproject = true;
        src = ./.;
        build-system = [pkgs.python3Packages.setuptools];
        nativeCheckInputs = [pkgs.python3Packages.pytestCheckHook pkgs.notmuch];
        # The commands run notmuch.
        makeWrapperArgs = ["--prefix" "PATH" ":" "${pkgs.notmuch}/bin"];
        meta.mainProgram = "mail-archive-import";
      };
    });

    # The tests run as part of the package build.
    checks = forAll (pkgs: {default = self.packages.${pkgs.stdenv.hostPlatform.system}.default;});

    devShells = forAll (pkgs: {
      default = pkgs.mkShell {packages = [pkgs.python3 pkgs.python3Packages.pytest pkgs.notmuch];};
    });

    formatter = forAll (pkgs: pkgs.alejandra);
  };
}
