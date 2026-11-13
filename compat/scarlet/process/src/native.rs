use super::*;
use scarlet_abi::fs::{CURRENT_DIRECTORY, VFS_O_CLOEXEC, VFS_O_RDONLY};
use scarlet_abi::{RawEnvironmentExec, RawEnvironmentHandleMapping, RawFileMetadata, Syscall};
use scarlet_sys::{syscall0, syscall1, syscall2, syscall3, syscall4};
use std::ffi::{CStr, c_char, c_int};
use std::path::{Path, PathBuf};
use std::sync::Mutex;

const EIO: i32 = 5;
const EBADF: i32 = 9;
const ENOENT: i32 = 2;
const EFAULT: i32 = 14;
const EACCES: i32 = 13;
const ENOTDIR: i32 = 20;
const ENOTSUP: i32 = 95;
// Also cover current kernels, which increased the pinned ABI's 1024 slots.
// Older kernels return EBADF for these extra slots.
const MAX_HANDLES: i32 = 16_384;
// A redirected CLOEXEC stream needs a temporary ordinary dup because explicit
// Environment mappings preserve its flags. Serialize transitions so another
// Git spawn cannot inherit that short-lived descriptor as an ambient extra.
static TRANSITION_LOCK: Mutex<()> = Mutex::new(());
static ENVIRONMENT_LOCK: Mutex<()> = Mutex::new(());

unsafe extern "C" {
    fn __errno_location() -> *mut c_int;
}

fn fail(error: i32) -> i32 {
    // SAFETY: the SDK CRT initializes this thread's shared Native errno slot.
    unsafe { *__errno_location() = error };
    -1
}

fn status(result: usize) -> Result<usize, i32> {
    if result > isize::MAX as usize {
        Err(-(result as isize) as i32)
    } else {
        Ok(result)
    }
}

fn legacy(result: usize) -> Result<usize, i32> {
    if result == usize::MAX {
        Err(EIO)
    } else {
        status(result)
    }
}

unsafe fn text<'a>(pointer: *const c_char) -> Result<&'a str, i32> {
    if pointer.is_null() {
        return Err(EFAULT);
    }
    // SAFETY: callers promise a readable, NUL-terminated C string.
    let value = unsafe { CStr::from_ptr(pointer) }
        .to_str()
        .map_err(|_| EINVAL)?;
    if value.len() > MAX_STRING_BYTES {
        return Err(E2BIG);
    }
    Ok(value)
}

unsafe fn strings(pointer: *const *const c_char) -> Result<Vec<CString>, i32> {
    let mut values = Vec::new();
    if pointer.is_null() {
        return Ok(values);
    }
    for index in 0..=MAX_STRINGS {
        // SAFETY: the caller supplies a NULL-terminated array of string pointers.
        let item = unsafe { *pointer.add(index) };
        if item.is_null() {
            return Ok(values);
        }
        if index == MAX_STRINGS {
            return Err(E2BIG);
        }
        values.push(cstring(unsafe { text(item)? })?);
    }
    Err(E2BIG)
}

fn environment() -> Result<BTreeMap<String, String>, i32> {
    let _lock = ENVIRONMENT_LOCK.lock().unwrap();
    std::env::vars_os()
        .map(|(name, value)| {
            Ok((
                name.into_string().map_err(|_| EINVAL)?,
                value.into_string().map_err(|_| EINVAL)?,
            ))
        })
        .collect()
}

fn path_string(path: &Path) -> Result<CString, i32> {
    cstring(path.to_str().ok_or(EINVAL)?)
}

struct Handle(usize);

impl Drop for Handle {
    fn drop(&mut self) {
        // SAFETY: this guard owns exactly one temporary Native handle.
        unsafe { syscall1(Syscall::HandleCloseWithStatus, self.0) };
    }
}

fn metadata(path: &CStr) -> Result<RawFileMetadata, i32> {
    let mut value = RawFileMetadata::default();
    // SAFETY: path and output remain live for the synchronous syscall.
    status(unsafe {
        syscall3(
            Syscall::VfsMetadataWithStatus,
            path.as_ptr() as usize,
            &mut value as *mut _ as usize,
            0,
        )
    })?;
    Ok(value)
}

fn descriptor_flags(fd: i32) -> Result<usize, i32> {
    if fd < 0 {
        return Err(EBADF);
    }
    // SAFETY: read-only scalar descriptor query.
    status(unsafe { syscall1(Syscall::HandleGetDescriptorFlags, fd as usize) })
}

#[unsafe(no_mangle)]
pub extern "C" fn dup2(source: c_int, target: c_int) -> c_int {
    if source < 0 || target < 0 {
        return fail(EBADF);
    }
    // SAFETY: C descriptors are Native handle numbers. The kernel validates
    // both handles before atomically replacing target, shares the open-file
    // description, and clears CLOEXEC except for a valid source == target.
    // No userspace close or descriptor allocation may split this operation.
    match status(unsafe {
        syscall2(
            Syscall::HandleDuplicateToWithStatus,
            source as usize,
            target as usize,
        )
    }) {
        Ok(fd) if fd == target as usize => target,
        Ok(_) => fail(EIO),
        Err(error) => fail(error),
    }
}

struct HandleMappings {
    records: Vec<RawEnvironmentHandleMapping>,
    _stdio: Vec<Handle>,
}

fn mappings(stdio: [i32; 3], close_extra: bool) -> Result<HandleMappings, i32> {
    let mut map = Vec::new();
    for (target, source) in stdio.into_iter().enumerate() {
        if source == -1 {
            continue;
        }
        descriptor_flags(source)?;
        map.push(RawEnvironmentHandleMapping {
            source: source as u32,
            target: target as u32,
        });
    }
    if !close_extra {
        for fd in 3..MAX_HANDLES {
            if stdio.contains(&fd) {
                continue;
            }
            match descriptor_flags(fd) {
                Ok(0) => map.push(RawEnvironmentHandleMapping {
                    source: fd as u32,
                    target: fd as u32,
                }),
                Ok(_) | Err(EBADF) => {}
                Err(error) => return Err(error),
            }
        }
    }
    // Capture extras before opening temporary handles. Native's explicit map
    // preserves CLOEXEC; dup2-style stdio redirection must clear that flag in
    // the child without changing the borrowed parent's descriptor flags.
    let mut copies = Vec::new();
    for mapping in &mut map {
        if mapping.target < 3 && descriptor_flags(mapping.source as i32)? != 0 {
            let copy = Handle(status(unsafe {
                syscall1(Syscall::HandleDuplicateWithStatus, mapping.source as usize)
            })?);
            mapping.source = copy.0 as u32;
            copies.push(copy);
        }
    }
    Ok(HandleMappings {
        records: map,
        _stdio: copies,
    })
}

fn inherited_stdio() -> Result<[i32; 3], i32> {
    let mut fds = [-1; 3];
    for fd in 0..3 {
        match descriptor_flags(fd) {
            Ok(0) => fds[fd as usize] = fd,
            Ok(_) | Err(EBADF) => {}
            Err(error) => return Err(error),
        }
    }
    Ok(fds)
}

fn working_directory(requested: Option<&str>) -> Result<PathBuf, i32> {
    let current = std::env::current_dir().map_err(|e| e.raw_os_error().unwrap_or(EIO))?;
    let cwd = match requested {
        Some("") => return Err(ENOENT),
        Some(dir) => current.join(dir),
        None => current,
    };
    if metadata(&path_string(&cwd)?)?.file_type != scarlet_abi::FILE_TYPE_DIRECTORY {
        return Err(ENOTDIR);
    }
    Ok(cwd)
}

fn executable(
    mut program: String,
    cwd: &Path,
    mut argv: Vec<CString>,
) -> Result<(Handle, Vec<CString>), i32> {
    if program.is_empty() {
        return Err(ENOENT);
    }
    if argv.is_empty() {
        return Err(EINVAL);
    }
    for _ in 0..4 {
        let path = path_string(&cwd.join(&program))?;
        let info = metadata(&path)?;
        if info.file_type != scarlet_abi::FILE_TYPE_REGULAR
            || info.permissions & scarlet_abi::FILE_PERMISSION_EXECUTE == 0
        {
            return Err(EACCES);
        }
        // SAFETY: path is NUL terminated; all other arguments are ABI scalars.
        let file = Handle(status(unsafe {
            syscall4(
                Syscall::VfsOpenAt,
                CURRENT_DIRECTORY,
                path.as_ptr() as usize,
                (VFS_O_RDONLY | VFS_O_CLOEXEC) as usize,
                0,
            )
        })?);
        let mut header = [0u8; 256];
        // SAFETY: writes at most the length of the exclusive local buffer.
        let n = status(unsafe {
            syscall3(
                Syscall::StreamReadWithStatus,
                file.0,
                header.as_mut_ptr() as usize,
                header.len(),
            )
        })?;
        if n > header.len() {
            return Err(EIO);
        }
        // The image reader may use either positioned or ordinary reads.
        legacy(unsafe { syscall3(Syscall::FileSeek, file.0, 0, 0) })?;
        if let Some((next, args)) = interpreter_args(&header[..n], &program, &argv)? {
            program = next;
            argv = args;
            continue;
        }
        if !header[..n].starts_with(b"\x7fELF") {
            return Err(ENOEXEC);
        }
        return Ok((file, argv));
    }
    Err(ENOEXEC)
}

fn execute(
    operation: Syscall,
    program: &str,
    argv: Vec<CString>,
    env: Vec<CString>,
    cwd: &Path,
    handles: &[RawEnvironmentHandleMapping],
) -> Result<i32, i32> {
    // Descriptor inheritance was captured before opening either temporary handle.
    let (file, argv) = executable(program.to_owned(), &cwd, argv)?;
    let cwd = path_string(&cwd)?;
    let argv_ptrs: Vec<_> = argv
        .iter()
        .map(|s| s.as_ptr())
        .chain([std::ptr::null()])
        .collect();
    let env_ptrs: Vec<_> = env
        .iter()
        .map(|s| s.as_ptr())
        .chain([std::ptr::null()])
        .collect();
    // SAFETY: returns a new owned Environment capability; no pointer arguments.
    let environment = Handle(legacy(unsafe { syscall0(Syscall::EnvironmentCurrent) })?);
    let options = RawEnvironmentExec {
        size: std::mem::size_of::<RawEnvironmentExec>() as u32,
        flags: 0,
        argv: argv_ptrs.as_ptr() as usize,
        envp: env_ptrs.as_ptr() as usize,
        cwd: cwd.as_ptr() as usize,
        handles: handles.as_ptr() as usize,
        handle_count: handles.len(),
    };
    // SAFETY: all C strings/arrays and borrowed mappings remain live until this
    // synchronous call copies them. Exec success replaces this process image.
    let result = legacy(unsafe {
        syscall4(
            operation,
            environment.0,
            file.0,
            &options as *const _ as usize,
            0,
        )
    })?;
    if operation == Syscall::EnvironmentExec || result == 0 || result > i32::MAX as usize {
        return Err(EIO);
    }
    Ok(result as i32)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn scarlet_spawnve(
    path: *const c_char,
    argv: *const *const c_char,
    delta: *const *const c_char,
    cwd: *const c_char,
    input: i32,
    output: i32,
    error: i32,
    close_extra: i32,
) -> i32 {
    let result = (|| {
        let _transition = TRANSITION_LOCK.lock().unwrap();
        let path = unsafe { text(path)? };
        let argv = unsafe { strings(argv)? };
        let mut env = environment()?;
        apply_environment_delta(&mut env, &unsafe { strings(delta)? })?;
        let handles = mappings([input, output, error], close_extra != 0)?;
        let cwd = working_directory(if cwd.is_null() {
            None
        } else {
            Some(unsafe { text(cwd)? })
        })?;
        execute(
            Syscall::EnvironmentSpawn,
            path,
            argv,
            encode_environment(env)?,
            &cwd,
            &handles.records,
        )
    })();
    result.unwrap_or_else(fail)
}

fn exec(
    program: &str,
    argv: Vec<CString>,
    env: Vec<CString>,
    search_path: bool,
) -> Result<i32, i32> {
    if program.is_empty() {
        return Err(ENOENT);
    }
    let _transition = TRANSITION_LOCK.lock().unwrap();
    let handles = mappings(inherited_stdio()?, false)?;
    let cwd = working_directory(None)?;
    let attempt = |candidate: &str| match execute(
        Syscall::EnvironmentExec,
        candidate,
        argv.clone(),
        env.clone(),
        &cwd,
        &handles.records,
    ) {
        Err(ENOEXEC) if search_path => execute(
            Syscall::EnvironmentExec,
            "/bin/sh",
            shell_args(candidate, &argv)?,
            env.clone(),
            &cwd,
            &handles.records,
        ),
        result => result,
    };
    if search_path && !program.contains('/') {
        let path = env
            .iter()
            .find_map(|e| e.to_str().ok()?.strip_prefix("PATH="))
            .unwrap_or("/bin:/usr/bin");
        let mut denied = false;
        for part in path.split(':') {
            let candidate = Path::new(part).join(program);
            let candidate = candidate.to_str().ok_or(EINVAL)?;
            match attempt(candidate) {
                Err(ENOENT | ENOTDIR) => {}
                Err(EACCES) => denied = true,
                result => return result,
            }
        }
        return Err(if denied { EACCES } else { ENOENT });
    }
    attempt(program)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn execve(
    path: *const c_char,
    argv: *const *const c_char,
    envp: *const *const c_char,
) -> i32 {
    (|| {
        exec(
            unsafe { text(path)? },
            unsafe { strings(argv)? },
            unsafe { strings(envp)? },
            false,
        )
    })()
    .unwrap_or_else(fail)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn execv(path: *const c_char, argv: *const *const c_char) -> i32 {
    (|| {
        exec(
            unsafe { text(path)? },
            unsafe { strings(argv)? },
            encode_environment(environment()?)?,
            false,
        )
    })()
    .unwrap_or_else(fail)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn execvp(path: *const c_char, argv: *const *const c_char) -> i32 {
    (|| {
        exec(
            unsafe { text(path)? },
            unsafe { strings(argv)? },
            encode_environment(environment()?)?,
            true,
        )
    })()
    .unwrap_or_else(fail)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn waitpid(pid: i32, output: *mut i32, options: i32) -> i32 {
    // Git waits for known children. Native wait-any has no reliable ECHILD
    // result when the caller has no children, so do not expose it as POSIX.
    if pid <= 0 {
        return fail(ENOTSUP);
    }
    if options & !1 != 0 {
        return fail(EINVAL);
    }
    let mut code = 0i32;
    // SAFETY: exclusive local status output, positive PID and WNOHANG only.
    match legacy(unsafe {
        syscall3(
            Syscall::Waitpid,
            pid as usize,
            &mut code as *mut _ as usize,
            options as usize,
        )
    }) {
        Ok(0) => 0,
        Ok(waited) if waited == pid as usize => {
            if !output.is_null() {
                // SAFETY: caller provides a writable optional status cell.
                unsafe { *output = wait_status(code) };
            }
            pid
        }
        Ok(_) => fail(EIO),
        Err(error) => fail(error),
    }
}

#[unsafe(no_mangle)]
pub extern "C" fn kill(pid: i32, signal: i32) -> i32 {
    if pid <= 0 {
        return fail(ENOTSUP);
    }
    if !matches!(signal, 0 | 1 | 2 | 3 | 9 | 15 | 18 | 19 | 20 | 21 | 22 | 28) {
        return fail(ENOTSUP);
    }
    // SAFETY: Native Kill accepts a positive namespace-local PID and scalar signal.
    match legacy(unsafe { syscall2(Syscall::Kill, pid as usize, signal as usize) }) {
        Ok(0) => 0,
        Ok(_) => fail(EIO),
        Err(error) => fail(error),
    }
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn setenv(name: *const c_char, value: *const c_char, overwrite: i32) -> i32 {
    let result = (|| {
        let name = unsafe { text(name)? };
        let value = unsafe { text(value)? };
        if name.is_empty() || name.contains('=') {
            return Err(EINVAL);
        }
        let _lock = ENVIRONMENT_LOCK.lock().unwrap();
        if overwrite != 0 || std::env::var_os(name).is_none() {
            // SAFETY: Scarlet std serializes its environment map, and does not
            // hand borrowed environment storage to another thread.
            unsafe { std::env::set_var(name, value) };
        }
        Ok(0)
    })();
    result.unwrap_or_else(fail)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn unsetenv(name: *const c_char) -> i32 {
    let result = (|| {
        let name = unsafe { text(name)? };
        if name.is_empty() || name.contains('=') {
            return Err(EINVAL);
        }
        let _lock = ENVIRONMENT_LOCK.lock().unwrap();
        // SAFETY: same synchronized Native std environment as setenv/getenv.
        unsafe { std::env::remove_var(name) };
        Ok(0)
    })();
    result.unwrap_or_else(fail)
}

#[unsafe(no_mangle)]
pub unsafe extern "C" fn putenv(value: *mut c_char) -> i32 {
    let value = match unsafe { text(value) } {
        Ok(v) => v,
        Err(e) => return fail(e),
    };
    if value.is_empty() {
        return fail(EINVAL);
    }
    if value.contains('=') {
        // The SDK getenv owns copies in Rust std. A real POSIX putenv requires
        // observing later writes to the caller's buffer; a copy would lie.
        return fail(ENOTSUP);
    }
    let _lock = ENVIRONMENT_LOCK.lock().unwrap();
    unsafe { std::env::remove_var(value) };
    0
}
