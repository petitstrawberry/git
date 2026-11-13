#include "../../git-compat-util.h"
#include <pthread.h>
#include "../../strvec.h"

/* Scarlet has no asynchronous POSIX signals. These handlers are used for
 * Git's explicit raise() paths (notably EPIPE) and cleanup chaining. */
static pthread_mutex_t signal_lock = PTHREAD_MUTEX_INITIALIZER;
static struct sigaction signal_actions[NSIG];
struct scarlet_thread_state {
	sigset_t signal_mask, pending_signals;
	struct passwd passwd_entry;
	char passwd_buffer[16384];
};
static pthread_once_t thread_state_once = PTHREAD_ONCE_INIT;
static pthread_key_t thread_state_key;
static int thread_state_error;

static void initialize_thread_state(void)
{
	thread_state_error = pthread_key_create(&thread_state_key, free);
}

static struct scarlet_thread_state *get_thread_state(void)
{
	struct scarlet_thread_state *state;
	int err = pthread_once(&thread_state_once, initialize_thread_state);
	if (err || thread_state_error) {
		errno = err ? err : thread_state_error;
		return NULL;
	}
	state = pthread_getspecific(thread_state_key);
	if (!state) {
		state = calloc(1, sizeof(*state));
		if (!state)
			return NULL;
		err = pthread_setspecific(thread_state_key, state);
		if (err) {
			free(state);
			errno = err;
			return NULL;
		}
	}
	return state;
}

static int valid_signal(int sig)
{
	if (sig <= 0 || sig >= NSIG) {
		errno = EINVAL;
		return 0;
	}
	return 1;
}

int sigaction(int sig, const struct sigaction *action, struct sigaction *old)
{
	if (!valid_signal(sig))
		return -1;
	if (action && (sig == SIGKILL || sig == SIGSTOP ||
		      (action->sa_flags & ~SA_NOCLDSTOP))) {
		errno = EINVAL;
		return -1;
	}
	pthread_mutex_lock(&signal_lock);
	if (old)
		*old = signal_actions[sig];
	if (action)
		signal_actions[sig] = *action;
	pthread_mutex_unlock(&signal_lock);
	return 0;
}

scarlet_sighandler_t signal(int sig, scarlet_sighandler_t handler)
{
	struct sigaction action = { .sa_handler = handler }, old;
	if (sigaction(sig, &action, &old))
		return SIG_ERR;
	return old.sa_handler;
}

int raise(int sig)
{
	struct sigaction action;
	struct scarlet_thread_state *state;
	if (!valid_signal(sig))
		return -1;
	if (sig == SIGSTOP) {
		errno = ENOTSUP;
		return -1;
	}
	if (sig == SIGKILL)
		_exit(128 + sig);
	state = get_thread_state();
	if (!state)
		return -1;
	if (state->signal_mask & (UINT64_C(1) << sig)) {
		state->pending_signals |= UINT64_C(1) << sig;
		return 0;
	}
	if (sigaction(sig, NULL, &action))
		return -1;
	if (action.sa_handler == SIG_IGN)
		return 0;
	if (action.sa_handler == SIG_DFL) {
		if (sig == SIGCHLD || sig == SIGCONT || sig == SIGWINCH)
			return 0;
		_exit(128 + sig);
	}
	action.sa_handler(sig);
	return 0;
}

int sigemptyset(sigset_t *set) { *set = 0; return 0; }
int sigfillset(sigset_t *set) { *set = UINT64_MAX; return 0; }
int sigaddset(sigset_t *set, int sig)
{
	if (!valid_signal(sig))
		return -1;
	*set |= UINT64_C(1) << sig;
	return 0;
}

int sigprocmask(int how, const sigset_t *set, sigset_t *old)
{
	int sig;
	struct scarlet_thread_state *state = get_thread_state();
	if (!state)
		return -1;
	if (old)
		*old = state->signal_mask;
	if (!set)
		return 0;
	switch (how) {
	case SIG_BLOCK: state->signal_mask |= *set; break;
	case SIG_UNBLOCK: state->signal_mask &= ~*set; break;
	case SIG_SETMASK: state->signal_mask = *set; break;
	default: errno = EINVAL; return -1;
	}
	state->signal_mask &= ~((UINT64_C(1) << SIGKILL) | (UINT64_C(1) << SIGSTOP));
	for (sig = 1; sig < NSIG; sig++) {
		sigset_t bit = UINT64_C(1) << sig;
		if ((state->pending_signals & bit) && !(state->signal_mask & bit)) {
			state->pending_signals &= ~bit;
			raise(sig);
		}
	}
	return 0;
}

int pthread_sigmask(int how, const sigset_t *set, sigset_t *old)
{
	return sigprocmask(how, set, old) ? errno : 0;
}

int utime(const char *path, const struct utimbuf *times)
{
	struct timespec ts[2];
	if (!times)
		return utimensat(AT_FDCWD, path, NULL, 0);
	ts[0].tv_sec = times->actime;
	ts[1].tv_sec = times->modtime;
	ts[0].tv_nsec = ts[1].tv_nsec = 0;
	return utimensat(AT_FDCWD, path, ts, 0);
}

struct passwd *getpwuid(uid_t uid)
{
	struct scarlet_thread_state *state = get_thread_state();
	struct passwd *result;
	int err;
	if (!state)
		return NULL;
	err = getpwuid_r(uid, &state->passwd_entry, state->passwd_buffer,
			sizeof(state->passwd_buffer), &result);
	if (err) {
		errno = err;
		return NULL;
	}
	return result;
}

struct passwd *getpwnam(const char *name)
{
	struct passwd *entry = getpwuid(getuid());
	return entry && !strcmp(name, entry->pw_name) ? entry : NULL;
}

int gethostname(char *name, size_t size)
{
	const char *value = getenv("HOSTNAME");
	if (!value) {
		errno = ENOSYS;
		return -1;
	}
	if (strlen(value) >= size) {
		errno = ENAMETOOLONG;
		return -1;
	}
	memcpy(name, value, strlen(value) + 1);
	return 0;
}

int uname(struct utsname *info)
{
	memset(info, 0, sizeof(*info));
	strlcpy(info->sysname, "Scarlet", sizeof(info->sysname));
	strlcpy(info->release, "unknown", sizeof(info->release));
	strlcpy(info->version, "Native ABI", sizeof(info->version));
#if defined(__aarch64__)
	strlcpy(info->machine, "aarch64", sizeof(info->machine));
#elif defined(__riscv)
	strlcpy(info->machine, "riscv64", sizeof(info->machine));
#else
#error Unsupported Scarlet architecture
#endif
	if (gethostname(info->nodename, sizeof(info->nodename)))
		info->nodename[0] = '\0';
	return 0;
}

unsigned int sleep(unsigned int seconds)
{
	struct timespec requested = { seconds, 0 }, remaining;
	if (!nanosleep(&requested, &remaining))
		return 0;
	return errno == EINTR ? remaining.tv_sec + !!remaining.tv_nsec : seconds;
}

int usleep(unsigned int micros)
{
	struct timespec requested = { micros / 1000000, (micros % 1000000) * 1000 };
	return nanosleep(&requested, NULL);
}

static int exec_list(const char *file, const char *arg, va_list ap, int search)
{
	struct strvec argv = STRVEC_INIT;
	int result, saved_errno;
	while (arg) {
		strvec_push(&argv, arg);
		arg = va_arg(ap, const char *);
	}
	result = search ? execvp(file, (char *const *)argv.v) :
			  execv(file, (char *const *)argv.v);
	saved_errno = errno;
	strvec_clear(&argv);
	errno = saved_errno;
	return result;
}

int execl(const char *file, const char *arg, ...)
{
	int result;
	va_list ap;
	va_start(ap, arg);
	result = exec_list(file, arg, ap, 0);
	va_end(ap);
	return result;
}

int execlp(const char *file, const char *arg, ...)
{
	int result;
	va_list ap;
	va_start(ap, arg);
	result = exec_list(file, arg, ap, 1);
	va_end(ap);
	return result;
}

char *getpass(const char *prompt UNUSED)
{
	/* Never read a password without being able to disable terminal echo.
	 * GIT_ASKPASS and credential helpers use the normal process backend. */
	errno = ENOTSUP;
	return NULL;
}

int scarlet_chmod(const char *path, mode_t mode)
{
	struct stat st;
	if (stat(path, &st))
		return -1;
	/* Copying unchanged metadata is valid even though Native has no chmod
	 * operation. A requested change (including the executable-bit probe)
	 * must fail instead of claiming that permissions were changed. */
	if ((st.st_mode & 07777) == (mode & 07777))
		return 0;
	errno = ENOTSUP;
	return -1;
}

void setbuf(FILE *stream, char *buffer)
{
	setvbuf(stream, buffer, buffer ? _IOFBF : _IONBF, BUFSIZ);
}

int statvfs(const char *path UNUSED, struct statvfs *out UNUSED)
{
	/* Native has no filesystem-capacity query. Do not invent disk space. */
	errno = ENOSYS;
	return -1;
}

#undef mkdir
int scarlet_mkdir(const char *path, mode_t mode)
{
	/* Git's ordinary directories request 0777 before the platform's default
	 * mask. Native supports 0755 directory creation. Forward other modes so
	 * the SDK rejects unsupported protection requests (e.g. private 0700). */
	return mkdir(path, mode == 0777 ? 0755 : mode);
}
