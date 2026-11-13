//! PID 1 supervisor for a private ext2 disk. Reuses every core Git case, then
//! unmounts/remounts and compares files and refs before checking Git objects.
//! Init must retain the writable VfsView handles (the normal distribution init
//! does this). Native FsMount=500 needs the device in its options string;
//! FsUmount=501 has no force/lazy semantics here. No shell or Linux ABI is used.
#[path = "guest.rs"]
mod core_fixture;

use std::collections::BTreeMap;
use std::ffi::CString;
use std::fs::{self, File};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::{Duration, Instant};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const EXTRA_CASES: usize = 6;
const MAX_BYTES: usize = 64 * 1024 * 1024;

fn require(ok: bool, message: impl Into<String>) -> Result<()> {
    if ok { Ok(()) } else { Err(io::Error::other(message.into()).into()) }
}

fn syscall(number: usize, args: [usize; 5]) -> usize {
    let mut result = args[0];
    #[cfg(target_arch = "aarch64")]
    unsafe {
        core::arch::asm!("svc #0", in("x8") number, inout("x0") result,
            in("x1") args[1], in("x2") args[2], in("x3") args[3], in("x4") args[4],
            options(nostack));
    }
    #[cfg(target_arch = "riscv64")]
    unsafe {
        core::arch::asm!("ecall", in("a7") number, inout("a0") result,
            in("a1") args[1], in("a2") args[2], in("a3") args[3], in("a4") args[4],
            options(nostack));
    }
    result
}

fn mount(device: &str) -> Result<()> {
    let source = CString::new(device)?;
    let options = CString::new(format!("device={device},rw"))?;
    let result = syscall(500, [source.as_ptr() as usize, c"/tmp".as_ptr() as usize,
        c"ext2".as_ptr() as usize, 0, options.as_ptr() as usize]);
    require(result == 0, format!("FsMount(500) {device} at /tmp failed: {result:#x}"))
}

fn unmount() -> Result<()> {
    let result = syscall(501, [c"/tmp".as_ptr() as usize, 0, 0, 0, 0]);
    require(result == 0, format!("FsUmount(501) /tmp failed: {result:#x}"))
}

fn device_by_uuid() -> Result<String> {
    let text = fs::read_to_string("/etc/git-ext2-uuid")?;
    let text = text.trim();
    require(text.len() == 32 && text.bytes().all(|b| b.is_ascii_hexdigit()), "bad fixture UUID")?;
    let mut uuid = [0u8; 16];
    for (i, byte) in uuid.iter_mut().enumerate() {
        *byte = u8::from_str_radix(&text[i * 2..i * 2 + 2], 16)?;
    }
    // vblk numbering follows discovery, not QEMU argument order. Match only
    // the host-created image's UUID, never assume the data disk is vblk1.
    let start = Instant::now();
    loop {
        for index in 0..16 {
            let path = format!("/dev/vblk{index}");
            let mut bytes = [0u8; 2048];
            if File::open(&path).and_then(|mut f| f.read_exact(&mut bytes)).is_ok()
                && bytes[1080..1082] == [0x53, 0xef]
                && bytes[1128..1144] == uuid
            {
                return Ok(path);
            }
        }
        require(start.elapsed() < Duration::from_secs(10), "private ext2 UUID not found on /dev/vblk0..15")?;
        thread::sleep(Duration::from_millis(50));
    }
}

#[derive(Debug, PartialEq, Eq)]
enum Entry { Directory, File(Vec<u8>), Symlink(PathBuf) }
type Snapshot = BTreeMap<PathBuf, Entry>;

fn visit(path: &Path, base: &Path, entries: &mut Snapshot, bytes: &mut usize, flush: bool) -> Result<()> {
    require(entries.len() < 4096, "snapshot exceeded 4096 entries")?;
    let metadata = fs::symlink_metadata(path)?;
    let relative = path.strip_prefix(base)?.to_owned();
    if metadata.is_dir() {
        entries.insert(relative, Entry::Directory);
        for child in fs::read_dir(path)? {
            visit(&child?.path(), base, entries, bytes, flush)?;
        }
    } else if metadata.file_type().is_symlink() {
        entries.insert(relative, Entry::Symlink(fs::read_link(path)?));
    } else {
        require(metadata.is_file(), format!("unexpected non-file: {}", path.display()))?;
        let file = File::open(path)?;
        if flush { file.sync_all()?; }
        let mut content = Vec::new();
        file.take((MAX_BYTES - *bytes + 1) as u64).read_to_end(&mut content)?;
        *bytes += content.len();
        require(*bytes <= MAX_BYTES, "snapshot exceeded 64 MiB")?;
        entries.insert(relative, Entry::File(content));
    }
    Ok(())
}

fn snapshot(flush: bool) -> Result<Snapshot> {
    let mut entries = BTreeMap::new();
    let mut bytes = 0;
    let root = Path::new(core_fixture::ROOT);
    visit(root, root, &mut entries, &mut bytes, flush)?;
    println!("SCARLET_EXT2_SNAPSHOT entries={} bytes={bytes} flush={flush}", entries.len());
    Ok(entries)
}

fn repositories() -> Result<Vec<PathBuf>> {
    let mut repos = Vec::new();
    for entry in fs::read_dir(core_fixture::ROOT)? {
        let path = entry?.path();
        if path.join(".git/HEAD").is_file()
            || (path.join("HEAD").is_file() && path.join("objects").is_dir())
        {
            repos.push(path);
        }
    }
    repos.sort();
    require(repos.len() == 3, format!("expected three repositories, found {}", repos.len()))?;
    Ok(repos)
}

fn pass(case: &str) { println!("SCARLET_GIT_CASE {case} PASS"); }

fn run() -> Result<()> {
    let device = device_by_uuid()?;
    println!("SCARLET_EXT2_DEVICE {device}");
    // Remove init's temporary mount first. All Git work, including /tmp used
    // by receive-pack, must then reach the private ext2 filesystem.
    unmount()?;
    mount(&device)?;
    require(Path::new("/tmp/lost+found").is_dir(), "ext2 lost+found missing after mount")?;
    pass("ext2_mount");
    core_fixture::run()?;

    let repos = repositories()?;
    let timeout = fs::read_to_string("/etc/git-smoke-timeout")?.trim().parse::<u64>()?;
    let mut fixture = core_fixture::Fixture { sequence: 1000, timeout: Duration::from_secs(timeout) };
    let mut refs = Vec::new();
    for repo in &repos {
        let value = fixture.git(repo, &["show-ref", "--head"], 0)?;
        require(!value.is_empty(), "empty reference snapshot")?;
        println!("SCARLET_EXT2_REFS repo={:?} refs={:?}", repo, String::from_utf8_lossy(&value));
        refs.push(value);
    }
    let before = snapshot(true)?;
    // No child, directory iterator or ext2 file handle survives this point.
    std::env::set_current_dir("/")?;
    unmount()?;
    require(!Path::new(core_fixture::ROOT).exists(), "fixture still visible after unmount")?;
    mount(&device)?;
    pass("ext2_remount");
    let after = snapshot(false)?;
    require(before.len() == after.len(), format!("entry count changed across remount: {} -> {}", before.len(), after.len()))?;
    for (path, entry) in &before {
        require(after.get(path) == Some(entry), format!("file changed across remount: {}", path.display()))?;
    }
    pass("ext2_files");
    require(repositories()? == repos, "repository set changed across remount")?;
    for (repo, expected) in repos.iter().zip(&refs) {
        require(fixture.git(repo, &["show-ref", "--head"], 0)? == *expected,
            format!("refs changed across remount: {}", repo.display()))?;
    }
    pass("ext2_refs");
    for repo in &repos {
        fixture.git(repo, &["fsck", "--full", "--strict"], 0)?;
    }
    pass("ext2_fsck");
    snapshot(true)?;
    unmount()?;
    require(!Path::new(core_fixture::ROOT).exists(), "final unmount did not hide fixture")?;
    pass("ext2_unmount");
    Ok(())
}

fn main() {
    println!("SCARLET_GIT_SMOKE START ext2");
    match run() {
        Ok(()) => println!("SCARLET_GIT_SMOKE PASS cases={}", core_fixture::CASES.len() + EXTRA_CASES),
        Err(error) => println!("SCARLET_GIT_SMOKE FAIL {error:?}"),
    }
    loop { thread::sleep(Duration::from_secs(1)); }
}
