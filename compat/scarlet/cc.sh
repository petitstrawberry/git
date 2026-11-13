#!/bin/sh
set -eu
: "${SCARLET_RUNTIME:?use nix develop}"
: "${SCARLET_CC:?use nix develop}"
: "${SCARLET_LD:?use nix develop}"
: "${SCARLET_RUST_TOOLCHAIN:?use nix develop}"
resource=$($SCARLET_CC -print-resource-dir)
for arg in "$@"; do
	case "$arg" in
		-c|-E|-S|--version)
			exec "$SCARLET_CC" --target=aarch64-unknown-none-elf -nostdinc \
				-isystem "$resource/include" -isystem "$SCARLET_RUNTIME/include" \
				-D__scarlet__=1 -mno-outline-atomics "$@"
			;;
	esac
done
"$SCARLET_CC" --target=aarch64-unknown-none-elf -nostdinc \
	-isystem "$resource/include" -isystem "$SCARLET_RUNTIME/include" \
	-D__scarlet__=1 -mno-outline-atomics -nostdlib -static \
	--ld-path="$SCARLET_LD" -Wl,--gc-sections,--no-undefined,-e,_start \
	-Wl,--wrap=main,--undefined=__wrap_main,-z,max-page-size=4096 \
	"$SCARLET_RUST_TOOLCHAIN/lib/rustlib/aarch64-unknown-scarlet/lib/scarlet-crt0.o" \
	"$@" -Wl,--start-group \
	"$(dirname "$0")/process/target/aarch64-unknown-scarlet/release/libgit_scarlet_process.a" \
	"$SCARLET_RUNTIME/lib/libscarlet_c_startup.a" \
	"$SCARLET_RUNTIME/lib/libscarlet_cxx_support.a" \
	"$SCARLET_RUNTIME/lib/libscarlet_c.a" \
	-Wl,--end-group
output=a.out
next_output=
for arg in "$@"; do
	if [ -n "$next_output" ]; then
		output=$arg
		next_output=
	elif [ "$arg" = -o ]; then
		next_output=1
	fi
done
python3 "$(dirname "$0")/mark-elf.py" "$output"
