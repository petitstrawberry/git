//! Git's process adapter for the Scarlet Native ABI.
//!
//! Build with the *same* scarlet-sdk Rust toolchain as libscarlet_c.a. That
//! shares std's allocator, environment and TLS with the C runtime. This crate
//! does not export a second allocator, pipe implementation or C runtime.

#![deny(unsafe_op_in_unsafe_fn)]
#![cfg(any(test, target_os = "scarlet"))]

#[cfg(target_os = "scarlet")]
mod native;

use std::collections::BTreeMap;
use std::ffi::CString;

const E2BIG: i32 = 7;
const ENOEXEC: i32 = 8;
const EINVAL: i32 = 22;
const MAX_STRINGS: usize = 256;
const MAX_STRING_BYTES: usize = 4095;

fn cstring(text: &str) -> Result<CString, i32> {
    if text.len() > MAX_STRING_BYTES {
        return Err(E2BIG);
    }
    CString::new(text).map_err(|_| EINVAL)
}

fn apply_environment_delta(
    env: &mut BTreeMap<String, String>,
    delta: &[CString],
) -> Result<(), i32> {
    for entry in delta {
        let entry = entry.to_str().map_err(|_| EINVAL)?;
        match entry.split_once('=') {
            Some((name, value)) if !name.is_empty() => {
                env.insert(name.to_owned(), value.to_owned());
            }
            None if !entry.is_empty() => {
                env.remove(entry);
            }
            _ => return Err(EINVAL),
        }
    }
    Ok(())
}

fn encode_environment(env: BTreeMap<String, String>) -> Result<Vec<CString>, i32> {
    if env.len() > MAX_STRINGS {
        return Err(E2BIG);
    }
    env.into_iter()
        .map(|(name, value)| cstring(&format!("{name}={value}")))
        .collect()
}

fn interpreter_args(
    header: &[u8],
    program: &str,
    argv: &[CString],
) -> Result<Option<(String, Vec<CString>)>, i32> {
    if !header.starts_with(b"#!") {
        return Ok(None);
    }
    let end = header
        .iter()
        .position(|&b| b == b'\n')
        .unwrap_or(header.len());
    if end == 256 {
        return Err(ENOEXEC);
    }
    let line = std::str::from_utf8(&header[2..end]).map_err(|_| ENOEXEC)?;
    let mut words = line.split_whitespace();
    let interpreter = words.next().filter(|s| s.starts_with('/')).ok_or(ENOEXEC)?;
    let mut next = vec![cstring(interpreter)?];
    if let Some(option) = words.next() {
        next.push(cstring(option)?);
    }
    // Native's script loader accepts one interpreter argument. Reject extras
    // rather than silently changing a hook's requested command line.
    if words.next().is_some() {
        return Err(ENOEXEC);
    }
    next.push(cstring(program)?);
    next.extend_from_slice(argv.get(1..).unwrap_or_default());
    if next.len() > MAX_STRINGS {
        return Err(E2BIG);
    }
    Ok(Some((interpreter.to_owned(), next)))
}

// Native Waitpid reports an exit *code*, not a POSIX wait status. It does not
// expose the termination cause. Never guess that exit(137) means SIGKILL.
fn wait_status(code: i32) -> i32 {
    (code & 255) << 8
}

fn shell_args(program: &str, argv: &[CString]) -> Result<Vec<CString>, i32> {
    let mut args = vec![cstring("/bin/sh")?, cstring(program)?];
    args.extend_from_slice(argv.get(1..).unwrap_or_default());
    if args.len() > MAX_STRINGS {
        return Err(E2BIG);
    }
    Ok(args)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn delta_preserves_empty_values_and_last_assignment_wins() {
        let mut env = BTreeMap::from([
            ("KEEP".into(), "parent".into()),
            ("REMOVE".into(), "parent".into()),
        ]);
        let delta = ["REMOVE", "EMPTY=", "X=first", "X=a=b"].map(|s| cstring(s).unwrap());
        apply_environment_delta(&mut env, &delta).unwrap();
        assert_eq!(env.get("KEEP").unwrap(), "parent");
        assert!(!env.contains_key("REMOVE"));
        assert_eq!(env.get("EMPTY").unwrap(), "");
        assert_eq!(env.get("X").unwrap(), "a=b");
        assert!(apply_environment_delta(&mut env, &[cstring("=bad").unwrap()]).is_err());
    }

    #[test]
    fn script_preserves_original_name_and_argument_boundaries() {
        let argv = ["ignored-argv0", "a b", "", "--literal"].map(|s| cstring(s).unwrap());
        let (program, args) = interpreter_args(b"#!/usr/bin/env sh\nbody", "hooks/my hook", &argv)
            .unwrap()
            .unwrap();
        assert_eq!(program, "/usr/bin/env");
        assert_eq!(
            args.iter().map(|s| s.to_str().unwrap()).collect::<Vec<_>>(),
            [
                "/usr/bin/env",
                "sh",
                "hooks/my hook",
                "a b",
                "",
                "--literal"
            ]
        );
        assert_eq!(
            interpreter_args(b"#!relative\n", "hook", &argv),
            Err(ENOEXEC)
        );
        assert_eq!(
            interpreter_args(b"#!/bin/sh -e -x\n", "hook", &argv),
            Err(ENOEXEC)
        );
    }

    #[test]
    fn native_limits_fail_before_starting_a_child() {
        assert_eq!(cstring(&"a".repeat(4096)), Err(E2BIG));
        let env = (0..257).map(|i| (format!("K{i}"), String::new())).collect();
        assert_eq!(encode_environment(env), Err(E2BIG));
    }

    #[test]
    fn native_exit_codes_are_not_guessed_signal_statuses() {
        for code in [0, 1, 42, 127, 128, 137, 143, 255] {
            let status = wait_status(code);
            assert_eq!(status & 127, 0);
            assert_eq!(status >> 8, code);
        }
    }

    #[test]
    fn execvp_shell_fallback_preserves_arguments() {
        let argv = ["old-argv0", "a b", ""].map(|s| cstring(s).unwrap());
        let args = shell_args("relative script", &argv).unwrap();
        assert_eq!(
            args.iter().map(|s| s.to_str().unwrap()).collect::<Vec<_>>(),
            ["/bin/sh", "relative script", "a b", ""]
        );
    }
}
