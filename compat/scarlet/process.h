#ifndef GIT_SCARLET_PROCESS_H
#define GIT_SCARLET_PROCESS_H

#include <sys/types.h>

/*
 * Spawn a Native image; env_delta contains NAME=value assignments or NAME
 * deletions, terminated by NULL. A NULL delta inherits the Rust environment.
 * cwd == NULL inherits the working directory. Each stdio argument is a borrowed
 * Native descriptor, or -1 to leave that stream closed in the child.
 * No descriptors are consumed, including on failure. Only non-CLOEXEC extra
 * descriptors survive unless close_extra_fds is nonzero. Stdio source fds are
 * mapped to 0/1/2 and are not also inherited at their original numbers.
 */
pid_t scarlet_spawnve(const char *path, const char *const *argv,
		     const char *const *env_delta, const char *cwd,
		     int input, int output, int error, int close_extra_fds);

/* These missing C exports are supplied by process/libgit_scarlet_process.a. */
int execve(const char *, char *const *, char *const *);
int execv(const char *, char *const *);
int execvp(const char *, char *const *);
int dup2(int, int);
pid_t waitpid(pid_t, int *, int);
int kill(pid_t, int);
int setenv(const char *, const char *, int);
int unsetenv(const char *);
int putenv(char *);

#endif
