#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

size_t scarlet_test_strftime(char *, size_t, const char *, const struct tm *);

int main(void)
{
	static const char *formats[] = {
		"%a %A %b %B %c %C %d %D %e %F %g %G %h %H %I %j",
		"%m %M %p %r %R %S %t %T %u %U %V %w %W %x %X %y %Y %z %Z %%",
		"%Y-%m-%dT%H:%M:%S%z", "日本語 %F", "", "%F %% %T"
	};
	unsigned long checked = 0;
	setenv("TZ", "UTC", 1);
	tzset();
	/* Includes century/leap-year and ISO week-year boundaries. */
	for (time_t stamp = -2208988800LL; stamp < 4133980800LL; stamp += 86400) {
		struct tm tm;
		gmtime_r(&stamp, &tm);
		tm.tm_hour = 17;
		tm.tm_min = 23;
		tm.tm_sec = 59;
		for (size_t i = 0; i < sizeof(formats) / sizeof(*formats); i++) {
			char expected[512], actual[512];
			size_t n = strftime(expected, sizeof(expected), formats[i], &tm);
			size_t result = scarlet_test_strftime(actual, sizeof(actual), formats[i], &tm);
			if (n != result || strcmp(expected, actual)) {
				fprintf(stderr, "%lld %s\nexpected: %s\nactual:   %s\n",
					(long long)stamp, formats[i], expected, actual);
				return 1;
			}
			if (scarlet_test_strftime(actual, n, formats[i], &tm) != 0 ||
			    scarlet_test_strftime(actual, n + 1, formats[i], &tm) != n)
				return 2;
			checked++;
		}
	}
	printf("strftime: %lu comparisons passed\n", checked);
	return 0;
}
