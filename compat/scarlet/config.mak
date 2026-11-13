# Native Scarlet ABI, using scarlet-sdk's C runtime (not Linux emulation).
# GIT-CFLAGS tracks this content ID, including SDK paths hidden by cc.sh.
SCARLET_BUILD_ID := $(shell python3 compat/scarlet/build_inputs.py)
ifeq ($(strip $(SCARLET_BUILD_ID)),)
$(error Cannot fingerprint Scarlet build inputs; use nix develop)
endif
BASIC_CFLAGS += -DSCARLET_BUILD_ID=scarlet_$(SCARLET_BUILD_ID)
BASIC_CFLAGS += -D__scarlet__=1
BASIC_CFLAGS += -ffunction-sections -fdata-sections -fno-stack-protector
BASIC_CFLAGS += -DNO_SYSLOG -DNO_INITGROUPS -DNO_GETRLIMIT
BASIC_CFLAGS += -DNO_POSIX_GOODIES
HAVE_ALLOCA_H = YesPlease
HAVE_STRINGS_H = YesPlease
HAVE_CLOCK_GETTIME = YesPlease
HAVE_CLOCK_MONOTONIC = YesPlease
CSPRNG_METHOD = getrandom
NO_STRLCPY = YesPlease
NO_STRCASESTR = YesPlease
NO_MEMMEM = YesPlease
NO_GETPAGESIZE = YesPlease
NO_REGEX = YesPlease
NO_LIBGEN_H = YesPlease
NO_ICONV = YesPlease
NO_GETTEXT = YesPlease
NO_OPENSSL = YesPlease
NO_CURL = YesPlease
NO_EXPAT = YesPlease
NO_PERL = YesPlease
NO_PYTHON = YesPlease
NO_TCLTK = YesPlease
NO_UNIX_SOCKETS = YesPlease
NO_MMAP = YesPlease
NO_WRITEV = YesPlease
NO_MKDTEMP = YesPlease
NO_RUST = YesPlease
NO_SYS_POLL_H = YesPlease
PTHREAD_LIBS =
PTHREAD_CFLAGS =
SHA256_BLK = YesPlease
SKIP_DASHED_BUILT_INS = YesPlease
SHELL_PATH = /bin/sh
DEFAULT_PAGER = cat
COMPAT_OBJS += compat/scarlet/compat.o
COMPAT_OBJS += compat/scarlet/strftime.o

# Track every native link input as well as the CFLAGS fingerprint.
SCARLET_LINK_INPUTS = compat/scarlet/cc.sh compat/scarlet/mark-elf.py \
    $(SCARLET_CC) $(SCARLET_LD) \
    $(SCARLET_RUNTIME)/lib/libscarlet_c_startup.a \
    $(SCARLET_RUNTIME)/lib/libscarlet_cxx_support.a \
    $(SCARLET_RUNTIME)/lib/libscarlet_c.a \
    compat/scarlet/process/target/aarch64-unknown-scarlet/release/libgit_scarlet_process.a
ifneq ($(strip $(ZLIB_PATH)),)
SCARLET_LINK_INPUTS += $(ZLIB_PATH)/lib/libz.a
endif
# Keep the CRT dependency behind a content stamp: Git's link rules collect
# every .o prerequisite into argv, and cc.sh already supplies this object.
.scarlet-build/link-inputs.json: $(SCARLET_RUST_TOOLCHAIN)/lib/rustlib/aarch64-unknown-scarlet/lib/scarlet-crt0.o FORCE
	@python3 compat/scarlet/build_inputs.py --write $@ >/dev/null
git git-remote-http git-http-fetch: $(SCARLET_LINK_INPUTS) .scarlet-build/link-inputs.json
git-remote-http git-http-fetch: compat/scarlet/http/target/install/lib/libcurl.a
