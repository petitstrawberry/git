#ifndef GIT_SCARLET_POSIX_H
#define GIT_SCARLET_POSIX_H

#include <stdint.h>
#include <pwd.h>
#include <sys/time.h>
#include <sys/types.h>

#define S_ISUID 04000
#define S_ISGID 02000
#define S_ISVTX 01000
#ifndef ENODEV
#define ENODEV 19
#endif
#ifndef ENOEXEC
#define ENOEXEC 8
#endif

/* Git's process-local signal bookkeeping. Native Scarlet has no POSIX
 * asynchronous signal delivery. External cancellation uses task termination. */
typedef int sig_atomic_t;
typedef uint64_t sigset_t;
typedef void (*scarlet_sighandler_t)(int);
#define SIGHUP 1
#define SIGINT 2
#define SIGQUIT 3
#define SIGABRT 6
#define SIGKILL 9
#define SIGPIPE 13
#define SIGALRM 14
#define SIGTERM 15
#define SIGCHLD 17
#define SIGCONT 18
#define SIGSTOP 19
#define SIGTSTP 20
#define SIGTTIN 21
#define SIGTTOU 22
#define SIGWINCH 28
#define NSIG 32
#define SIG_DFL ((scarlet_sighandler_t)0)
#define SIG_IGN ((scarlet_sighandler_t)1)
#define SIG_ERR ((scarlet_sighandler_t)-1)
#define SIG_BLOCK 0
#define SIG_UNBLOCK 1
#define SIG_SETMASK 2
#define SA_RESTART 0
#define SA_NOCLDSTOP 1
struct sigaction { scarlet_sighandler_t sa_handler; sigset_t sa_mask; int sa_flags; };
scarlet_sighandler_t signal(int, scarlet_sighandler_t);
int raise(int);
int sigaction(int, const struct sigaction *, struct sigaction *);
int sigemptyset(sigset_t *);
int sigfillset(sigset_t *);
int sigaddset(sigset_t *, int);
int sigprocmask(int, const sigset_t *, sigset_t *);
int pthread_sigmask(int, const sigset_t *, sigset_t *);
unsigned int alarm(unsigned int);

#define WNOHANG 1
#define WIFEXITED(s) (((s) & 0x7f) == 0)
#define WEXITSTATUS(s) (((s) >> 8) & 0xff)
#define WIFSIGNALED(s) (((s) & 0x7f) != 0)
#define WTERMSIG(s) ((s) & 0x7f)
pid_t waitpid(pid_t, int *, int);
int kill(pid_t, int);
pid_t fork(void);
int execvp(const char *, char *const *);
int execv(const char *, char *const *);
int execl(const char *, const char *, ...);
int execlp(const char *, const char *, ...);
int pipe(int *);
int dup2(int, int);

struct utimbuf { time_t actime, modtime; };
int utime(const char *, const struct utimbuf *);
struct utsname { char sysname[65], nodename[65], release[65], version[65], machine[65]; };
int uname(struct utsname *);
int gethostname(char *, size_t);
struct passwd *getpwuid(uid_t);
struct passwd *getpwnam(const char *);
int scarlet_chmod(const char *, mode_t);
#define chmod scarlet_chmod
int scarlet_mkdir(const char *, mode_t);
#define mkdir scarlet_mkdir
int setenv(const char *, const char *, int);
int unsetenv(const char *);
int putenv(char *);
char *getpass(const char *);
extern char **environ;
unsigned int sleep(unsigned int);
int usleep(unsigned int);
int fchdir(int);
int setpgid(pid_t, pid_t);
pid_t setsid(void);
pid_t tcgetpgrp(int);
int getdtablesize(void);
struct rusage { struct timeval ru_utime, ru_stime; long ru_maxrss; };
#define RUSAGE_SELF 0
#define RUSAGE_CHILDREN -1
int getrusage(int, struct rusage *);
struct itimerval { struct timeval it_interval, it_value; };
#define ITIMER_REAL 0

#endif
