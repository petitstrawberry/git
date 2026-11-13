//! Link libcurl and rustls to the shared Scarlet native C runtime.
//!
//! The archive packager uses dependency rlibs, excluding Rust's std and
//! allocator shim, which already belong to libscarlet_c.a.

extern crate curl_sys;
extern crate rustls_ffi;

#[cfg(not(target_os = "scarlet"))]
compile_error!("this integration is only for the native Scarlet ABI");

// Reuse the SDK's entropy-checked implementation instead of exporting another
// libc or embedding a second version of Scarlet's syscall ABI in this library.
fn fill_random(mut dest: &mut [u8]) -> Result<(), getrandom::Error> {
    unsafe extern "C" {
        fn getrandom(buffer: *mut u8, count: usize, flags: u32) -> isize;
    }
    while !dest.is_empty() {
        // flags=0 requires a registered entropy source in scarlet-libc.
        let count = unsafe { getrandom(dest.as_mut_ptr(), dest.len(), 0) };
        if count <= 0 || count as usize > dest.len() {
            return Err(getrandom::Error::UNEXPECTED);
        }
        dest = &mut dest[count as usize..];
    }
    Ok(())
}

getrandom::register_custom_getrandom!(fill_random);
