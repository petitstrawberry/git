//! Native Scarlet acceptance fixture. The distribution init establishes the
//! Environment and stdio, then execs this supervisor as /bin/git-smoke.
use std::fs::{self, File};
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::thread;
use std::time::{Duration, Instant};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
const GIT: &str = "/usr/bin/git";
pub(crate) const ROOT: &str = "/tmp/git-scarlet-smoke";
pub(crate) const CASES: &[&str] = &[
    "dup2",
    "version",
    "init",
    "status",
    "add",
    "commit",
    "diff",
    "branch_checkout",
    "localclone",
    "fetch",
    "push",
    "merge",
    "hooks",
    "fsck",
    "cancellation",
];

fn require(ok: bool, message: impl Into<String>) -> Result<()> {
    if ok {
        Ok(())
    } else {
        Err(io::Error::other(message.into()).into())
    }
}

/// The shipped native Rust std still returns Unsupported from Child::kill.
/// Use Scarlet's native Kill(pid, signal), not a Linux syscall or a host kill.
fn cancel(child: &mut Child) -> Result<()> {
    #[cfg(all(target_os = "scarlet", target_arch = "aarch64"))]
    let result = unsafe {
        let mut result = child.id() as usize;
        core::arch::asm!("svc #0", in("x8") 6usize, inout("x0") result,
                         in("x1") 9usize, options(nostack));
        result
    };
    #[cfg(all(target_os = "scarlet", target_arch = "riscv64"))]
    let result = unsafe {
        let mut result = child.id() as usize;
        core::arch::asm!("ecall", in("a7") 6usize, inout("a0") result,
                         in("a1") 9usize, options(nostack));
        result
    };
    #[cfg(not(target_os = "scarlet"))]
    let result = {
        child.kill()?;
        0usize
    };
    require(
        result == 0,
        format!("Native Kill(6) failed for pid {}: {result:#x}", child.id()),
    )
}

fn wait(child: &mut Child, timeout: Duration) -> Result<ExitStatus> {
    let start = Instant::now();
    loop {
        if let Some(status) = child.try_wait()? {
            return Ok(status);
        }
        if start.elapsed() >= timeout {
            let cancellation = cancel(child);
            // Host QEMU watchdog remains authoritative if native kill/wait fails.
            return Err(io::Error::other(format!(
                "child {} timed out; cancellation={cancellation:?}",
                child.id()
            ))
            .into());
        }
        thread::sleep(Duration::from_millis(10));
    }
}

fn read_bounded(path: &Path) -> Result<Vec<u8>> {
    let mut data = Vec::new();
    File::open(path)?
        .take(1024 * 1024 + 1)
        .read_to_end(&mut data)?;
    require(data.len() <= 1024 * 1024, "command output exceeded 1 MiB")?;
    Ok(data)
}

pub(crate) struct Fixture {
    pub(crate) sequence: usize,
    pub(crate) timeout: Duration,
}

impl Fixture {
    fn command(&self, directory: &Path) -> Command {
        self.program(directory, GIT)
    }

    fn program(&self, directory: &Path, program: &str) -> Command {
        let mut cmd = Command::new(program);
        cmd.current_dir(directory)
            .env_clear()
            .env("PATH", "/usr/bin:/bin:/usr/libexec/git-core")
            .env("HOME", format!("{ROOT}/home"))
            .env("TMPDIR", "/tmp")
            .env("LC_ALL", "C")
            .env("TZ", "UTC")
            .env("GIT_CONFIG_NOSYSTEM", "1")
            .env("GIT_CONFIG_GLOBAL", "/dev/null")
            .env("GIT_TEMPLATE_DIR", "/fixtures/empty-template")
            .env("GIT_EXEC_PATH", "/usr/libexec/git-core")
            .env("GIT_TERMINAL_PROMPT", "0")
            .env("GIT_PAGER", "cat")
            .env("SCARLET_GIT_HOOK_TOKEN", "native-hook-token")
            .env("GIT_AUTHOR_NAME", "Scarlet Git Smoke")
            .env("GIT_AUTHOR_EMAIL", "smoke@example.invalid")
            .env("GIT_COMMITTER_NAME", "Scarlet Git Smoke")
            .env("GIT_COMMITTER_EMAIL", "smoke@example.invalid");
        cmd
    }

    fn files(&mut self) -> Result<(PathBuf, PathBuf)> {
        self.sequence += 1;
        Ok((
            PathBuf::from(format!("{ROOT}/logs/{}.stdout", self.sequence)),
            PathBuf::from(format!("{ROOT}/logs/{}.stderr", self.sequence)),
        ))
    }

    pub(crate) fn git(&mut self, cwd: &Path, args: &[&str], expected: i32) -> Result<Vec<u8>> {
        self.execute(cwd, GIT, args, expected)
    }

    fn execute(
        &mut self,
        cwd: &Path,
        program: &str,
        args: &[&str],
        expected: i32,
    ) -> Result<Vec<u8>> {
        let (stdout, stderr) = self.files()?;
        println!(
            "SCARLET_GIT_COMMAND {} cwd={} program={program:?} argv={args:?}",
            self.sequence,
            cwd.display()
        );
        let mut child = self
            .program(cwd, program)
            .args(args)
            .stdin(Stdio::null())
            .stdout(Stdio::from(File::create(&stdout)?))
            .stderr(Stdio::from(File::create(&stderr)?))
            .spawn()?;
        let status = wait(&mut child, self.timeout)?;
        let out = read_bounded(&stdout)?;
        let err = read_bounded(&stderr)?;
        // Serialize captured output as escaped text: a child cannot forge a
        // supervisor marker by printing one on a separate line.
        println!(
            "SCARLET_GIT_EXIT {} code={:?} stdout={:?} stderr={:?}",
            self.sequence,
            status.code(),
            String::from_utf8_lossy(&out),
            String::from_utf8_lossy(&err)
        );
        require(
            status.code() == Some(expected),
            format!("{program} {args:?}: expected exit {expected}, got {status}"),
        )?;
        Ok(out)
    }

    fn text(&mut self, cwd: &Path, args: &[&str]) -> Result<String> {
        Ok(String::from_utf8(self.git(cwd, args, 0)?)?
            .trim()
            .to_string())
    }

    fn merge(&mut self, cwd: &Path) -> Result<()> {
        self.git(cwd, &["checkout", "--quiet", "-b", "merge-topic"], 0)?;
        fs::write(cwd.join("topic merge.txt"), b"from topic\n")?;
        self.git(cwd, &["add", "--", "topic merge.txt"], 0)?;
        self.git(cwd, &["commit", "--quiet", "-m", "native merge topic"], 0)?;
        let topic_parent = self.text(cwd, &["rev-parse", "HEAD"])?;

        self.git(cwd, &["checkout", "--quiet", "main"], 0)?;
        fs::write(cwd.join("main merge.txt"), b"from main\n")?;
        self.git(cwd, &["add", "--", "main merge.txt"], 0)?;
        self.git(cwd, &["commit", "--quiet", "-m", "native merge main"], 0)?;
        let main_parent = self.text(cwd, &["rev-parse", "HEAD"])?;
        self.git(
            cwd,
            &[
                "merge",
                "--quiet",
                "--no-ff",
                "--no-edit",
                "-m",
                "native divergent merge",
                "merge-topic",
            ],
            0,
        )?;
        require(
            self.text(cwd, &["show", "--no-patch", "--format=%P", "HEAD"])?
                == format!("{main_parent} {topic_parent}"),
            "merge did not create the expected two-parent commit",
        )?;
        for (name, contents) in [
            ("main merge.txt", "from main"),
            ("topic merge.txt", "from topic"),
        ] {
            require(
                fs::read(cwd.join(name))? == format!("{contents}\n").as_bytes()
                    && self.text(cwd, &["show", &format!("HEAD:{name}")])? == contents,
                format!("merged content differs for {name}"),
            )?;
        }
        require(
            self.git(cwd, &["status", "--porcelain=v1"], 0)?.is_empty(),
            "merge left changes",
        )
    }

    fn hooks(&mut self, cwd: &Path) -> Result<()> {
        // The native Rust copy/chmod APIs cannot preserve executable modes.
        // Use the host-staged 0755 native ELF through Git's normal hook lookup.
        self.git(cwd, &["config", "core.hooksPath", "/fixtures/hooks"], 0)?;
        let mode = cwd.join(".git/native-hook-mode");
        let events = cwd.join(".git/native-hook-events");
        require(!events.exists(), "hook events already exist")?;
        fs::write(&mode, b"allow\n")?;
        fs::write(cwd.join("hooked.txt"), b"accepted by native hook\n")?;
        self.git(cwd, &["add", "--", "hooked.txt"], 0)?;
        let before = self.text(cwd, &["rev-parse", "HEAD"])?;
        self.git(cwd, &["commit", "--quiet", "-m", "native hook accepts"], 0)?;
        let accepted = self.text(cwd, &["rev-parse", "HEAD"])?;
        let allowed_event = format!("allow cwd={}\n", cwd.display());
        let rejected_event = format!("reject cwd={}\n", cwd.display());
        require(
            fs::read_to_string(&events)? == allowed_event,
            "native hook did not run exactly once",
        )?;
        require(
            accepted != before
                && self.text(cwd, &["rev-parse", "HEAD^"])? == before
                && self.text(cwd, &["show", "HEAD:hooked.txt"])? == "accepted by native hook",
            "accepted hook commit differs",
        )?;

        fs::write(&mode, b"reject\n")?;
        fs::write(
            cwd.join("hooked.txt"),
            b"retry after native hook rejection\n",
        )?;
        self.git(cwd, &["add", "--", "hooked.txt"], 0)?;
        let staged = self.text(cwd, &["write-tree"])?;
        // Git maps a pre-commit hook's nonzero result (42) to commit exit 1.
        self.git(cwd, &["commit", "--quiet", "-m", "native hook rejects"], 1)?;
        require(
            fs::read_to_string(&events)? == format!("{allowed_event}{rejected_event}"),
            "rejecting native hook was not invoked exactly once",
        )?;
        require(
            self.text(cwd, &["rev-parse", "HEAD"])? == accepted
                && self.text(cwd, &["write-tree"])? == staged
                && self.git(cwd, &["diff", "--cached", "--name-only", "-z"], 0)? == b"hooked.txt\0"
                && self.text(cwd, &["show", "HEAD:hooked.txt"])? == "accepted by native hook",
            "hook rejection changed HEAD or staged changes",
        )?;
        fs::write(&mode, b"allow\n")?;
        self.git(cwd, &["commit", "--quiet", "-m", "native hook retry"], 0)?;
        require(
            fs::read_to_string(&events)?
                == format!("{allowed_event}{rejected_event}{allowed_event}"),
            "retry native hook invocation differs",
        )?;
        require(
            self.text(cwd, &["rev-parse", "HEAD^"])? == accepted
                && self.text(cwd, &["show", "HEAD:hooked.txt"])?
                    == "retry after native hook rejection"
                && self.git(cwd, &["status", "--porcelain=v1"], 0)?.is_empty(),
            "commit retry did not preserve staged contents or left changes",
        )
    }

    fn cancellation(&mut self, cwd: &Path) -> Result<()> {
        let (stdout, stderr) = self.files()?;
        // Keep the pipe writer open and send no bytes. hash-object must remain
        // blocked in read until the native process-control request arrives.
        let mut child = self
            .command(cwd)
            .args(["hash-object", "--stdin"])
            .stdin(Stdio::piped())
            .stdout(Stdio::from(File::create(&stdout)?))
            .stderr(Stdio::from(File::create(&stderr)?))
            .spawn()?;
        thread::sleep(Duration::from_millis(150));
        require(
            child.try_wait()?.is_none(),
            "hash-object exited before cancellation",
        )?;
        cancel(&mut child)?;
        let status = wait(&mut child, self.timeout)?;
        drop(child.stdin.take());
        println!(
            "SCARLET_GIT_CANCEL pid={} native_syscall=6 signal=9 code={:?}",
            child.id(),
            status.code()
        );
        require(
            status.code() == Some(137),
            "native SIGKILL did not produce exit 137",
        )?;
        require(
            read_bounded(&stdout)?.is_empty(),
            "cancelled hash-object produced an object ID",
        )?;
        self.git(cwd, &["status", "--porcelain=v1"], 0)?;
        Ok(())
    }
}

fn pass(name: &str) {
    println!("SCARLET_GIT_CASE {name} PASS");
}

pub(crate) fn run() -> Result<()> {
    // A fresh boot owns this filesystem. Refuse to erase data on rerun.
    fs::create_dir(ROOT)?;
    fs::create_dir(format!("{ROOT}/logs"))?;
    fs::create_dir(format!("{ROOT}/home"))?;
    let timeout = fs::read_to_string("/etc/git-smoke-timeout")?
        .trim()
        .parse::<u64>()?;
    let mut f = Fixture {
        sequence: 0,
        timeout: Duration::from_secs(timeout),
    };
    let root = Path::new(ROOT);
    let repo = root.join("source repo's worktree");
    let clone = root.join("clone repo's worktree");
    let remote = root.join("remote repo's bare.git");
    let repo_s = repo.to_str().unwrap();
    let clone_s = clone.to_str().unwrap();
    let remote_s = remote.to_str().unwrap();

    require(
        f.execute(root, "/bin/git-dup2-probe", &[], 0)? == b"SCARLET_DUP2_PROBE PASS\n",
        "native dup2 probe output",
    )?;
    require(
        fs::read(root.join("dup2-redirected"))? == b"redirected via native syscall 111\n",
        "native dup2 stdout redirection content",
    )?;
    pass("dup2");

    require(
        f.text(root, &["--version"])?.starts_with("git version "),
        "version output",
    )?;
    pass("version");
    f.git(
        root,
        &["init", "--quiet", "--initial-branch=main", repo_s],
        0,
    )?;
    require(repo.join(".git/HEAD").is_file(), "init did not create HEAD")?;
    require(
        f.text(&repo, &["symbolic-ref", "HEAD"])? == "refs/heads/main",
        "initial branch",
    )?;
    pass("init");

    require(
        f.git(&repo, &["status", "--porcelain=v1", "-z"], 0)?
            .is_empty(),
        "initial worktree is dirty",
    )?;
    fs::write(repo.join("tracked.txt"), b"one\n")?;
    fs::create_dir(repo.join("nested"))?;
    fs::copy(
        "/fixtures/space name.txt",
        repo.join("nested/space name.txt"),
    )?;
    let status = f.git(
        &repo,
        &["status", "--porcelain=v1", "--untracked-files=all", "-z"],
        0,
    )?;
    require(
        status.split(|b| *b == 0).any(|s| s == b"?? tracked.txt"),
        "untracked file missing",
    )?;
    require(
        status
            .split(|b| *b == 0)
            .any(|s| s == b"?? nested/space name.txt"),
        "path with spaces missing",
    )?;
    pass("status");

    f.git(
        &repo,
        &["add", "--", "tracked.txt", "nested/space name.txt"],
        0,
    )?;
    let staged = f.git(&repo, &["diff", "--cached", "--name-only", "-z"], 0)?;
    require(
        staged == b"nested/space name.txt\0tracked.txt\0",
        "staged names differ",
    )?;
    pass("add");
    f.git(&repo, &["commit", "--quiet", "-m", "native first"], 0)?;
    let first = f.text(&repo, &["rev-parse", "HEAD"])?;
    require(
        (first.len() == 40 || first.len() == 64) && first.bytes().all(|b| b.is_ascii_hexdigit()),
        "invalid commit ID",
    )?;
    require(
        f.git(&repo, &["status", "--porcelain=v1"], 0)?.is_empty(),
        "commit left changes",
    )?;
    pass("commit");

    fs::write(repo.join("tracked.txt"), b"two\n")?;
    let patch =
        String::from_utf8(f.git(&repo, &["diff", "--exit-code", "--", "tracked.txt"], 1)?)?;
    require(
        patch.contains("\n-one\n") && patch.contains("\n+two\n"),
        "diff content differs",
    )?;
    require(
        f.text(&repo, &["rev-parse", "HEAD"])? == first,
        "diff changed HEAD",
    )?;
    f.git(&repo, &["add", "--", "tracked.txt"], 0)?;
    f.git(&repo, &["commit", "--quiet", "-m", "native second"], 0)?;
    let second = f.text(&repo, &["rev-parse", "HEAD"])?;
    pass("diff");

    f.git(&repo, &["branch", "topic", &first], 0)?;
    f.git(&repo, &["checkout", "--quiet", "topic"], 0)?;
    require(
        fs::read(repo.join("tracked.txt"))? == b"one\n",
        "checkout topic content",
    )?;
    f.git(&repo, &["checkout", "--quiet", "main"], 0)?;
    require(
        fs::read(repo.join("tracked.txt"))? == b"two\n",
        "checkout main content",
    )?;
    require(
        f.text(&repo, &["rev-parse", "HEAD"])? == second,
        "checkout main HEAD",
    )?;
    pass("branch_checkout");

    f.git(
        root,
        &[
            "clone",
            "--quiet",
            "--local",
            "--no-hardlinks",
            repo_s,
            clone_s,
        ],
        0,
    )?;
    require(
        f.text(&clone, &["rev-parse", "HEAD"])? == second,
        "clone HEAD",
    )?;
    require(
        f.text(&clone, &["remote", "get-url", "origin"])? == repo_s,
        "clone lost spaces or apostrophe in origin path",
    )?;
    require(
        fs::read(clone.join("nested/space name.txt"))? == fs::read("/fixtures/space name.txt")?,
        "clone fixture content",
    )?;
    pass("localclone");

    fs::write(repo.join("tracked.txt"), b"three\n")?;
    f.git(&repo, &["add", "--", "tracked.txt"], 0)?;
    f.git(&repo, &["commit", "--quiet", "-m", "native third"], 0)?;
    let third = f.text(&repo, &["rev-parse", "HEAD"])?;
    f.git(
        root,
        &[
            "clone",
            "--quiet",
            "--bare",
            "--local",
            "--no-hardlinks",
            repo_s,
            remote_s,
        ],
        0,
    )?;
    f.git(&clone, &["remote", "set-url", "origin", remote_s], 0)?;
    require(
        f.text(&clone, &["remote", "get-url", "origin"])? == remote_s,
        "remote path with spaces and apostrophe differs",
    )?;
    f.git(
        &clone,
        &[
            "-c",
            "protocol.file.allow=always",
            "fetch",
            "--quiet",
            "origin",
        ],
        0,
    )?;
    require(
        f.text(&clone, &["rev-parse", "refs/remotes/origin/main"])? == third,
        "fetch remote ref",
    )?;
    require(
        f.text(&clone, &["rev-parse", "HEAD"])? == second,
        "fetch changed local HEAD",
    )?;
    pass("fetch");

    f.git(&clone, &["merge", "--quiet", "--ff-only", "origin/main"], 0)?;
    fs::write(clone.join("pushed.txt"), b"native push\n")?;
    f.git(&clone, &["add", "--", "pushed.txt"], 0)?;
    f.git(&clone, &["commit", "--quiet", "-m", "native fourth"], 0)?;
    let fourth = f.text(&clone, &["rev-parse", "HEAD"])?;
    f.git(
        &clone,
        &[
            "-c",
            "protocol.file.allow=always",
            "push",
            "--quiet",
            "origin",
            "main:main",
        ],
        0,
    )?;
    require(
        f.text(&remote, &["rev-parse", "refs/heads/main"])? == fourth,
        "push remote ref",
    )?;
    require(
        f.text(&remote, &["show", "main:pushed.txt"])? == "native push",
        "push object content",
    )?;
    pass("push");

    f.merge(&clone)?;
    pass("merge");
    f.hooks(&clone)?;
    pass("hooks");

    for directory in [&repo, &clone, &remote] {
        f.git(directory, &["fsck", "--full", "--strict"], 0)?;
    }
    require(
        f.git(&clone, &["status", "--porcelain=v1"], 0)?.is_empty(),
        "final worktree is dirty",
    )?;
    pass("fsck");
    f.cancellation(&clone)?;
    pass("cancellation");
    Ok(())
}

#[allow(dead_code)] // Also compiled as a module by the ext2 supervisor.
fn main() {
    println!("SCARLET_GIT_SMOKE START");
    match run() {
        Ok(()) => println!("SCARLET_GIT_SMOKE PASS cases={}", CASES.len()),
        Err(error) => println!("SCARLET_GIT_SMOKE FAIL {error:?}"),
    }
    // PID 1 must stay alive. The host records the outcome then stops only its VM.
    loop {
        thread::sleep(Duration::from_secs(1));
    }
}
