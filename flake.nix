{
  description = "Git for the native Scarlet ABI";
  nixConfig = {
    extra-substituters = [ "https://scarlet-rust-toolchain.cachix.org" ];
    extra-trusted-public-keys = [ "scarlet-rust-toolchain.cachix.org-1:p+coBExi0nNTIvWF/oM9H9/1/GhwFtqGZ2Vs+4pYl6o=" ];
  };
  inputs.scarlet-sdk.url = "github:petitstrawberry/scarlet-sdk/515e5e47eec47b2da559e8faef346fee9dc0a2bf";
  inputs.nixpkgs.follows = "scarlet-sdk/nixpkgs";
  outputs = { self, nixpkgs, scarlet-sdk }:
    let
      systems = [ "aarch64-darwin" "x86_64-darwin" "aarch64-linux" "x86_64-linux" ];
      eachSystem = nixpkgs.lib.genAttrs systems;
    in {
      devShells = eachSystem (system:
        let
          pkgs = import nixpkgs { inherit system; };
          llvm = pkgs.llvmPackages_21;
          rust = scarlet-sdk.inputs.scarlet-rust-toolchain.packages.${system}.scarlet-rust-toolchain;
        in { default = pkgs.mkShell {
          packages = [ rust llvm.clang-unwrapped llvm.llvm llvm.lld pkgs.gnumake pkgs.pkg-config pkgs.python3 pkgs.git ];
          SCARLET_RUNTIME = scarlet-sdk.packages.${system}.scarlet-native-runtime;
          SCARLET_SDK_SOURCE = scarlet-sdk.outPath;
          SCARLET_RUST_TOOLCHAIN = rust;
          SCARLET_CC = "${llvm.clang-unwrapped}/bin/clang";
          SCARLET_AR = "${llvm.llvm}/bin/llvm-ar";
          SCARLET_LD = "${llvm.lld}/bin/ld.lld";
          SCARLET_ZLIB_SOURCE = pkgs.zlib.src;
          SCARLET_CA_BUNDLE = "${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
        }; });
    };
}
