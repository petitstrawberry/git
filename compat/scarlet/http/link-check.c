#include <stdio.h>
#include <string.h>
#include <sys/random.h>
#include <time.h>
#include <curl/curl.h>

static size_t count_body(char *data, size_t size, size_t count, void *context)
{
    size_t bytes = size * count;
    (void)data;
    *(size_t *)context += bytes;
    return bytes;
}

/* This also exercises the ordinary C API without curl-rust's Easy wrapper. */
int main(int argc, char **argv)
{
    CURL *easy;
    CURLM *multi;
    const curl_version_info_data *info;
    CURLcode result;
    size_t bytes = 0;
    long status = 0;
    unsigned char entropy[32];
    char error[CURL_ERROR_SIZE] = {0};
    FILE *report = NULL;
    time_t now = time(NULL);
    if (now == (time_t)-1 || getrandom(entropy, sizeof(entropy), 0) != sizeof(entropy)) {
	fprintf(stderr, "Scarlet clock or required entropy source unavailable\n");
	return 4;
    }
    printf("SCARLET_HTTP_PREREQUISITES epoch=%lld entropy=PASS\n", (long long)now);
    if (curl_global_init(CURL_GLOBAL_ALL))
	return 1;
    info = curl_version_info(CURLVERSION_NOW);
    if (!info || !(info->features & CURL_VERSION_SSL) || !info->ssl_version ||
	!strstr(info->ssl_version, "rustls"))
	return 2;
    printf("Scarlet libcurl %s %s\n", info->version, info->ssl_version);
    easy = curl_easy_init();
    multi = curl_multi_init();
    if (!easy || !multi)
	return 3;
    if (argc > 1) {
	report = fopen("/tmp/curl-link-check.result", "w");
	if (!report)
	    return 5;
	fprintf(report, "epoch=%lld entropy=PASS version=%s backend=%s\n",
		(long long)now, info->version, info->ssl_version);
	curl_easy_setopt(easy, CURLOPT_URL, argv[1]);
	curl_easy_setopt(easy, CURLOPT_PROTOCOLS_STR, "https");
	curl_easy_setopt(easy, CURLOPT_SSL_VERIFYPEER, 1L);
	curl_easy_setopt(easy, CURLOPT_SSL_VERIFYHOST, 2L);
	curl_easy_setopt(easy, CURLOPT_ERRORBUFFER, error);
	if (argc > 2)
	    curl_easy_setopt(easy, CURLOPT_CAINFO, argv[2]);
	curl_easy_setopt(easy, CURLOPT_FAILONERROR, 1L);
	curl_easy_setopt(easy, CURLOPT_TIMEOUT, 30L);
	curl_easy_setopt(easy, CURLOPT_WRITEFUNCTION, count_body);
	curl_easy_setopt(easy, CURLOPT_WRITEDATA, &bytes);
	result = curl_easy_perform(easy);
	curl_easy_getinfo(easy, CURLINFO_RESPONSE_CODE, &status);
	printf("SCARLET_HTTP_TRANSFER result=%d http=%ld bytes=%zu detail=%s\n",
	       (int)result, status, bytes, error);
	fprintf(report, "result=%d http=%ld bytes=%zu detail=%s\n",
		(int)result, status, bytes, error);
	if (fclose(report))
	    return 6;
	if (result) {
	    fprintf(stderr, "libcurl: %s\n", curl_easy_strerror(result));
	    return (int)result;
	}
	if (status != 200 || bytes == 0)
	    return 7;
	printf("HTTP %ld, %zu bytes\n", status, bytes);
    }
    curl_multi_cleanup(multi);
    curl_easy_cleanup(easy);
    curl_global_cleanup();
    return 0;
}
