#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <fcntl.h>
#include <math.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#include "xtos/bci.h"

static void write_samples(const char *path)
{
    int fd = open(path, O_WRONLY);
    int32_t samples[] = {1250, -2500, 0};
    assert(fd >= 0);
    assert(write(fd, samples, sizeof(samples)) == (ssize_t)sizeof(samples));
    close(fd);
}

int main(void)
{
    char path[] = "/tmp/xtos-bci-samples-XXXXXX";
    int seed = mkstemp(path);
    xtos_bci_session_t *session;
    float samples[3] = {0};
    pid_t writer;
    ssize_t count;
    int status;

    assert(seed >= 0);
    close(seed);
    unlink(path);
    assert(mkfifo(path, 0600) == 0);

    writer = fork();
    assert(writer >= 0);
    if (writer == 0) {
        write_samples(path);
        _exit(0);
    }

    session = xtos_bci_session_create(path);
    assert(session != NULL);
    count = xtos_bci_read_samples(session, samples, 3);
    assert(count == 3);
    assert(fabsf(samples[0] - 1.25f) < 0.0001f);
    assert(fabsf(samples[1] + 2.5f) < 0.0001f);
    assert(fabsf(samples[2]) < 0.0001f);
    xtos_bci_session_destroy(session);

    assert(waitpid(writer, &status, 0) == writer);
    assert(WIFEXITED(status) && WEXITSTATUS(status) == 0);
    unlink(path);
    puts("BCI sample conversion: PASS");
    return 0;
}
