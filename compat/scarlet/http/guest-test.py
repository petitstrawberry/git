#!/usr/bin/env python3
"""Boot a private Scarlet HTTP fixture; retain logs, never modify source images."""
import argparse
import atexit
import hashlib
import json
import os
from pathlib import Path
import selectors
import shutil
import stat
import subprocess
import time
import urllib.request

HERE = Path(__file__).resolve().parent
GIT_URL = "https://github.com/octocat/Hello-World.git"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def cpio(root, output):
    with output.open("wb") as stream:
        paths = [root, *sorted(root.rglob("*"))]
        for inode, path in enumerate([*paths, None], 1):
            if path is None:
                name, mode, contents = "TRAILER!!!", 0, b""
            else:
                name = "." if path == root else path.relative_to(root).as_posix()
                mode = path.lstat().st_mode
                contents = (os.fsencode(os.readlink(path)) if stat.S_ISLNK(mode)
                            else path.read_bytes() if stat.S_ISREG(mode) else b"")
            encoded = name.encode() + b"\0"
            fields = (inode, mode, 0, 0, 1, 0, len(contents), 0, 0, 0, 0, len(encoded), 0)
            stream.write(b"070701" + b"".join(f"{value:08x}".encode() for value in fields))
            stream.write(encoded)
            stream.write(b"\0" * (-(110 + len(encoded)) % 4))
            stream.write(contents)
            stream.write(b"\0" * (-len(contents) % 4))
        stream.write(b"\0" * (-stream.tell() % 512))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", type=Path, required=True)
    parser.add_argument("--rootfs", type=Path, required=True)
    parser.add_argument("--bootstrap", type=Path, required=True,
                        help="paired userspace release directory with init and scarlet-ld")
    parser.add_argument("--format", choices=("image", "limine"), default="image")
    parser.add_argument("--limine-plugin", type=Path)
    parser.add_argument("--limine-cache", type=Path)
    parser.add_argument("--firmware-code", type=Path)
    parser.add_argument("--firmware-vars", type=Path)
    parser.add_argument("--toolchain", type=Path, required=True)
    parser.add_argument("--qemu", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--url", default="https://www.kernel.org/")
    parser.add_argument("--git", type=Path, help="enable native Git smart HTTPS acceptance")
    parser.add_argument("--git-remote-http", type=Path)
    parser.add_argument("--git-http-fetch", type=Path)
    parser.add_argument("--pipe-metadata-source", type=Path,
                        help="compile/run the kernel's narrow native C metadata probe")
    parser.add_argument("--accel", choices=("hvf", "tcg"), default="hvf")
    parser.add_argument("--timeout", type=float, default=150)
    args = parser.parse_args()
    if not args.url.startswith("https://"):
        parser.error("the test URL must use HTTPS")
    if args.format == "limine" and not all((args.limine_plugin, args.limine_cache,
                                           args.firmware_code, args.firmware_vars)):
        parser.error("Limine boot requires plugin, cache, and firmware paths")
    output = args.output.resolve()
    # Every mutable artifact belongs to this component's ignored target tree.
    if HERE / "target" not in output.parents:
        parser.error("output must be within compat/scarlet/http/target/")
    output.mkdir(parents=True, exist_ok=False)
    def cleanup():
        for name in ("rootfs", "limine-cache", ".limine-work"):
            shutil.rmtree(output / name, ignore_errors=True)
        for name in ("initramfs.cpio", "boot.img", "efi-vars.fd", "wrong-ca-key.pem"):
            (output / name).unlink(missing_ok=True)
    atexit.register(cleanup)
    staging = output / "rootfs"
    for directory in ("bin", "dev", "etc/netcfgd.d", "etc/ssl/certs", "tmp", "root",
                      "mnt/newroot", "fixtures/empty-template", "usr/bin", "usr/libexec/git-core"):
        (staging / directory).mkdir(parents=True, exist_ok=True)
    bootstrap_bins = args.bootstrap / "bin" if (args.bootstrap / "bin").is_dir() else args.bootstrap
    inputs = {
        "init": args.bootstrap / "init",
        "bin/scarlet-ld": bootstrap_bins / "scarlet-ld",
        "bin/netcfgd": bootstrap_bins / "netcfgd",
        "bin/resolverd": args.rootfs / "bin/resolverd",
        "bin/sh": args.rootfs / "bin/sh",
        "bin/curl-link-check": HERE / "target/link-check",
        "etc/ssl/certs/ca-certificates.crt": args.rootfs / "etc/ssl/certs/ca-certificates.crt",
    }
    host_git = None
    if args.git:
        inputs.update({
            "usr/bin/git": args.git.resolve(),
            "usr/libexec/git-core/git-remote-http": (args.git_remote_http or args.git.with_name("git-remote-http")).resolve(),
            "usr/libexec/git-core/git-http-fetch": (args.git_http_fetch or args.git.with_name("git-http-fetch")).resolve(),
        })
        host_command = ["git", "ls-remote", "--symref", GIT_URL, "HEAD"]
        listing = subprocess.check_output(host_command, text=True, timeout=30)
        (output / "host-ls-remote.txt").write_text(listing)
        heads = [line.split()[0] for line in listing.splitlines()
                 if not line.startswith("ref:") and line.split()[-1:] == ["HEAD"]]
        if len(heads) != 1 or len(heads[0]) != 40 or any(c not in "0123456789abcdef" for c in heads[0]):
            raise RuntimeError("host ls-remote did not return one SHA-1 HEAD")
        head = heads[0]
        readme_url = f"https://raw.githubusercontent.com/octocat/Hello-World/{head}/README"
        with urllib.request.urlopen(readme_url, timeout=30) as response:
            readme = response.read(1024 * 1024 + 1)
        if not readme or len(readme) > 1024 * 1024:
            raise RuntimeError("unexpected host README size")
        (staging / "fixtures/git-README").write_bytes(readme)
        (staging / "fixtures/git-url").write_text(GIT_URL + "\n")
        (staging / "fixtures/git-head").write_text(head + "\n")
        (output / "host-README").write_bytes(readme)
        host_git = {"command": host_command, "url": GIT_URL, "head": head,
                    "readme_url": readme_url, "readme_sha256": hashlib.sha256(readme).hexdigest()}
    for name, source in inputs.items():
        shutil.copyfile(source, staging / name)
        if name == "init" or name.startswith(("bin/", "usr/")):
            (staging / name).chmod(0o755)
    if args.git:
        helpers = staging / "usr/libexec/git-core"
        (helpers / "git-remote-https").symlink_to("git-remote-http")
        (helpers / "git").symlink_to("../../bin/git")
    (staging / "etc/netcfgd.d/10-http.toml").write_text('''[network]
dhcp_timeout_ms = 1500
dhcp_attempts = 3
[[interface]]
name = "*"
method = "dhcp"
metric = 100
''')
    (staging / "etc/resolv.conf").write_text("")
    (staging / "fixtures/url").write_text(args.url + "\n")
    (staging / "fixtures/host-epoch").write_text(str(int(time.time())) + "\n")
    with (output / "prepare.log").open("w") as log:
        key = output / "wrong-ca-key.pem"
        try:
            subprocess.run([
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                "-subj", "/CN=Scarlet HTTP unrelated test CA", "-keyout", key,
                "-out", staging / "fixtures/wrong-ca.pem",
            ], check=True, stdout=log, stderr=subprocess.STDOUT)
        finally:
            key.unlink(missing_ok=True)
        subprocess.run([
            args.toolchain / "bin/rustc", "--edition=2024", "--target", "aarch64-unknown-scarlet",
            "-O", "-C", "panic=abort", HERE / "guest.rs", "-o", staging / "bin/http-smoke",
        ], check=True, stdout=log, stderr=subprocess.STDOUT)
        if args.pipe_metadata_source:
            subprocess.run([
                HERE.parent / "cc.sh", "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                args.pipe_metadata_source, "-o", staging / "bin/pipe-metadata-test",
            ], check=True, stdout=log, stderr=subprocess.STDOUT)
    archive = output / "initramfs.cpio"
    cpio(staging, archive)
    cmdline = "console=ttyAMA0 init=/init init.console=/dev/tty0 init.exec=/bin/http-smoke"
    command = [
        str(args.qemu), "-machine", "virt,gic-version=3,acpi=off", "-accel", args.accel,
        "-cpu", "host" if args.accel == "hvf" else "max", "-m", "2G", "-smp", "2",
        "-display", "none", "-monitor", "none", "-serial", "stdio", "-no-reboot",
        "-global", "virtio-mmio.force-legacy=false", "-rtc", "base=utc",
        "-netdev", "user,id=httpnet", "-device",
        "virtio-net-device,netdev=httpnet,bus=virtio-mmio-bus.1",
        "-object", "rng-random,id=entropy,filename=/dev/urandom", "-device",
        "virtio-rng-device,rng=entropy,bus=virtio-mmio-bus.6",
    ]
    if args.format == "image":
        command += ["-kernel", str(args.kernel), "-initrd", str(archive), "-append", cmdline]
    else:
        cache = output / "limine-cache/limine-12.4.0"
        cache.mkdir(parents=True)
        shutil.copyfile(args.limine_cache / "limine-12.4.0/BOOTAA64.EFI", cache / "BOOTAA64.EFI")
        with (output / "image-build.log").open("w") as log:
            subprocess.run([
                args.limine_plugin, "--arch", "aarch64", "--kernel", args.kernel,
                "--initramfs", archive, "--output", output / "boot.img",
                "--cache-dir", cache.parent, "--cmdline", cmdline,
            ], check=True, stdout=log, stderr=subprocess.STDOUT)
        shutil.rmtree(cache.parent)
        variables = output / "efi-vars.fd"
        shutil.copyfile(args.firmware_vars, variables)
        variables.chmod(0o600)
        command += [
            "-drive", f"if=pflash,format=raw,unit=0,file={args.firmware_code},readonly=on",
            "-drive", f"if=pflash,format=raw,unit=1,file={variables}",
            "-drive", f"id=boot,if=none,format=raw,file={output / 'boot.img'}",
            "-device", "virtio-blk-device,drive=boot,bus=virtio-mmio-bus.0",
            "-device", "virtio-gpu-device,bus=virtio-mmio-bus.3",
        ]
    manifest = {"command": command, "url": args.url,
                "host_git": host_git,
                "pipe_metadata_probe": ({"source": str(args.pipe_metadata_source),
                                         "source_sha256": digest(args.pipe_metadata_source),
                                         "elf_sha256": digest(staging / "bin/pipe-metadata-test")}
                                        if args.pipe_metadata_source else None),
                "kernel": {"path": str(args.kernel), "sha256": digest(args.kernel)},
                "inputs": {name: {"path": str(path), "sha256": digest(staging / name)}
                           for name, path in inputs.items()},
                "wrong_ca_sha256": digest(staging / "fixtures/wrong-ca.pem"),
                "fixture_sha256": digest(staging / "bin/http-smoke")}
    (output / "commands.json").write_text(json.dumps(manifest, indent=2) + "\n")
    started = time.monotonic()
    result, cases, pending = "TIMEOUT", [], b""
    details = {}
    clock = None
    with (output / "serial.log").open("wb") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while time.monotonic() - started < args.timeout:
                    if not selector.select(0.2):
                        if process.poll() is not None:
                            result = "QEMU exited without result"
                            break
                        continue
                    data = os.read(process.stdout.fileno(), 65536)
                    if not data:
                        result = "QEMU closed serial without result"
                        break
                    log.write(data)
                    log.flush()
                    pending += data
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        text = line.decode(errors="replace").strip()
                        if "SCARLET_HTTP_" in text or text.startswith("[resolverd]"):
                            print(text, flush=True)
                        if text.startswith("SCARLET_HTTP_CASE "):
                            cases.append(text)
                        if text.startswith("SCARLET_HTTP_DETAIL "):
                            _, case, detail = text.split(" ", 2)
                            details.setdefault(case, []).append(detail)
                        if text.startswith("SCARLET_HTTP_CLOCK "):
                            clock = dict(field.split("=", 1) for field in text.split()[1:])
                        if text.startswith("SCARLET_HTTP_RESULT "):
                            result = text.removeprefix("SCARLET_HTTP_RESULT ")
                        if "panicked at" in text or "[Scarlet Kernel] panic:" in text:
                            result = "PANIC " + text
                    if result != "TIMEOUT":
                        break
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    expected = ["SCARLET_HTTP_CASE trusted_https PASS exit=0",
                "SCARLET_HTTP_CASE wrong_ca_rejected PASS exit=60"]
    if args.pipe_metadata_source:
        expected.insert(0, "SCARLET_HTTP_CASE pipe_metadata PASS exit=0")
    if host_git:
        expected += [f"SCARLET_HTTP_CASE git_ls_remote PASS head={host_git['head']}",
                     f"SCARLET_HTTP_CASE git_shallow_clone PASS head={host_git['head']}",
                     "SCARLET_HTTP_CASE git_fsck PASS"]
    if result == "PASS" and cases != expected:
        result = "FAIL incomplete or duplicate case evidence"
    report = {"result": result, "cases": cases,
              "details": details, "clock": clock,
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "qemu_exit_code": process.returncode}
    (output / "result.json").write_text(json.dumps(report, indent=2) + "\n")
    # Only compact evidence remains; source images and input rootfs are untouched.
    cleanup()
    print(json.dumps(report), flush=True)
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
