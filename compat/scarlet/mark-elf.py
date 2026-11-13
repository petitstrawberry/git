#!/usr/bin/env python3
"""Mark the ELF linked against the native Scarlet CRT and runtime."""
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
with path.open("r+b") as stream:
    header = stream.read(64)
    if (len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01"
            or int.from_bytes(header[16:18], "little") not in (2, 3)
            or int.from_bytes(header[18:20], "little") not in (183, 243)
            or header[7] not in (0, 0x53)):
        raise SystemExit(f"not a supported native ELF executable: {path}")
    stream.seek(7)
    stream.write(b"\x53")
