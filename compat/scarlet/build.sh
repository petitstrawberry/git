#!/bin/sh
set -eu
cd "$(dirname "$0")/../.."
root=$(pwd)
build="$root/.scarlet-build"
python3 compat/scarlet/build_inputs.py --write "$build/build-inputs.json"
python3 compat/scarlet/build_zlib.py
export RUSTC="$SCARLET_RUST_TOOLCHAIN/bin/rustc"
"$SCARLET_RUST_TOOLCHAIN/bin/cargo" build --locked --release \
    --manifest-path compat/scarlet/process/Cargo.toml \
    --target aarch64-unknown-scarlet
compat/scarlet/http/build.sh
exec make uname_S=Scarlet \
    CC="$root/compat/scarlet/cc.sh" AR="$SCARLET_AR" \
    ZLIB_PATH="$build/zlib" CFLAGS="-O2 -g" \
    NO_CURL= CURL_CONFIG="$root/compat/scarlet/http/target/install/bin/curl-config" \
    prefix=/usr git git-remote-http git-http-fetch "${@}"
