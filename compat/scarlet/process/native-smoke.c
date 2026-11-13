/* Build with compat/scarlet/cc.sh and run with an absolute argv[0] in Scarlet. */
#include "../process.h"
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CHECK(condition) do { \
	if (!(condition)) { \
		fprintf(stderr, "process smoke line %d failed: %s (errno %d)\n", \
			__LINE__, #condition, errno); \
		return 1; \
	} \
} while (0)

static int child(int argc, char **argv)
{
	char cwd[4096], byte;
	char *next[] = { argv[0], "execvp", NULL };
	CHECK(argc == 2);
	CHECK(getcwd(cwd, sizeof(cwd)) && !strcmp(cwd, "/"));
	CHECK(getenv("SCARLET_PROCESS_TEST") &&
	      !strcmp(getenv("SCARLET_PROCESS_TEST"), "child"));
	CHECK(!getenv("SCARLET_PROCESS_REMOVE"));
	CHECK(getenv("SCARLET_PROCESS_EMPTY") &&
	      !strcmp(getenv("SCARLET_PROCESS_EMPTY"), ""));
	CHECK(fcntl(0, F_GETFD) == 0);
	CHECK(fcntl(1, F_GETFD) == 0);
	CHECK(fcntl(2, F_GETFD) == 0);
	if (!strcmp(argv[1], "child")) {
		execv(argv[0], next);
		CHECK(0);
	}
	if (!strcmp(argv[1], "execvp")) {
		char *base = strrchr(argv[0], '/');
		CHECK(base);
		*base = '\0';
		CHECK(!setenv("PATH", argv[0], 1));
		next[0] = base + 1;
		next[1] = "final";
		execvp(next[0], next);
		CHECK(0);
	}
	CHECK(!strcmp(argv[1], "final"));
	CHECK(read(0, &byte, 1) == 1 && byte == 'I');
	CHECK(write(1, "O", 1) == 1);
	CHECK(write(2, "E", 1) == 1);
	return 42;
}

int main(int argc, char **argv)
{
	const char *delta[] = { "SCARLET_PROCESS_TEST=child",
		"SCARLET_PROCESS_REMOVE", "SCARLET_PROCESS_EMPTY=", NULL };
	const char *args[] = { argv[0], "child", NULL };
	int input[2], output[2], status = -1, pid, target;
	char bytes[3] = { 0 }, put[] = "SCARLET_PROCESS_PUT=unsupported";
	ssize_t count, total = 0;
	if (argc > 1 && !strcmp(argv[1], "hold")) {
		CHECK(read(0, bytes, 1) == 0);
		return 0;
	}
	if (argc > 1)
		return child(argc, argv);
	CHECK(argv[0][0] == '/');
	CHECK(!setenv("SCARLET_PROCESS_TEST", "parent", 1));
	CHECK(!setenv("SCARLET_PROCESS_TEST", "ignored", 0));
	CHECK(!strcmp(getenv("SCARLET_PROCESS_TEST"), "parent"));
	CHECK(!setenv("SCARLET_PROCESS_REMOVE", "parent", 1));
	CHECK(!setenv("SCARLET_PROCESS_EMPTY", "parent", 1));
	CHECK(!setenv("SCARLET_PROCESS_UNSET", "value", 1));
	CHECK(!unsetenv("SCARLET_PROCESS_UNSET"));
	CHECK(!getenv("SCARLET_PROCESS_UNSET"));
	errno = 0;
	CHECK(putenv(put) == -1 && errno == ENOTSUP);
	CHECK(!pipe2(output, O_CLOEXEC));
	target = open("/dev/null", O_WRONLY | O_CLOEXEC);
	CHECK(target >= 0);
	CHECK(dup2(target, target) == target);
	CHECK(fcntl(target, F_GETFD) == FD_CLOEXEC);
	CHECK(dup2(output[1], target) == target);
	CHECK(fcntl(target, F_GETFD) == 0);
	errno = 0;
	CHECK(dup2(-1, target) == -1 && errno == EBADF);
	errno = 0;
	CHECK(dup2(16384, target) == -1 && errno == EBADF);
	CHECK(write(target, "D", 1) == 1);
	CHECK(read(output[0], bytes, 1) == 1 && bytes[0] == 'D');
	bytes[0] = 0;
	CHECK(!close(target));
	CHECK(!close(output[0]));
	CHECK(!close(output[1]));
	CHECK(!pipe2(input, O_CLOEXEC));
	CHECK(!pipe2(output, O_CLOEXEC));
	pid = scarlet_spawnve(argv[0], args, delta, "/",
			      input[0], output[1], output[1], 1);
	CHECK(pid > 0);
	CHECK(fcntl(input[0], F_GETFD) == FD_CLOEXEC);
	CHECK(fcntl(output[1], F_GETFD) == FD_CLOEXEC);
	CHECK(!close(input[0]));
	CHECK(!close(output[1]));
	CHECK(write(input[1], "I", 1) == 1);
	CHECK(!close(input[1]));
	while ((count = read(output[0], bytes + total, 2 - total)) > 0) {
		total += count;
		if (total == 2)
			break;
	}
	CHECK(total == 2 && !strcmp(bytes, "OE"));
	CHECK(read(output[0], bytes, 1) == 0);
	CHECK(!close(output[0]));
	CHECK(waitpid(pid, &status, 0) == pid && status == (42 << 8));
	CHECK(!strcmp(getenv("SCARLET_PROCESS_TEST"), "parent"));
	CHECK(!strcmp(getenv("SCARLET_PROCESS_REMOVE"), "parent"));
	errno = 0;
	CHECK(scarlet_spawnve("/nonexistent-git-process-test", args, NULL,
			      NULL, 0, 1, 2, 1) == -1 && errno == ENOENT);
	errno = 0;
	CHECK(scarlet_spawnve(argv[0], args, NULL,
		"/nonexistent-git-process-test", 0, 1, 2, 1) == -1 && errno == ENOENT);
	CHECK(!pipe2(input, O_CLOEXEC));
	args[1] = "hold";
	pid = scarlet_spawnve(argv[0], args, NULL, NULL, input[0], 1, 2, 1);
	CHECK(pid > 0);
	CHECK(!close(input[0]));
	status = 1234;
	CHECK(waitpid(pid, &status, 1) == 0 && status == 1234);
	CHECK(!kill(pid, 9));
	CHECK(waitpid(pid, &status, 0) == pid && status == (137 << 8));
	CHECK(!close(input[1]));
	puts("Scarlet process smoke passed");
	return 0;
}
