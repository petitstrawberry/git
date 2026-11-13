use std::fs::{self, File};
use std::io::Read;
use std::process::{Child, Command, Stdio};
use std::thread::sleep;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

fn wait_child(child: &mut Child, name: &str, expected: i32) -> Result<(), String> {
    let start = Instant::now();
    while start.elapsed() < Duration::from_secs(45) {
        match child.try_wait().map_err(|e| format!("{name}: wait: {e}"))? {
            Some(status) if status.code() == Some(expected) => return Ok(()),
            Some(status) => return Err(format!("{name}: expected {expected}, got {status}")),
            None => sleep(Duration::from_millis(50)),
        }
    }
    Err(format!("{name}: timeout"))
}

fn git(name: &str, cwd: &str, args: &[&str]) -> Result<String, String> {
    let stdout = format!("/tmp/http-git-{name}.stdout");
    let stderr = format!("/tmp/http-git-{name}.stderr");
    println!("SCARLET_HTTP_GIT_COMMAND {name} cwd={cwd:?} argv={args:?}");
    let mut child = Command::new("/usr/bin/git")
        .current_dir(cwd)
        .args(args)
        .env_clear()
        .env("PATH", "/usr/bin:/bin:/usr/libexec/git-core")
        .env("HOME", "/root")
        .env("TMPDIR", "/tmp")
        .env("LC_ALL", "C")
        .env("TZ", "UTC")
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_CONFIG_GLOBAL", "/dev/null")
        .env("GIT_TEMPLATE_DIR", "/fixtures/empty-template")
        .env("GIT_EXEC_PATH", "/usr/libexec/git-core")
        .env("GIT_TERMINAL_PROMPT", "0")
        .env("GIT_PAGER", "cat")
        .stdin(Stdio::null())
        .stdout(Stdio::from(File::create(&stdout).map_err(|e| e.to_string())?))
        .stderr(Stdio::from(File::create(&stderr).map_err(|e| e.to_string())?))
        .spawn().map_err(|e| format!("{name}: spawn: {e}"))?;
    let status = wait_child(&mut child, name, 0);
    let read = |path: &str| -> Result<String, String> {
        let mut bytes = Vec::new();
        File::open(path).map_err(|e| e.to_string())?
            .take(1024 * 1024 + 1).read_to_end(&mut bytes).map_err(|e| e.to_string())?;
        if bytes.len() > 1024 * 1024 {
            return Err(format!("{name}: output exceeded 1 MiB"));
        }
        String::from_utf8(bytes).map_err(|e| e.to_string())
    };
    let out = read(&stdout)?;
    let err = read(&stderr)?;
    // Escape child output so it cannot become a supervisor case/result marker.
    println!("SCARLET_HTTP_GIT_EXIT {name} status={status:?} stdout={out:?} stderr={err:?}");
    status?;
    Ok(out)
}

fn git_https() -> Result<(), String> {
    let url = fs::read_to_string("/fixtures/git-url").map_err(|e| e.to_string())?;
    let head = fs::read_to_string("/fixtures/git-head").map_err(|e| e.to_string())?;
    let expected = head.trim();
    let listing = git("ls_remote", "/tmp", &["ls-remote", url.trim(), "HEAD"])?;
    let observed: Vec<_> = listing.split_whitespace().collect();
    if observed != [expected, "HEAD"] {
        return Err(format!("native ls-remote does not match host HEAD {expected}: {listing:?}"));
    }
    println!("SCARLET_HTTP_CASE git_ls_remote PASS head={expected}");
    git("clone", "/tmp", &["clone", "--depth=1", "--single-branch", url.trim(), "/tmp/http-hello-world"])?;
    let observed = git("head", "/tmp/http-hello-world", &["rev-parse", "HEAD"])?;
    if observed.trim() != expected {
        return Err(format!("cloned HEAD differs from host HEAD: {observed:?}"));
    }
    if git("shallow", "/tmp/http-hello-world", &["rev-parse", "--is-shallow-repository"])?.trim() != "true" {
        return Err("clone is not shallow".into());
    }
    let readme = fs::read("/tmp/http-hello-world/README").map_err(|e| format!("cloned README: {e}"))?;
    let expected_readme = fs::read("/fixtures/git-README").map_err(|e| e.to_string())?;
    if readme != expected_readme || readme.is_empty() {
        return Err("cloned README differs from host's commit-pinned README".into());
    }
    println!("SCARLET_HTTP_DETAIL git_clone head={expected} shallow=true readme_bytes={}", readme.len());
    println!("SCARLET_HTTP_CASE git_shallow_clone PASS head={expected}");
    git("fsck", "/tmp/http-hello-world", &["fsck", "--full", "--strict"])?;
    println!("SCARLET_HTTP_CASE git_fsck PASS");
    Ok(())
}

fn transfer(url: &str, ca: Option<&str>, expected: i32, name: &str) -> Result<(), String> {
    let mut command = Command::new("/bin/curl-link-check");
    command.arg(url);
    if let Some(ca) = ca {
        command.arg(ca);
    }
    let mut child = command.spawn().map_err(|e| format!("{name}: spawn: {e}"))?;
    let status = wait_child(&mut child, name, expected);
    let report = fs::read_to_string("/tmp/curl-link-check.result")
        .map_err(|e| format!("{name}: missing C transfer evidence: {e}"))?;
    for line in report.lines() {
        println!("SCARLET_HTTP_DETAIL {name} {line}");
    }
    status?;
    if !report.contains(&format!("result={expected} ")) || !report.contains("entropy=PASS") {
        return Err(format!("{name}: incomplete C transfer evidence"));
    }
    println!("SCARLET_HTTP_CASE {name} PASS exit={expected}");
    Ok(())
}

fn run() -> Result<(), String> {
    println!("SCARLET_HTTP_BEGIN");
    if fs::metadata("/bin/pipe-metadata-test").is_ok() {
        let mut child = Command::new("/bin/pipe-metadata-test").spawn()
            .map_err(|e| format!("pipe metadata probe: {e}"))?;
        wait_child(&mut child, "pipe_metadata", 0)?;
        println!("SCARLET_HTTP_CASE pipe_metadata PASS exit=0");
    }
    let epoch = SystemTime::now().duration_since(UNIX_EPOCH).map_err(|e| e.to_string())?.as_secs();
    let host_epoch = fs::read_to_string("/fixtures/host-epoch").map_err(|e| e.to_string())?
        .trim().parse::<u64>().map_err(|e| e.to_string())?;
    println!("SCARLET_HTTP_CLOCK guest={epoch} host={host_epoch}");
    if epoch.abs_diff(host_epoch) > 3600 {
        return Err("guest clock differs from host by more than one hour".into());
    }
    let mut netcfgd = Command::new("/bin/netcfgd").spawn().map_err(|e| format!("netcfgd: {e}"))?;
    let deadline = Instant::now() + Duration::from_secs(30);
    loop {
        if fs::read_to_string("/etc/resolv.conf").unwrap_or_default().contains("nameserver ") {
            break;
        }
        if netcfgd.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Err("netcfgd exited before configuring the interface".into());
        }
        if Instant::now() >= deadline {
            return Err("network configuration timeout".into());
        }
        sleep(Duration::from_millis(100));
    }
    println!("SCARLET_HTTP_DNS {}", fs::read_to_string("/etc/resolv.conf").unwrap_or_default().trim());
    let mut resolverd = Command::new("/bin/resolverd").spawn().map_err(|e| format!("resolverd: {e}"))?;
    let deadline = Instant::now() + Duration::from_secs(10);
    while fs::metadata("/tmp/resolverd.sock").is_err() {
        if resolverd.try_wait().map_err(|e| e.to_string())?.is_some() {
            return Err("resolverd exited before binding its socket".into());
        }
        if Instant::now() >= deadline {
            return Err("resolverd startup timeout".into());
        }
        sleep(Duration::from_millis(100));
    }
    let url = fs::read_to_string("/fixtures/url").map_err(|e| e.to_string())?;
    transfer(url.trim(), None, 0, "trusted_https")?;
    transfer(url.trim(), Some("/fixtures/wrong-ca.pem"), 60, "wrong_ca_rejected")?;
    if fs::metadata("/fixtures/git-url").is_ok() {
        git_https()?;
    }
    Ok(())
}

fn main() {
    match run() {
        Ok(()) => println!("SCARLET_HTTP_RESULT PASS"),
        Err(error) => println!("SCARLET_HTTP_RESULT FAIL {error}"),
    }
    // Keep the serial evidence stable until the host terminates this guest.
    loop {
        sleep(Duration::from_secs(1));
    }
}
