#!/bin/sh
set -eu
: "${SCARLET_RUNTIME:?use nix develop}"
: "${SCARLET_CC:?use nix develop}"
resource=$($SCARLET_CC -print-resource-dir)
# cc-rs derives a target option from the custom Rust triple; use a raw ELF
# compiler target after it, while retaining the native runtime's headers.
exec "$SCARLET_CC" "$@" --target=aarch64-unknown-none-elf -nostdinc \
    -isystem "$resource/include" -isystem "$SCARLET_RUNTIME/include" \
    -D__scarlet__=1 -mno-outline-atomics -fno-stack-protector
