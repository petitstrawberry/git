#!/usr/bin/env python3
"""Run the native Git acceptance fixture using private, disposable boot images."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import stat
import subprocess
import sys
import tempfile
import time

CASES = ("dup2", "version", "init", "status", "add", "commit", "diff", "branch_checkout",
         "localclone", "fetch", "push", "merge", "hooks", "fsck", "cancellation")
PANIC = re.compile(rb"panicked at|kernel panic|\[panic\]|Panic occurred:|\[Scarlet Kernel\] panic:", re.I)
TARGETS = {"aarch64": (183, "aarch64-unknown-scarlet"),
           "riscv64": (243, "riscv64gc-unknown-scarlet")}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def native_elf(path, arch):
    with path.open("rb") as stream:
        header = stream.read(64)
    if (len(header) != 64 or header[:7] != b"\x7fELF\x02\x01\x01"
            or header[7] != 83 or int.from_bytes(header[18:20], "little") != TARGETS[arch][0]
            or int.from_bytes(header[16:18], "little") not in (2, 3)):
        raise ValueError(f"not a native Scarlet {arch} executable: {path}")


def archive_entry(stream, name, mode, contents, inode):
    name = os.fsencode(name) + b"\0"
    values = (inode, mode, 0, 0, 1, 0, len(contents), 0, 0, 0, 0, len(name), 0)
    stream.write(b"070701" + b"".join(f"{v:08x}".encode() for v in values))
    stream.write(name)
    stream.write(b"\0" * (-(110 + len(name)) % 4))
    stream.write(contents)
    stream.write(b"\0" * (-len(contents) % 4))


def create_initramfs(staging, destination):
    with destination.open("wb") as stream:
        paths = [staging, *sorted(staging.rglob("*"))]
        for inode, path in enumerate(paths, 1):
            mode = path.lstat().st_mode
            name = "." if path == staging else path.relative_to(staging).as_posix()
            if stat.S_ISREG(mode):
                contents = path.read_bytes()
            elif stat.S_ISLNK(mode):
                contents = os.fsencode(os.readlink(path))
            elif stat.S_ISDIR(mode):
                contents = b""
            else:
                raise ValueError(f"unsupported staging entry: {path}")
            archive_entry(stream, name, mode, contents, inode)
        archive_entry(stream, "TRAILER!!!", 0, b"", len(paths) + 1)
        stream.write(b"\0" * (-stream.tell() % 512))


def executable(value):
    path = shutil.which(str(value))
    if path is None:
        raise ValueError(f"executable not found: {value}")
    return str(Path(path).resolve())


def firmware(explicit, names):
    for value in [explicit, *(os.environ.get(name) for name in names)]:
        if value and Path(value).is_file():
            return Path(value).resolve()
    raise ValueError("provide firmware path or set " + " / ".join(names))


def run_logged(command, output, log, timeout=180, env=None):
    with (output / log).open("wb") as stream:
        subprocess.run([str(x) for x in command], stdout=stream, stderr=subprocess.STDOUT,
                       check=True, timeout=timeout, env=env)


class Markers:
    def __init__(self, cases=CASES):
        self.expected = tuple(cases)
        self.cases = []
        self.result = None

    def line(self, line):
        if self.result is not None:
            return
        line = line.rstrip(b"\r\n")
        if PANIC.search(line):
            self.result = "guest panic"
        elif line.startswith(b"SCARLET_GIT_SMOKE FAIL "):
            self.result = line.decode(errors="replace")
        elif line.startswith(b"SCARLET_GIT_CASE "):
            expected = f"SCARLET_GIT_CASE {self.expected[len(self.cases)]} PASS".encode() if len(self.cases) < len(self.expected) else None
            if line != expected:
                self.result = "unexpected or duplicate case marker"
            else:
                self.cases.append(self.expected[len(self.cases)])
        elif line.startswith(b"SCARLET_GIT_SMOKE PASS"):
            self.result = ("PASS" if self.cases == list(self.expected)
                           and line == f"SCARLET_GIT_SMOKE PASS cases={len(self.expected)}".encode()
                           else "incomplete case markers")


def run_guest(command, output, timeout, cases=CASES):
    markers = Markers(cases)
    started = time.monotonic()
    pending = b""
    with (output / "serial.log").open("wb") as serial, subprocess.Popen(
            command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT) as process:
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while markers.result is None:
                    remaining = timeout - (time.monotonic() - started)
                    if remaining <= 0:
                        markers.result = "guest timeout"
                        break
                    if not selector.select(min(remaining, 0.25)):
                        if process.poll() is not None:
                            break
                        continue
                    chunk = os.read(process.stdout.fileno(), 65536)
                    if not chunk:
                        break
                    serial.write(chunk)
                    serial.flush()
                    sys.stdout.buffer.write(chunk)
                    sys.stdout.buffer.flush()
                    pending += chunk
                    while b"\n" in pending and markers.result is None:
                        line, pending = pending.split(b"\n", 1)
                        markers.line(line)
                    if len(pending) > 4 * 1024 * 1024:
                        markers.result = "serial line exceeded 4 MiB"
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return {"result": markers.result or "QEMU exited without a complete result",
            "cases_passed": markers.cases, "qemu_exit_code": process.returncode,
            "elapsed_seconds": round(time.monotonic() - started, 3)}


def qemu_path(path):
    return str(path).replace(",", ",,")


def main(argv=None, *, fixture=None):
    """An optional fixture supplies staging, QEMU arguments and final checks.

    ext2-run.py uses this to retain the same boot, watchdog and artifact policy.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kernel", required=True, type=Path)
    parser.add_argument("--rootfs", required=True, type=Path, help="existing staged root directory with init, bin/scarlet-ld and bin/sh")
    parser.add_argument("--toolchain", required=True, type=Path, help="Scarlet Rust toolchain prefix")
    parser.add_argument("--git", type=Path, default=Path(__file__).resolve().parents[3] / "git")
    parser.add_argument("--dup2-probe", type=Path, help="prebuilt native dup2.c ELF; otherwise compile with --c-compiler")
    parser.add_argument("--c-compiler", type=Path, default=Path(__file__).resolve().parents[1] / "cc.sh",
                        help="native C compiler wrapper (default: ../cc.sh; requires SCARLET_RUNTIME, SCARLET_CC, SCARLET_LD)")
    parser.add_argument("--arch", choices=TARGETS, default="aarch64")
    parser.add_argument("--accel", choices=("tcg", "hvf"), default="tcg")
    parser.add_argument("--kernel-format", choices=("limine", "image"), default="limine", help="image: AArch64 Linux-boot Image, without EFI")
    parser.add_argument("--qemu", help="QEMU executable; defaults to qemu-system-ARCH")
    parser.add_argument("--limine-plugin", default="cargo-scarlet-plugin-limine")
    parser.add_argument("--limine-cache", type=Path, help="existing cache containing limine-12.4.0; copied privately")
    parser.add_argument("--firmware-code", type=Path)
    parser.add_argument("--firmware-vars", type=Path)
    parser.add_argument("--output", type=Path, help="new output directory; default: fresh temporary directory")
    parser.add_argument("--timeout", type=float, default=240)
    parser.add_argument("--command-timeout", type=int, default=30)
    parser.add_argument("--memory", choices=("2G", "4G", "8G"), help="default: AArch64 2G, RV64 4G")
    parser.add_argument("--smp", type=int, choices=(1, 2, 4), default=2)
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--keep-image", action="store_true", help="retain private images and staging after the run for debugging")
    args = parser.parse_args(argv)
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.command_timeout <= 0:
        parser.error("timeouts must be positive and finite")
    if args.arch != "aarch64" and (args.accel == "hvf" or args.kernel_format == "image"):
        parser.error("HVF and direct Image boot are AArch64-only")
    rootfs, kernel, git, toolchain = (p.resolve() for p in (args.rootfs, args.kernel, args.git, args.toolchain))
    if not rootfs.is_dir():
        parser.error("--rootfs must be an existing staged directory, not a disk image")
    if not kernel.is_file():
        parser.error("kernel is missing")
    qemu = executable(args.qemu or f"qemu-system-{args.arch}")
    rustc = executable(toolchain / "bin/rustc")
    inputs = {"git": git, "kernel": kernel, "init": rootfs / "init",
              "loader": rootfs / "bin/scarlet-ld", "shell": rootfs / "bin/sh",
              "harness_source": fixture.source if fixture else Path(__file__).with_name("guest.rs").resolve(),
              "hook_source": Path(__file__).with_name("hook.rs").resolve(),
              "probe_source": Path(__file__).with_name("dup2.c").resolve()}
    for name in ("git", "init", "loader", "shell"):
        native_elf(inputs[name], args.arch)
    if args.dup2_probe:
        inputs["dup2_probe"] = args.dup2_probe.resolve()
        native_elf(inputs["dup2_probe"], args.arch)
    if args.output:
        output = args.output.absolute()
        if any(source == output.resolve() or source in output.resolve().parents for source in (rootfs, toolchain)):
            parser.error("--output must be outside the input rootfs and toolchain")
        # Never overwrite an earlier result, a source checkout or another VM.
        output.mkdir(parents=True, exist_ok=False)
        output = output.resolve()
    else:
        output = Path(tempfile.mkdtemp(prefix="git-scarlet-smoke-"))
    print(f"ARTIFACTS={output}", flush=True)
    (output / "result.json").write_text(json.dumps({"result": "PREPARING"}) + "\n")
    try:
        kernel_copy = output / ("Image" if args.kernel_format == "image" else "kernel.elf")
        shutil.copyfile(kernel, kernel_copy)
        staging = output / "staging"
        for directory in ("bin", "usr/bin", "usr/libexec/git-core", "etc", "root", "dev", "tmp", "mnt/newroot", "fixtures/empty-template", "fixtures/hooks"):
            (staging / directory).mkdir(parents=True, exist_ok=True)
        for source, destination in ((git, "usr/bin/git"), (inputs["init"], "init"),
                                    (inputs["loader"], "bin/scarlet-ld"), (inputs["shell"], "bin/sh")):
            shutil.copyfile(source, staging / destination)
            (staging / destination).chmod(0o755)
            native_elf(staging / destination, args.arch)
        for name in ("git", "git-upload-pack", "git-receive-pack", "git-upload-archive"):
            (staging / "usr/libexec/git-core" / name).symlink_to("../../bin/git")
            if name != "git":
                (staging / "usr/bin" / name).symlink_to("git")
        (staging / "fixtures/space name.txt").write_bytes(b"fixture from staged rootfs\n")
        (staging / "etc/git-smoke-timeout").write_text(f"{args.command_timeout}\n")
        probe_command = None
        if args.dup2_probe:
            shutil.copyfile(inputs["dup2_probe"], staging / "bin/git-dup2-probe")
        else:
            probe_command = [executable(args.c_compiler), "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror",
                             str(inputs["probe_source"]), "-o", str(staging / "bin/git-dup2-probe")]
            run_logged(probe_command, output, "probe-build.log",
                       env={**os.environ, "SCARLET_RUST_TOOLCHAIN": str(toolchain)})
        (staging / "bin/git-dup2-probe").chmod(0o755)
        native_elf(staging / "bin/git-dup2-probe", args.arch)
        build_command = [rustc, "--edition=2024", "--target", TARGETS[args.arch][1],
                         "-O", "-C", "panic=abort", str(inputs["harness_source"]),
                         "-o", str(staging / "bin/git-smoke")]
        run_logged(build_command, output, "harness-build.log")
        native_elf(staging / "bin/git-smoke", args.arch)
        hook_command = [rustc, "--edition=2024", "--target", TARGETS[args.arch][1],
                        "-O", "-C", "panic=abort", str(inputs["hook_source"]),
                        "-o", str(staging / "fixtures/hooks/pre-commit")]
        run_logged(hook_command, output, "hook-build.log")
        (staging / "fixtures/hooks/pre-commit").chmod(0o755)
        native_elf(staging / "fixtures/hooks/pre-commit", args.arch)
        if fixture:
            fixture.prepare(args, staging, output)
        archive = output / "initramfs.cpio"
        create_initramfs(staging, archive)
        console = "ttyAMA0" if args.arch == "aarch64" else "ttyS0"
        cmdline = f"console={console} init=/init init.console=/dev/tty0 init.exec=/bin/git-smoke"
        if fixture:
            cmdline += " " + fixture.cmdline_extra
        command = [qemu, "-machine", "virt,gic-version=3,acpi=off" if args.arch == "aarch64" else "virt,acpi=off",
                   "-accel", args.accel, "-m", args.memory or ("2G" if args.arch == "aarch64" else "4G"), "-smp", str(args.smp),
                   "-display", "none", "-monitor", "none", "-serial", "stdio", "-no-reboot",
                   "-global", "virtio-mmio.force-legacy=false", "-net", "none"]
        if args.arch == "aarch64":
            command += ["-cpu", "host" if args.accel == "hvf" else "max"]
        else:
            command += ["-bios", "default"]
        image_command = None
        if args.kernel_format == "image":
            command += ["-kernel", str(kernel_copy), "-initrd", str(archive), "-append", cmdline]
        else:
            if args.arch == "aarch64":
                suffixes = ["ARM64_HVF", "ARM64"] if args.accel == "hvf" else ["ARM64_EL2", "ARM64"]
            else:
                suffixes = ["RV64"]
            code = firmware(args.firmware_code, [f"SCARLET_EFI_CODE_{s}" for s in suffixes])
            variables = firmware(args.firmware_vars, [f"SCARLET_EFI_VARS_{s}" for s in suffixes])
            inputs.update(firmware_code=code, firmware_vars=variables)
            private_vars = output / "efi-vars.fd"
            shutil.copyfile(variables, private_vars)
            private_vars.chmod(0o600)
            cache = output / "limine-cache"
            if args.limine_cache:
                source = args.limine_cache.resolve() / "limine-12.4.0"
                efi = "BOOTAA64.EFI" if args.arch == "aarch64" else "BOOTRISCV64.EFI"
                (cache / "limine-12.4.0").mkdir(parents=True)
                shutil.copyfile(source / efi, cache / "limine-12.4.0" / efi)
            boot = output / "boot.img"
            image_command = [executable(args.limine_plugin), "--arch", args.arch, "--kernel", str(kernel_copy),
                             "--initramfs", str(archive), "--output", str(boot), "--cache-dir", str(cache),
                             "--cmdline", cmdline]
            try:
                run_logged(image_command, output, "image-build.log")
            finally:
                if cache.exists():
                    shutil.rmtree(cache)
            command += ["-drive", f"if=pflash,format=raw,unit=0,file={qemu_path(code)},readonly=on",
                        "-drive", f"if=pflash,format=raw,unit=1,file={qemu_path(private_vars)}",
                        "-drive", f"id=boot,if=none,format=raw,file={qemu_path(boot)}",
                        "-device", "virtio-blk-device,drive=boot,bus=virtio-mmio-bus.0" if args.arch == "aarch64"
                                   else "virtio-blk-pci,drive=boot,bus=pcie.0",
                        "-device", "virtio-gpu-device,bus=virtio-mmio-bus.3" if args.arch == "aarch64"
                                   else "virtio-gpu-device,bus=virtio-mmio-bus.1"]
        command += ["-object", "rng-random,id=entropy,filename=/dev/urandom",
                    "-device", f"virtio-rng-device,rng=entropy,bus=virtio-mmio-bus.{6 if args.arch == 'aarch64' else 5}"]
        if fixture:
            command += fixture.qemu_options(args, output)
        deployed = {"git": staging / "usr/bin/git", "init": staging / "init",
                    "loader": staging / "bin/scarlet-ld", "shell": staging / "bin/sh", "kernel": kernel_copy,
                    "dup2_probe": staging / "bin/git-dup2-probe", "hook": staging / "fixtures/hooks/pre-commit"}
        manifest = {"build": build_command, "probe_build": probe_command, "hook_build": hook_command,
                    "image": image_command, "qemu": command,
                    "arch": args.arch, "accel": args.accel, "kernel_format": args.kernel_format,
                    "inputs": {name: {"path": str(path), "sha256": sha256(path)} for name, path in inputs.items()},
                    "deployed_sha256": {name: sha256(path) for name, path in deployed.items()},
                    "harness_sha256": sha256(staging / "bin/git-smoke"), "initramfs_sha256": sha256(archive)}
        (output / "commands.json").write_text(json.dumps(manifest, indent=2) + "\n")
        result = {"result": "PREPARED"} if args.prepare_only else run_guest(
            command, output, args.timeout, fixture.cases if fixture else CASES)
        if fixture and not args.prepare_only:
            result = fixture.finish(result, args, output)
        (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps(result), flush=True)
        return 0 if result["result"] in ("PASS", "PREPARED") else 1
    except (Exception, KeyboardInterrupt) as error:
        (output / "result.json").write_text(json.dumps({"result": "ERROR", "error": str(error)}) + "\n")
        raise
    finally:
        # All names belong to this freshly created run directory. Keep compact
        # evidence, never a copied dependency cache or another project's disk.
        if not args.keep_image and not args.prepare_only:
            for name in ("staging", "limine-cache", ".limine-work"):
                if (output / name).is_dir():
                    shutil.rmtree(output / name)
            for name in ("kernel.elf", "Image", "efi-vars.fd", "initramfs.cpio", "boot.img"):
                (output / name).unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"Scarlet Git smoke: {error}", file=sys.stderr)
        sys.exit(1)
