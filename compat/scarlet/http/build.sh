#!/bin/sh
set -eu
cd "$(dirname "$0")"
http_root=$(pwd)
git_root=$(cd ../../.. && pwd)
: "${SCARLET_RUNTIME:?run nix develop --command compat/scarlet/http/build.sh}"
: "${SCARLET_RUST_TOOLCHAIN:?use nix develop}"
: "${SCARLET_AR:?use nix develop}"
SCARLET_BUILD_ID=$(python3 ../build_inputs.py)
export SCARLET_BUILD_ID
zlib=${SCARLET_ZLIB_PREFIX:-"$git_root/.scarlet-build/zlib"}
test -f "$zlib/include/zlib.h" || {
    echo "Build Git's native zlib first, or set SCARLET_ZLIB_PREFIX." >&2
    exit 1
}
python3 prepare.py
export CARGO_TARGET_DIR="$http_root/target/cargo"
export CC_aarch64_unknown_scarlet="$http_root/cc.sh"
export AR_aarch64_unknown_scarlet="$SCARLET_AR"
export CFLAGS_aarch64_unknown_scarlet="-ffunction-sections -fdata-sections -DSCARLET_BUILD_ID=scarlet_$SCARLET_BUILD_ID"
export RUSTC="$SCARLET_RUST_TOOLCHAIN/bin/rustc"
# Extra metadata is additive to Cargo's per-crate metadata. Both cc-rs and
# Cargo must observe SDK/header/compiler changes hidden behind the wrapper.
CARGO_ENCODED_RUSTFLAGS=$(printf '%s\037%s' '-Cembed-bitcode=no' "-Cmetadata=scarlet_$SCARLET_BUILD_ID")
export CARGO_ENCODED_RUSTFLAGS
# The zlib links override makes curl-sys use Git's existing zlib headers and
# library. In particular, it must not build a separate copy of zlib/libc.
python3 - "$zlib" <<'PY'
import json, sys
from pathlib import Path
prefix = Path(sys.argv[1]).resolve()
text = ('[target.aarch64-unknown-scarlet.z]\n'
        'rustc-link-lib = ["z"]\n'
        'rustc-link-search = ' + json.dumps([str(prefix / 'lib')]) + '\n'
        'include = ' + json.dumps(str(prefix / 'include')) + '\n')
config = Path('target/zlib.toml')
if not config.exists() or config.read_text() != text:
    config.write_text(text)
PY
"$SCARLET_RUST_TOOLCHAIN/bin/cargo" build --locked --release \
    --target aarch64-unknown-scarlet --config target/zlib.toml \
    --message-format=json-render-diagnostics "$@" > target/artifacts.jsonl
python3 package.py
