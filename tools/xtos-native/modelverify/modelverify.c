#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "xtos_ed25519.h"

#define MODELVERIFY_KEY_BYTES 32U
#define MODELVERIFY_SIGNATURE_BYTES 64U
#define MODELVERIFY_MESSAGE_MAX 65536U

enum modelverify_result {
    MODELVERIFY_VALID = 0,
    MODELVERIFY_INVALID_SIGNATURE = 1,
    MODELVERIFY_BAD_INPUT = 2,
};

static int open_regular_nofollow(const char *path, int *fd_out) {
    int fd = open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW | O_NONBLOCK);
    if (fd < 0) return -1;

    struct stat st;
    if (fstat(fd, &st) < 0 || !S_ISREG(st.st_mode)) {
        (void)close(fd);
        return -1;
    }
    *fd_out = fd;
    return 0;
}

static int read_exact_file(const char *path, uint8_t *out, size_t size) {
    int fd;
    if (open_regular_nofollow(path, &fd) < 0) return -1;

    size_t offset = 0;
    while (offset < size) {
        ssize_t got = read(fd, out + offset, size - offset);
        if (got > 0) {
            offset += (size_t)got;
            continue;
        }
        if (got < 0 && errno == EINTR) continue;
        (void)close(fd);
        return -1;
    }

    uint8_t extra;
    ssize_t got;
    do {
        got = read(fd, &extra, sizeof(extra));
    } while (got < 0 && errno == EINTR);
    (void)close(fd);
    return got == 0 ? 0 : -1;
}

static int read_message_file(const char *path,
                             uint8_t message[MODELVERIFY_MESSAGE_MAX],
                             size_t *message_size) {
    int fd;
    if (open_regular_nofollow(path, &fd) < 0) return -1;

    size_t size = 0;
    for (;;) {
        if (size == MODELVERIFY_MESSAGE_MAX) {
            uint8_t extra;
            ssize_t got;
            do {
                got = read(fd, &extra, sizeof(extra));
            } while (got < 0 && errno == EINTR);
            (void)close(fd);
            if (got != 0) return -1;
            *message_size = size;
            return 0;
        }

        ssize_t got = read(fd, message + size, MODELVERIFY_MESSAGE_MAX - size);
        if (got > 0) {
            size += (size_t)got;
            continue;
        }
        if (got < 0 && errno == EINTR) continue;
        (void)close(fd);
        if (got == 0 && size > 0) {
            *message_size = size;
            return 0;
        }
        return -1;
    }
}

static int verify_files(const char *pubkey_path, const char *message_path,
                        const char *signature_path) {
    uint8_t public_key[MODELVERIFY_KEY_BYTES];
    uint8_t signature[MODELVERIFY_SIGNATURE_BYTES];
    uint8_t message[MODELVERIFY_MESSAGE_MAX];
    size_t message_size;

    if (read_exact_file(pubkey_path, public_key, sizeof(public_key)) < 0 ||
        read_exact_file(signature_path, signature, sizeof(signature)) < 0 ||
        read_message_file(message_path, message, &message_size) < 0) {
        return MODELVERIFY_BAD_INPUT;
    }

    return crypto_ed25519_check(signature, public_key, message, message_size) == 0
               ? MODELVERIFY_VALID
               : MODELVERIFY_INVALID_SIGNATURE;
}

int main(int argc, char **argv) {
    if (argc != 7 || argv[1] == NULL || argv[2] == NULL ||
        argv[3] == NULL || argv[4] == NULL || argv[5] == NULL ||
        argv[6] == NULL || strcmp(argv[1], "--pubkey") != 0 ||
        strcmp(argv[3], "--message") != 0 ||
        strcmp(argv[5], "--signature") != 0) {
        return MODELVERIFY_BAD_INPUT;
    }
    return verify_files(argv[2], argv[4], argv[6]);
}
