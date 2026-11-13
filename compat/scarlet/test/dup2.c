/* Exercise the native ABI, independently of the C process bridge's dup2. */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static long duplicate_to(long source, long target)
{
#if defined(__aarch64__)
	register long result __asm__("x0") = source;
	register long destination __asm__("x1") = target;
	register long number __asm__("x8") = 111;
	__asm__ volatile("svc #0" : "+r" (result)
			 : "r" (destination), "r" (number) : "memory", "cc");
#elif defined(__riscv) && __riscv_xlen == 64
	register long result __asm__("a0") = source;
	register long destination __asm__("a1") = target;
	register long number __asm__("a7") = 111;
	__asm__ volatile("ecall" : "+r" (result)
			 : "r" (destination), "r" (number) : "memory");
#else
#error Native Scarlet AArch64 or RISC-V 64 is required
#endif
	return result;
}

#define CHECK(expression) do { \
	if (!(expression)) { \
		fprintf(stderr, "SCARLET_DUP2_PROBE FAIL line=%d errno=%d: %s\n", \
			__LINE__, errno, #expression); \
		return 1; \
	} \
} while (0)

int main(void)
{
	const char redirected[] = "redirected via native syscall 111\n";
	const char passed[] = "SCARLET_DUP2_PROBE PASS\n";
	char data[sizeof(redirected)];
	int source, target, saved_stdout;

	source = open("dup2-source", O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	target = open("dup2-replaced", O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
	CHECK(source > STDERR_FILENO && target > STDERR_FILENO && source != target);
	CHECK(write(source, "ABCDEF", 6) == 6);
	CHECK(write(target, "old", 3) == 3);
	CHECK(fcntl(source, F_GETFD) == FD_CLOEXEC);
	CHECK(fcntl(target, F_GETFD) == FD_CLOEXEC);
	CHECK(duplicate_to(source, source) == source);
	CHECK(fcntl(source, F_GETFD) == FD_CLOEXEC);
	CHECK(duplicate_to(source, target) == target);
	CHECK(fcntl(target, F_GETFD) == 0);
	CHECK(fcntl(source, F_GETFD) == FD_CLOEXEC);

	CHECK(lseek(source, 0, SEEK_SET) == 0);
	CHECK(read(source, data, 2) == 2 && memcmp(data, "AB", 2) == 0);
	CHECK(read(target, data, 2) == 2 && memcmp(data, "CD", 2) == 0);
	CHECK(lseek(source, 0, SEEK_CUR) == 4);
	CHECK(fcntl(target, F_SETFD, FD_CLOEXEC) == 0);
	CHECK(duplicate_to(-1, target) == -EBADF);
	CHECK(duplicate_to(-1, -1) == -EBADF);
	CHECK(duplicate_to(source, -1) == -EBADF);
	CHECK(fcntl(target, F_GETFD) == FD_CLOEXEC);
	CHECK(read(target, data, 2) == 2 && memcmp(data, "EF", 2) == 0);
	CHECK(close(source) == 0);
	CHECK(duplicate_to(source, target) == -EBADF);
	CHECK(duplicate_to(source, source) == -EBADF);
	CHECK(lseek(target, 0, SEEK_CUR) == 6);
	CHECK(close(target) == 0);

	/* Replaced descriptor closure must not change its previous backing file. */
	target = open("dup2-replaced", O_RDONLY);
	CHECK(target >= 0);
	CHECK(read(target, data, sizeof(data)) == 3 && memcmp(data, "old", 3) == 0);
	CHECK(close(target) == 0);

	/* Actual redirection through fd 1, then restore the supervisor's capture. */
	source = open("dup2-redirected", O_CREAT | O_EXCL | O_RDWR, 0600);
	CHECK(source > STDERR_FILENO);
	saved_stdout = dup(STDOUT_FILENO);
	CHECK(saved_stdout > STDERR_FILENO);
	CHECK(duplicate_to(source, STDOUT_FILENO) == STDOUT_FILENO);
	CHECK(write(STDOUT_FILENO, redirected, sizeof(redirected) - 1) == (ssize_t)(sizeof(redirected) - 1));
	CHECK(duplicate_to(saved_stdout, STDOUT_FILENO) == STDOUT_FILENO);
	CHECK(close(saved_stdout) == 0);
	CHECK(lseek(source, 0, SEEK_SET) == 0);
	CHECK(read(source, data, sizeof(data)) == (ssize_t)(sizeof(redirected) - 1));
	CHECK(memcmp(data, redirected, sizeof(redirected) - 1) == 0);
	CHECK(close(source) == 0);
	CHECK(write(STDOUT_FILENO, passed, sizeof(passed) - 1) == (ssize_t)(sizeof(passed) - 1));
	return 0;
}
