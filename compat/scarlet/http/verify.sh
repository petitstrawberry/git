#!/bin/sh
set -eu
cd "$(dirname "$0")/../../.."
http=compat/scarlet/http
: "${SCARLET_RUNTIME:?use nix develop}"
: "${SCARLET_LD:?use nix develop}"
zlib=${SCARLET_ZLIB_PREFIX:-"$PWD/.scarlet-build/zlib"}
python3 - "$http/target/install/lib/libcurl.a" <<'PY'
import os
from pathlib import Path
import subprocess
import sys
nm = Path(os.environ['SCARLET_AR']).with_name('llvm-nm')
def defined(path):
    listing = subprocess.check_output(
        [nm, '--defined-only', '--extern-only', '--format=posix', path], text=True
    )
    return {fields[0] for line in listing.splitlines()
            if len(fields := line.split()) >= 2 and fields[1] in {'T', 'D', 'B', 'R', 'S', 'G', 'A'}}
curl = defined(sys.argv[1])
runtime = Path(os.environ['SCARLET_RUNTIME']) / 'lib'
for library in ('libscarlet_c.a', 'libscarlet_c_startup.a', 'libscarlet_cxx_support.a'):
    duplicates = curl & defined(runtime / library)
    if duplicates:
        raise SystemExit(f'duplicate strong symbols in {library}: {sorted(duplicates)}')
print('No duplicated strong global symbols with the shared native runtime.')
PY
compat/scarlet/cc.sh -O2 -ffunction-sections -fdata-sections -fno-stack-protector \
    -I"$http/target/install/include" -c "$http/link-check.c" \
    -o "$http/target/link-check.o"
# Whole-archive forces every dependency object through the linker, exposing
# duplicate runtime definitions instead of relying on archive ordering.
compat/scarlet/cc.sh "$http/target/link-check.o" \
    -Wl,--whole-archive "$http/target/install/lib/libcurl.a" \
    -Wl,--no-whole-archive "$zlib/lib/libz.a" -o "$http/target/link-check"
python3 - "$http/target/link-check" <<'PY'
from pathlib import Path
import sys
path = Path(sys.argv[1])
image = bytearray(path.read_bytes())
if image[:7] != b'\x7fELF\x02\x01\x01' or int.from_bytes(image[18:20], 'little') != 183:
    raise SystemExit('link check did not produce AArch64 ELF')
# The generic ELF linker does not know Scarlet's native OSABI marker.
image[7] = 83
path.write_bytes(image)
PY
echo "Native C API smoke executable: $PWD/$http/target/link-check"
