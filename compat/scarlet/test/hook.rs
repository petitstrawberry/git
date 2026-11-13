//! Native pre-commit executable: records actual execution and can reject a commit.
use std::env;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::path::Path;

fn run() -> io::Result<bool> {
    if env::args_os().count() != 1 {
        return Err(io::Error::other("pre-commit received unexpected arguments"));
    }
    if env::var("SCARLET_GIT_HOOK_TOKEN").as_deref() != Ok("native-hook-token") {
        return Err(io::Error::other("pre-commit environment token missing"));
    }
    let index = env::var_os("GIT_INDEX_FILE")
        .ok_or_else(|| io::Error::other("pre-commit GIT_INDEX_FILE missing"))?;
    if !Path::new(&index).is_file() || !Path::new(".git/HEAD").is_file() {
        return Err(io::Error::other(
            "pre-commit index or worktree directory invalid",
        ));
    }
    let mode = fs::read_to_string(".git/native-hook-mode")?;
    let accept = match mode.as_str() {
        "allow\n" => true,
        "reject\n" => false,
        _ => return Err(io::Error::other("pre-commit mode invalid")),
    };
    let cwd = env::current_dir()?;
    let mut events = OpenOptions::new()
        .create(true)
        .append(true)
        .open(".git/native-hook-events")?;
    writeln!(events, "{} cwd={}", mode.trim(), cwd.display())?;
    eprintln!("SCARLET_NATIVE_HOOK {} cwd={}", mode.trim(), cwd.display());
    Ok(accept)
}

fn main() {
    match run() {
        Ok(true) => {}
        Ok(false) => std::process::exit(42),
        Err(error) => {
            eprintln!("SCARLET_NATIVE_HOOK ERROR {error:?}");
            std::process::exit(99);
        }
    }
}
