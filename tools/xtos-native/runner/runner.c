#define _GNU_SOURCE
#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <grp.h>
#include <limits.h>
#include <poll.h>
#include <signal.h>
#include <sched.h>
#include <stddef.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/prctl.h>
#include <sys/resource.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <linux/audit.h>
#include <linux/filter.h>
#include <linux/landlock.h>
#include <linux/openat2.h>
#include <linux/seccomp.h>
#include <sys/syscall.h>

#if defined(__x86_64__)
#define XTOS_AUDIT_ARCH AUDIT_ARCH_X86_64
#elif defined(__aarch64__)
#define XTOS_AUDIT_ARCH AUDIT_ARCH_AARCH64
#else
#error "xtos runner supports only x86_64 and AArch64"
#endif

#define OUTPUT_CAPACITY (1024U * 1024U)
#define FILTER_CAPACITY 256U
#define DEFAULT_TIMEOUT_MS 1000U
#define MAX_TIMEOUT_MS 86400000U
#define MAX_WRITE_DIRS 16U

/* A production build must select a dedicated account explicitly. Runtime
 * identity selection exists only in the locally compiled test fixture. */
#ifndef XTOS_RUNNER_TEST_MODE
#define XTOS_RUNNER_TEST_MODE 0
#endif
#ifndef XTOS_RUNNER_UID
#define XTOS_RUNNER_UID 0U
#endif
#ifndef XTOS_RUNNER_GID
#define XTOS_RUNNER_GID 0U
#endif

enum child_stage {
    STAGE_LIMITS = 1,
    STAGE_NO_NEW_PRIVS,
    STAGE_GROUPS,
    STAGE_GID,
    STAGE_UID,
    STAGE_IDENTITY_READBACK,
    STAGE_CWD,
    STAGE_ROOT,
    STAGE_FILESYSTEM_POLICY,
    STAGE_NAMESPACE,
    STAGE_PROC_MOUNT,
    STAGE_SECCOMP,
    STAGE_SECCOMP_READBACK,
    STAGE_EXEC
};

struct child_error {
    int stage;
    int error_number;
};

struct output_buffer {
    unsigned char data[OUTPUT_CAPACITY];
    size_t length;
    bool truncated;
    int fd;
};

struct config {
    const char *root_arg;
    const char *cwd_arg;
    const char *write_dirs[MAX_WRITE_DIRS];
    size_t write_dir_count;
    uid_t uid;
    gid_t gid;
    struct rlimit cpu;
    struct rlimit address_space;
    struct rlimit nofile;
    struct rlimit nproc;
    unsigned timeout_ms;
    bool have_uid;
    bool have_gid;
    bool clear_groups;
    bool deny_socket;
    bool deny_getpid;
    bool namespace_isolation;
    int command_index;
};

static void json_string(FILE *stream, const unsigned char *text, size_t length)
{
    size_t i;

    fputc('"', stream);
    for (i = 0; i < length; i++) {
        unsigned char c = text[i];
        switch (c) {
        case '"': fputs("\\\"", stream); break;
        case '\\': fputs("\\\\", stream); break;
        case '\b': fputs("\\b", stream); break;
        case '\f': fputs("\\f", stream); break;
        case '\n': fputs("\\n", stream); break;
        case '\r': fputs("\\r", stream); break;
        case '\t': fputs("\\t", stream); break;
        default:
            if (c < 0x20 || c >= 0x7f) {
                fprintf(stream, "\\u%04x", (unsigned int)c);
            } else {
                fputc(c, stream);
            }
            break;
        }
    }
    fputc('"', stream);
}

static void emit_cli_error(const char *message)
{
    fputs("{\"status\":\"error\",\"error\":", stdout);
    json_string(stdout, (const unsigned char *)message, strlen(message));
    fputs("}\n", stdout);
}

static int parse_unsigned(const char *text, unsigned long long maximum,
                          unsigned long long *value)
{
    char *end = NULL;
    unsigned long long parsed;

    if (text == NULL || text[0] == '\0' || text[0] == '-')
        return -1;
    errno = 0;
    parsed = strtoull(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || parsed > maximum)
        return -1;
    *value = parsed;
    return 0;
}

static int parse_limit(const char *text, rlim_t *value)
{
    unsigned long long parsed;
    if (parse_unsigned(text, (unsigned long long)RLIM_INFINITY - 1ULL,
                       &parsed) != 0 || parsed == 0)
        return -1;
    *value = (rlim_t)parsed;
    return 0;
}

static void usage(FILE *stream)
{
    fputs("usage: xtos-runner --root DIR --cwd DIR --uid UID --gid GID "
          "--supp-groups-clear --cpu-sec N --as-bytes N --nofile N --nproc N "
          "[--write-dir /DIR] [--timeout-ms N] [--deny-socket] [--deny-getpid] "
          "[--namespace-isolation] -- COMMAND [ARG... ]\n"
          "namespace-isolation: privileged supervisor, permanent UID/GID drop, no user namespace; "
          "requires a root-owned empty /proc directory with no write bits; network APIs denied\n",
          stream);
}

static int parse_options(int argc, char **argv, struct config *config)
{
    int i = 1;
    unsigned long long parsed;

    memset(config, 0, sizeof(*config));
    config->timeout_ms = DEFAULT_TIMEOUT_MS;
#if !XTOS_RUNNER_TEST_MODE
    config->uid = (uid_t)XTOS_RUNNER_UID;
    config->gid = (gid_t)XTOS_RUNNER_GID;
    config->have_uid = config->uid != 0 && config->uid != (uid_t)-1;
    config->have_gid = config->gid != 0 && config->gid != (gid_t)-1;
#endif
    while (i < argc) {
        const char *option = argv[i++];
        if (strcmp(option, "--") == 0) {
            config->command_index = i;
            break;
        }
        if (strcmp(option, "--supp-groups-clear") == 0) {
            config->clear_groups = true;
            continue;
        }
        if (strcmp(option, "--deny-socket") == 0) {
            config->deny_socket = true;
            continue;
        }
        if (strcmp(option, "--deny-getpid") == 0) {
            config->deny_getpid = true;
            continue;
        }
        if (strcmp(option, "--namespace-isolation") == 0) {
            config->namespace_isolation = true;
            continue;
        }
        if (i >= argc)
            return -1;
        if (strcmp(option, "--root") == 0) {
            config->root_arg = argv[i++];
        } else if (strcmp(option, "--cwd") == 0) {
            config->cwd_arg = argv[i++];
        } else if (strcmp(option, "--write-dir") == 0) {
            if (config->write_dir_count >= MAX_WRITE_DIRS)
                return -1;
            config->write_dirs[config->write_dir_count++] = argv[i++];
        } else if (strcmp(option, "--uid") == 0) {
            if (!XTOS_RUNNER_TEST_MODE)
                return -1;
            if (parse_unsigned(argv[i++], (unsigned long long)(uid_t)-1,
                               &parsed) != 0 || parsed == 0 || parsed == (uid_t)-1)
                return -1;
            config->uid = (uid_t)parsed;
            config->have_uid = true;
        } else if (strcmp(option, "--gid") == 0) {
            if (!XTOS_RUNNER_TEST_MODE)
                return -1;
            if (parse_unsigned(argv[i++], (unsigned long long)(gid_t)-1,
                               &parsed) != 0 || parsed == 0 || parsed == (gid_t)-1)
                return -1;
            config->gid = (gid_t)parsed;
            config->have_gid = true;
        } else if (strcmp(option, "--cpu-sec") == 0) {
            if (parse_limit(argv[i++], &config->cpu.rlim_cur) != 0)
                return -1;
            config->cpu.rlim_max = config->cpu.rlim_cur;
        } else if (strcmp(option, "--as-bytes") == 0) {
            if (parse_limit(argv[i++], &config->address_space.rlim_cur) != 0)
                return -1;
            config->address_space.rlim_max = config->address_space.rlim_cur;
        } else if (strcmp(option, "--nofile") == 0) {
            if (parse_limit(argv[i++], &config->nofile.rlim_cur) != 0)
                return -1;
            config->nofile.rlim_max = config->nofile.rlim_cur;
        } else if (strcmp(option, "--nproc") == 0) {
            if (parse_limit(argv[i++], &config->nproc.rlim_cur) != 0)
                return -1;
            config->nproc.rlim_max = config->nproc.rlim_cur;
        } else if (strcmp(option, "--timeout-ms") == 0) {
            if (parse_unsigned(argv[i++], MAX_TIMEOUT_MS, &parsed) != 0 || parsed == 0)
                return -1;
            config->timeout_ms = (unsigned)parsed;
        } else {
            return -1;
        }
    }
    if (config->root_arg == NULL || config->cwd_arg == NULL ||
        !config->have_uid || !config->have_gid || !config->clear_groups ||
        config->cpu.rlim_cur == 0 || config->address_space.rlim_cur == 0 ||
        config->nofile.rlim_cur == 0 || config->nproc.rlim_cur == 0 ||
        config->command_index == 0 || config->command_index >= argc ||
        config->uid == 0 || config->gid == 0)
        return -1;
    return 0;
}

static int path_is_under(const char *root, const char *candidate)
{
    size_t root_length = strlen(root);
    if (strcmp(root, candidate) == 0)
        return 1;
    if (root_length == 1 && root[0] == '/')
        return candidate[0] == '/';
    return strncmp(root, candidate, root_length) == 0 &&
           candidate[root_length] == '/';
}

static int canonical_paths(const struct config *config, char root[PATH_MAX],
                           char cwd[PATH_MAX], char *error, size_t error_size)
{
    struct stat root_stat;
    struct stat cwd_stat;

    if (realpath(config->root_arg, root) == NULL) {
        (void)snprintf(error, error_size, "cannot resolve root directory: %s",
                       strerror(errno));
        return -1;
    }
    if (realpath(config->cwd_arg, cwd) == NULL) {
        (void)snprintf(error, error_size, "cannot resolve cwd directory: %s",
                       strerror(errno));
        return -1;
    }
    if (stat(root, &root_stat) != 0 || !S_ISDIR(root_stat.st_mode)) {
        (void)snprintf(error, error_size, "root is not a directory");
        return -1;
    }
    if (stat(cwd, &cwd_stat) != 0 || !S_ISDIR(cwd_stat.st_mode)) {
        (void)snprintf(error, error_size, "cwd is not a directory");
        return -1;
    }
    if (!path_is_under(root, cwd)) {
        (void)snprintf(error, error_size, "cwd is outside the root allowlist");
        return -1;
    }
    return 0;
}

static int add_allow(struct sock_filter *filter, size_t *count, int syscall_number)
{
    if (*count + 2 >= FILTER_CAPACITY)
        return -1;
    filter[(*count)++] = (struct sock_filter)BPF_JUMP(
        BPF_JMP | BPF_JEQ | BPF_K, (unsigned int)syscall_number, 0, 1);
    filter[(*count)++] = (struct sock_filter)BPF_STMT(
        BPF_RET | BPF_K, SECCOMP_RET_ALLOW);
    return 0;
}

static int add_deny(struct sock_filter *filter, size_t *count, int syscall_number)
{
    if (*count + 2 >= FILTER_CAPACITY)
        return -1;
    filter[(*count)++] = (struct sock_filter)BPF_JUMP(
        BPF_JMP | BPF_JEQ | BPF_K, (unsigned int)syscall_number, 0, 1);
    filter[(*count)++] = (struct sock_filter)BPF_STMT(
        BPF_RET | BPF_K, SECCOMP_RET_ERRNO | (EPERM & SECCOMP_RET_DATA));
    return 0;
}

#define ADD_ALLOW(name) do { \
    if (add_allow(filter, &count, __NR_##name) != 0) return -1; \
} while (0)

static int install_seccomp(const struct config *config)
{
    struct sock_filter filter[FILTER_CAPACITY];
    struct sock_fprog program;
    size_t count = 0;
    int denied[4];
    size_t denied_count = 0;

    filter[count++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
                                                    offsetof(struct seccomp_data, arch));
    filter[count++] = (struct sock_filter)BPF_JUMP(BPF_JMP | BPF_JEQ | BPF_K,
                                                   XTOS_AUDIT_ARCH, 1, 0);
    filter[count++] = (struct sock_filter)BPF_STMT(BPF_RET | BPF_K,
                                                   SECCOMP_RET_KILL_PROCESS);
    filter[count++] = (struct sock_filter)BPF_STMT(BPF_LD | BPF_W | BPF_ABS,
                                                    offsetof(struct seccomp_data, nr));
#ifdef __NR_socket
    if (config->deny_socket || config->namespace_isolation)
        denied[denied_count++] = __NR_socket;
#else
    if (config->deny_socket || config->namespace_isolation)
        return -1;
#endif
#ifdef __NR_socketpair
    if (config->namespace_isolation)
        denied[denied_count++] = __NR_socketpair;
#else
    if (config->namespace_isolation)
        return -1;
#endif
#ifdef __NR_connect
    if (config->namespace_isolation)
        denied[denied_count++] = __NR_connect;
#else
    if (config->namespace_isolation)
        return -1;
#endif
#ifdef __NR_getpid
    if (config->deny_getpid)
        denied[denied_count++] = __NR_getpid;
#else
    if (config->deny_getpid)
        return -1;
#endif
    for (size_t i = 0; i < denied_count; i++) {
        if (add_deny(filter, &count, denied[i]) != 0)
            return -1;
    }

    ADD_ALLOW(read);
    ADD_ALLOW(write);
#ifdef __NR_readv
    ADD_ALLOW(readv);
#endif
#ifdef __NR_writev
    ADD_ALLOW(writev);
#endif
#ifdef __NR_pread64
    ADD_ALLOW(pread64);
#endif
#ifdef __NR_pwrite64
    ADD_ALLOW(pwrite64);
#endif
#ifdef __NR_ftruncate
    ADD_ALLOW(ftruncate);
#endif
#ifdef __NR_truncate
    ADD_ALLOW(truncate);
#endif
    ADD_ALLOW(close);
    ADD_ALLOW(getcwd);
    ADD_ALLOW(lseek);
#ifdef __NR_fstat
    ADD_ALLOW(fstat);
#endif
#ifdef __NR_newfstatat
    ADD_ALLOW(newfstatat);
#endif
#ifdef __NR_statx
    ADD_ALLOW(statx);
#endif
#ifdef __NR_openat
    ADD_ALLOW(openat);
#endif
    /* Landlock restricts these ordinary mutations to configured write dirs. */
#ifdef __NR_mkdir
    ADD_ALLOW(mkdir);
#endif
#ifdef __NR_mkdirat
    ADD_ALLOW(mkdirat);
#endif
#ifdef __NR_unlink
    ADD_ALLOW(unlink);
#endif
#ifdef __NR_unlinkat
    ADD_ALLOW(unlinkat);
#endif
#ifdef __NR_rmdir
    ADD_ALLOW(rmdir);
#endif
#ifdef __NR_access
    ADD_ALLOW(access);
#endif
#ifdef __NR_faccessat
    ADD_ALLOW(faccessat);
#endif
#ifdef __NR_faccessat2
    ADD_ALLOW(faccessat2);
#endif
#ifdef __NR_readlink
    ADD_ALLOW(readlink);
#endif
#ifdef __NR_readlinkat
    ADD_ALLOW(readlinkat);
#endif
    ADD_ALLOW(mmap);
    ADD_ALLOW(mprotect);
    ADD_ALLOW(munmap);
#ifdef __NR_mremap
    ADD_ALLOW(mremap);
#endif
    ADD_ALLOW(brk);
#ifdef __NR_madvise
    ADD_ALLOW(madvise);
#endif
#ifdef __NR_arch_prctl
    ADD_ALLOW(arch_prctl);
#endif
    ADD_ALLOW(rt_sigaction);
    ADD_ALLOW(rt_sigprocmask);
    ADD_ALLOW(rt_sigreturn);
#ifdef __NR_sigaltstack
    ADD_ALLOW(sigaltstack);
#endif
    ADD_ALLOW(ioctl);
    ADD_ALLOW(fcntl);
    ADD_ALLOW(dup);
#ifdef __NR_dup2
    ADD_ALLOW(dup2);
#endif
#ifdef __NR_dup3
    ADD_ALLOW(dup3);
#endif
    ADD_ALLOW(poll);
#ifdef __NR_ppoll
    ADD_ALLOW(ppoll);
#endif
#ifdef __NR_select
    ADD_ALLOW(select);
#endif
    ADD_ALLOW(nanosleep);
#ifdef __NR_clock_nanosleep
    ADD_ALLOW(clock_nanosleep);
#endif
    ADD_ALLOW(clock_gettime);
#ifdef __NR_gettimeofday
    ADD_ALLOW(gettimeofday);
#endif
    ADD_ALLOW(futex);
#ifdef __NR_futex_waitv
    ADD_ALLOW(futex_waitv);
#endif
    ADD_ALLOW(set_tid_address);
#ifdef __NR_set_robust_list
    ADD_ALLOW(set_robust_list);
#endif
#ifdef __NR_rseq
    ADD_ALLOW(rseq);
#endif
    ADD_ALLOW(prlimit64);
#ifdef __NR_getrlimit
    ADD_ALLOW(getrlimit);
#endif
    ADD_ALLOW(uname);
    ADD_ALLOW(getpid);
#ifdef __NR_getppid
    ADD_ALLOW(getppid);
#endif
    ADD_ALLOW(getuid);
    ADD_ALLOW(geteuid);
    ADD_ALLOW(getgid);
    ADD_ALLOW(getegid);
#ifdef __NR_getgroups
    ADD_ALLOW(getgroups);
#endif
    ADD_ALLOW(sched_yield);
    ADD_ALLOW(getrandom);
    ADD_ALLOW(execve);
#ifdef __NR_execveat
    ADD_ALLOW(execveat);
#endif
    ADD_ALLOW(prctl);
    ADD_ALLOW(exit);
    ADD_ALLOW(exit_group);

    if (count + 1 >= FILTER_CAPACITY)
        return -1;
    filter[count++] = (struct sock_filter)BPF_STMT(
        BPF_RET | BPF_K, SECCOMP_RET_ERRNO | (EPERM & SECCOMP_RET_DATA));
    program.len = (unsigned short)count;
    program.filter = filter;
#if XTOS_RUNNER_TEST_MODE && defined(XTOS_RUNNER_TEST_FAIL_SECCOMP)
    /* Exercise a real kernel EINVAL, without installing a test-only policy. */
    program.len = 0;
#endif
    if (prctl(PR_SET_SECCOMP, SECCOMP_MODE_FILTER, &program) != 0)
        return -1;
    return 0;
}

#undef ADD_ALLOW

static void child_fail(int error_fd, enum child_stage stage, int error_number)
{
    struct child_error error = { (int)stage, error_number };
    const unsigned char *bytes = (const unsigned char *)&error;
    size_t remaining = sizeof(error);
    while (remaining > 0) {
        ssize_t written = write(error_fd, bytes, remaining);
        if (written > 0) {
            bytes += (size_t)written;
            remaining -= (size_t)written;
        } else if (written < 0 && errno == EINTR) {
            continue;
        } else {
            break;
        }
    }
    _exit(125);
}

static int same_limit(const struct rlimit *actual, const struct rlimit *expected)
{
    return actual->rlim_cur == expected->rlim_cur &&
           actual->rlim_max == expected->rlim_max;
}

static int close_fd_checked(int fd)
{
    /* Linux close() reports the release state synchronously and must never be
     * retried: after EINTR the descriptor is already released, and the number
     * could have been reused by any concurrent allocator. Treat EBADF as
     * already closed; report every other error so the caller fails closed
     * instead of re-closing a possibly recycled descriptor. */
    if (close(fd) == 0 || errno == EBADF)
        return 0;
    return errno;
}

static int close_inherited_fds(int error_fd, int extra_fd_a, int extra_fd_b)
{
    /* Enumerate actual descriptors, including those above the new nofile limit. */
    DIR *directory = opendir("/proc/self/fd");
    int directory_fd;
    int failure = 0;

    if (directory == NULL)
        return errno != 0 ? errno : EIO;
    directory_fd = dirfd(directory);
    if (directory_fd < 0) {
        failure = errno != 0 ? errno : EIO;
        (void)closedir(directory);
        return failure;
    }

    for (;;) {
        struct dirent *entry;
        char *end = NULL;
        long parsed_fd;

        errno = 0;
        entry = readdir(directory);
        if (entry == NULL) {
            if (errno != 0)
                failure = errno;
            break;
        }
        if (entry->d_name[0] == '.' &&
            (entry->d_name[1] == '\0' ||
             (entry->d_name[1] == '.' && entry->d_name[2] == '\0')))
            continue;

        errno = 0;
        parsed_fd = strtol(entry->d_name, &end, 10);
        if (errno != 0 || end == entry->d_name || *end != '\0' ||
            parsed_fd < 0 || parsed_fd > INT_MAX) {
            failure = errno != 0 ? errno : EIO;
            break;
        }
        if (parsed_fd <= STDERR_FILENO || parsed_fd == error_fd ||
            parsed_fd == extra_fd_a || parsed_fd == extra_fd_b ||
            parsed_fd == directory_fd)
            continue;
        failure = close_fd_checked((int)parsed_fd);
        if (failure != 0)
            break;
    }

    if (closedir(directory) != 0 && failure == 0)
        failure = errno != 0 ? errno : EIO;
    return failure;
}

static int install_filesystem_policy(const struct config *config)
{
    /* ABI 3 is mandatory: older ABIs cannot deny O_TRUNC/ftruncate. There is
     * deliberately no permissive fallback if the LSM is absent or disabled. */
    int abi = (int)syscall(SYS_landlock_create_ruleset, NULL, 0,
                          LANDLOCK_CREATE_RULESET_VERSION);
    if (abi < 0)
        return errno != 0 ? errno : EIO;
    if (abi < 3)
        return EOPNOTSUPP;
    const uint64_t writable = LANDLOCK_ACCESS_FS_WRITE_FILE |
        LANDLOCK_ACCESS_FS_REMOVE_DIR | LANDLOCK_ACCESS_FS_REMOVE_FILE |
        LANDLOCK_ACCESS_FS_MAKE_DIR | LANDLOCK_ACCESS_FS_MAKE_REG |
        LANDLOCK_ACCESS_FS_TRUNCATE;
    struct landlock_ruleset_attr attr = {0};
    attr.handled_access_fs = writable | LANDLOCK_ACCESS_FS_MAKE_CHAR |
        LANDLOCK_ACCESS_FS_MAKE_SOCK | LANDLOCK_ACCESS_FS_MAKE_FIFO |
        LANDLOCK_ACCESS_FS_MAKE_BLOCK | LANDLOCK_ACCESS_FS_MAKE_SYM |
        LANDLOCK_ACCESS_FS_REFER;
    int ruleset = (int)syscall(SYS_landlock_create_ruleset, &attr,
                              sizeof(attr.handled_access_fs), 0);
    if (ruleset < 0)
        return errno != 0 ? errno : EIO;
    int root_fd = open("/", O_PATH | O_DIRECTORY | O_CLOEXEC);
    int failure = root_fd < 0 ? (errno != 0 ? errno : EIO) : 0;
    for (size_t i = 0; failure == 0 && i < config->write_dir_count; i++) {
        const char *path = config->write_dirs[i];
        struct open_how how = {
            .flags = O_PATH | O_DIRECTORY | O_CLOEXEC,
            .resolve = RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS
        };
        /* Rules name absolute paths inside the new root, never host paths.
         * Root-wide grants and symlink redirects are intentionally refused. */
        if (path[0] != '/' || path[1] == '\0' || path[1] == '/') {
            failure = EINVAL;
            break;
        }
        for (const char *part = path + 1; *part != '\0';) {
            size_t length = strcspn(part, "/");
            if (length == 0 || (length == 1 && part[0] == '.') ||
                (length == 2 && part[0] == '.' && part[1] == '.') ||
                (part[length] == '/' && part[length + 1] == '\0')) {
                failure = EINVAL;
                break;
            }
            part += length;
            if (*part == '/')
                part++;
        }
        if (failure != 0)
            break;
        int directory = (int)syscall(SYS_openat2, root_fd, path + 1,
                                    &how, sizeof(how));
        if (directory < 0) {
            failure = errno != 0 ? errno : EIO;
            break;
        }
        struct landlock_path_beneath_attr rule = {
            .allowed_access = writable,
            .parent_fd = directory
        };
        if (syscall(SYS_landlock_add_rule, ruleset,
                    LANDLOCK_RULE_PATH_BENEATH, &rule, 0) != 0)
            failure = errno != 0 ? errno : EIO;
        int close_error = close_fd_checked(directory);
        if (failure == 0)
            failure = close_error;
    }
    if (root_fd >= 0) {
        int close_error = close_fd_checked(root_fd);
        if (failure == 0)
            failure = close_error;
    }
    if (failure == 0 && syscall(SYS_landlock_restrict_self, ruleset, 0) != 0)
        failure = errno != 0 ? errno : EIO;
    int close_error = close_fd_checked(ruleset);
    return failure != 0 ? failure : close_error;
}

static volatile sig_atomic_t supervisor_stop;

static void supervisor_terminate(int signal_number)
{
    (void)signal_number;
    supervisor_stop = 1;
}

/* Intentionally privileged trusted setup supervisor; no user namespace.
 * Only the forked PID-1 target proceeds to chroot, permanent identity drop,
 * Landlock and seccomp. This process never execs caller-controlled code. */
static int namespace_supervise(pid_t runner_pid, int stdout_fd, int stderr_fd,
                               int error_fd)
{
    struct sigaction action = {0};
    action.sa_handler = supervisor_terminate;
    sigemptyset(&action.sa_mask);
    if (sigaction(SIGTERM, &action, NULL) != 0 ||
        prctl(PR_SET_PDEATHSIG, SIGTERM, 0, 0, 0) != 0 ||
        getppid() != runner_pid || setpgid(0, 0) != 0)
        child_fail(error_fd, STAGE_NAMESPACE, errno != 0 ? errno : EPIPE);
    int failure = close_inherited_fds(error_fd, stdout_fd, stderr_fd);
    if (failure != 0)
        child_fail(error_fd, STAGE_NAMESPACE, failure);
    if (unshare(CLONE_NEWNS | CLONE_NEWNET | CLONE_NEWPID) != 0)
        child_fail(error_fd, STAGE_NAMESPACE, errno);
    if (mount(NULL, "/", NULL, MS_REC | MS_PRIVATE, NULL) != 0)
        child_fail(error_fd, STAGE_NAMESPACE, errno);
    /* A fresh network namespace has only down loopback, no usable route.
     * Never enable an interface, configure a route, or fall back on failure. */
    int alive_pipe[2];
    if (pipe2(alive_pipe, O_CLOEXEC) != 0 || supervisor_stop)
        child_fail(error_fd, STAGE_NAMESPACE, errno != 0 ? errno : EPIPE);
    pid_t target = fork();
    if (target < 0)
        child_fail(error_fd, STAGE_NAMESPACE, errno);
    if (target == 0) {
        if (close_fd_checked(alive_pipe[1]) != 0 ||
            prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0)
            child_fail(error_fd, STAGE_NAMESPACE, errno != 0 ? errno : EIO);
        struct pollfd alive = {.fd = alive_pipe[0], .events = POLLIN | POLLHUP};
        int ready;
        do { ready = poll(&alive, 1, 0); } while (ready < 0 && errno == EINTR);
        if (ready != 0 || alive.revents != 0)
            child_fail(error_fd, STAGE_NAMESPACE, EPIPE);
        action.sa_handler = SIG_DFL;
        if (sigaction(SIGTERM, &action, NULL) != 0)
            child_fail(error_fd, STAGE_NAMESPACE, errno);
        return alive_pipe[0];
    }
    /* Release every output/error write end: only the target owns them now. */
    int close_error = close_fd_checked(stdout_fd);
    int second_error = close_fd_checked(stderr_fd);
    int third_error = close_fd_checked(error_fd);
    int fourth_error = close_fd_checked(alive_pipe[0]);
    if (close_error != 0 || second_error != 0 || third_error != 0 || fourth_error != 0)
        supervisor_stop = 1;
    int status = 0;
    for (;;) {
        if (supervisor_stop)
            (void)kill(target, SIGKILL);
        pid_t waited = waitpid(target, &status, WNOHANG);
        if (waited == target)
            break;
        if (waited < 0 && errno != EINTR) {
            (void)kill(target, SIGKILL);
            while (waitpid(target, &status, 0) < 0 && errno == EINTR) { }
            _exit(125);
        }
        (void)poll(NULL, 0, 10);
    }
    (void)close_fd_checked(alive_pipe[1]);
    if (WIFEXITED(status))
        _exit(WEXITSTATUS(status));
    if (WIFSIGNALED(status)) {
        int signal_number = WTERMSIG(status);
        action.sa_handler = SIG_DFL;
        (void)sigaction(signal_number, &action, NULL);
        sigset_t unblocked;
        sigemptyset(&unblocked);
        sigaddset(&unblocked, signal_number);
        (void)sigprocmask(SIG_UNBLOCK, &unblocked, NULL);
        (void)kill(getpid(), signal_number);
        _exit(128 + signal_number);
    }
    _exit(125);
}

static void child_main(const struct config *config, const char *root, const char *cwd,
                       int stdout_fd, int stderr_fd, int error_fd, int parent_alive_fd)
{
    struct rlimit actual;
    uid_t real_uid, effective_uid, saved_uid;
    gid_t real_gid, effective_gid, saved_gid;

    if (!config->namespace_isolation)
        (void)setpgid(0, 0);
    if (dup2(stdout_fd, STDOUT_FILENO) < 0 || dup2(stderr_fd, STDERR_FILENO) < 0)
        child_fail(error_fd, STAGE_CWD, errno);
    close(stdout_fd);
    close(stderr_fd);
    if (chdir(root) != 0)
        child_fail(error_fd, STAGE_CWD, errno);
    int null_fd = open("/dev/null", O_RDONLY);
    if (null_fd < 0 || dup2(null_fd, STDIN_FILENO) < 0)
        child_fail(error_fd, STAGE_CWD, errno);
    if (null_fd > STDERR_FILENO)
        close(null_fd);

    if (setrlimit(RLIMIT_CPU, &config->cpu) != 0 ||
        setrlimit(RLIMIT_AS, &config->address_space) != 0 ||
        setrlimit(RLIMIT_NOFILE, &config->nofile) != 0 ||
        setrlimit(RLIMIT_NPROC, &config->nproc) != 0)
        child_fail(error_fd, STAGE_LIMITS, errno);
    if (getrlimit(RLIMIT_CPU, &actual) != 0 || !same_limit(&actual, &config->cpu) ||
        getrlimit(RLIMIT_AS, &actual) != 0 || !same_limit(&actual, &config->address_space) ||
        getrlimit(RLIMIT_NOFILE, &actual) != 0 || !same_limit(&actual, &config->nofile) ||
        getrlimit(RLIMIT_NPROC, &actual) != 0 || !same_limit(&actual, &config->nproc))
        child_fail(error_fd, STAGE_LIMITS, errno != 0 ? errno : EIO);

    unsigned long no_new_privs_value = 1;
#if XTOS_RUNNER_TEST_MODE && defined(XTOS_RUNNER_TEST_FAIL_NO_NEW_PRIVS)
    /* Linux rejects zero for this operation; exec must never be attempted. */
    no_new_privs_value = 0;
#endif
    if (prctl(PR_SET_NO_NEW_PRIVS, no_new_privs_value, 0, 0, 0) != 0)
        child_fail(error_fd, STAGE_NO_NEW_PRIVS, errno);
    if (prctl(PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) != 1)
        child_fail(error_fd, STAGE_NO_NEW_PRIVS, errno != 0 ? errno : EIO);

    /* Enumerate proc before replacing the filesystem root. Drop every caller
     * descriptor except stdio and the private CLOEXEC setup-error pipe. */
    int descriptor_error = close_inherited_fds(error_fd, parent_alive_fd, -1);
    if (descriptor_error != 0)
        child_fail(error_fd, STAGE_EXEC, descriptor_error);

    /* chdir(root) pins the selected directory as cwd. chroot alone does not
     * move cwd; reset it before resolving the canonical root-relative cwd. */
    const char *inside_cwd = strcmp(root, "/") == 0 ? cwd : cwd + strlen(root);
    if (*inside_cwd == '\0')
        inside_cwd = "/";
    if (chroot(".") != 0 || chdir("/") != 0 || chdir(inside_cwd) != 0)
        child_fail(error_fd, STAGE_ROOT, errno);

    if (config->namespace_isolation) {
        int proc_fd = open("/proc", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        struct stat proc_stat;
        int proc_failure = 0;
        if (proc_fd < 0) {
            proc_failure = errno != 0 ? errno : EIO;
        } else if (fstat(proc_fd, &proc_stat) != 0) {
            proc_failure = errno != 0 ? errno : EIO;
        } else if (!S_ISDIR(proc_stat.st_mode) || proc_stat.st_uid != 0 ||
                   (proc_stat.st_mode & 0222) != 0) {
            proc_failure = EPERM;
        } else {
            int scan_fd = dup(proc_fd);
            DIR *directory = scan_fd >= 0 ? fdopendir(scan_fd) : NULL;
            if (directory == NULL) {
                proc_failure = errno != 0 ? errno : EIO;
                if (scan_fd >= 0)
                    (void)close_fd_checked(scan_fd);
            } else {
                for (;;) {
                    struct dirent *entry;
                    errno = 0;
                    entry = readdir(directory);
                    if (entry == NULL) {
                        if (errno != 0)
                            proc_failure = errno;
                        break;
                    }
                    if (strcmp(entry->d_name, ".") != 0 &&
                        strcmp(entry->d_name, "..") != 0) {
                        proc_failure = ENOTEMPTY;
                        break;
                    }
                }
                if (closedir(directory) != 0 && proc_failure == 0)
                    proc_failure = errno != 0 ? errno : EIO;
            }
        }
        /* Mount on the opened, verified directory, not a re-resolved pathname.
         * This runs only in the PID-namespace child and private mount namespace. */
        if (proc_failure == 0 && fchdir(proc_fd) != 0)
            proc_failure = errno != 0 ? errno : EIO;
        if (proc_failure == 0 &&
            mount("proc", ".", "proc", MS_NOSUID | MS_NODEV | MS_NOEXEC, NULL) != 0)
            proc_failure = errno != 0 ? errno : EIO;
        if (proc_failure == 0 && chdir(inside_cwd) != 0)
            proc_failure = errno != 0 ? errno : EIO;
        if (proc_fd >= 0) {
            int close_error = close_fd_checked(proc_fd);
            if (proc_failure == 0)
                proc_failure = close_error;
        }
        if (proc_failure != 0)
            child_fail(error_fd, STAGE_PROC_MOUNT, proc_failure);
    }

    int policy_error = install_filesystem_policy(config);
    if (policy_error != 0)
        child_fail(error_fd, STAGE_FILESYSTEM_POLICY, policy_error);

    if (setgroups(0, NULL) != 0)
        child_fail(error_fd, STAGE_GROUPS, errno);
    if (setresgid(config->gid, config->gid, config->gid) != 0)
        child_fail(error_fd, STAGE_GID, errno);
    if (setresuid(config->uid, config->uid, config->uid) != 0)
        child_fail(error_fd, STAGE_UID, errno);
    if (getresuid(&real_uid, &effective_uid, &saved_uid) != 0 ||
        getresgid(&real_gid, &effective_gid, &saved_gid) != 0 ||
        real_uid != config->uid || effective_uid != config->uid || saved_uid != config->uid ||
        real_gid != config->gid || effective_gid != config->gid || saved_gid != config->gid ||
        getgroups(0, NULL) != 0)
        child_fail(error_fd, STAGE_IDENTITY_READBACK, errno != 0 ? errno : EPERM);

    if (config->namespace_isolation) {
        /* Credential changes clear PDEATHSIG. Re-arm it after the permanent
         * drop, and use a pipe because getppid() is 0 for PID 1's outer parent. */
        if (prctl(PR_SET_PDEATHSIG, SIGKILL, 0, 0, 0) != 0)
            child_fail(error_fd, STAGE_NAMESPACE, errno);
        struct pollfd alive = {.fd = parent_alive_fd, .events = POLLIN | POLLHUP};
        int ready;
        do { ready = poll(&alive, 1, 0); } while (ready < 0 && errno == EINTR);
        if (ready != 0 || alive.revents != 0)
            child_fail(error_fd, STAGE_NAMESPACE, EPIPE);
        int close_error = close_fd_checked(parent_alive_fd);
        if (close_error != 0)
            child_fail(error_fd, STAGE_NAMESPACE, close_error);
    }

    if (install_seccomp(config) != 0)
        child_fail(error_fd, STAGE_SECCOMP, errno != 0 ? errno : EINVAL);
    if (prctl(PR_GET_SECCOMP, 0, 0, 0, 0) != SECCOMP_MODE_FILTER)
        child_fail(error_fd, STAGE_SECCOMP_READBACK, errno != 0 ? errno : EIO);

}

/* Kept separate so the fixed argv reaches execv unchanged. */
static void child_exec(const struct config *config, char **argv, int error_fd)
{
    execv(argv[config->command_index], &argv[config->command_index]);
    child_fail(error_fd, STAGE_EXEC, errno);
}

static long long elapsed_milliseconds(const struct timespec *start,
                                      const struct timespec *now)
{
    long long seconds = (long long)now->tv_sec - (long long)start->tv_sec;
    long long nanoseconds = (long long)now->tv_nsec - (long long)start->tv_nsec;
    return seconds * 1000LL + nanoseconds / 1000000LL;
}

static void close_fd(int *fd)
{
    if (*fd >= 0) {
        close(*fd);
        *fd = -1;
    }
}

static void drain_output(struct output_buffer *buffer)
{
    unsigned char scratch[4096];
    for (;;) {
        unsigned char *target = scratch;
        size_t capacity = sizeof(scratch);
        if (buffer->length < sizeof(buffer->data)) {
            target = buffer->data + buffer->length;
            capacity = sizeof(buffer->data) - buffer->length;
            if (capacity > sizeof(scratch))
                capacity = sizeof(scratch);
        }
        ssize_t count = read(buffer->fd, target, capacity);
        if (count > 0) {
            if (target != scratch)
                buffer->length += (size_t)count;
            else
                buffer->truncated = true;
            continue;
        }
        if (count == 0) {
            close_fd(&buffer->fd);
            return;
        }
        if (errno == EINTR)
            continue;
        if (errno == EAGAIN || errno == EWOULDBLOCK)
            return;
        close_fd(&buffer->fd);
        return;
    }
}

static void stage_name(int stage, char *buffer, size_t capacity)
{
    const char *name = "unknown";
    switch (stage) {
    case STAGE_LIMITS: name = "rlimit setup/readback"; break;
    case STAGE_NO_NEW_PRIVS: name = "no_new_privs setup/readback"; break;
    case STAGE_GROUPS: name = "supplementary group clear"; break;
    case STAGE_GID: name = "setgid"; break;
    case STAGE_UID: name = "setuid"; break;
    case STAGE_IDENTITY_READBACK: name = "identity readback"; break;
    case STAGE_CWD: name = "cwd setup"; break;
    case STAGE_ROOT: name = "filesystem root setup"; break;
    case STAGE_FILESYSTEM_POLICY: name = "filesystem write policy"; break;
    case STAGE_NAMESPACE: name = "namespace setup"; break;
    case STAGE_PROC_MOUNT: name = "private proc mount"; break;
    case STAGE_SECCOMP: name = "seccomp install"; break;
    case STAGE_SECCOMP_READBACK: name = "seccomp readback"; break;
    case STAGE_EXEC: name = "exec"; break;
    default: break;
    }
    (void)snprintf(buffer, capacity, "%s", name);
}

static int set_nonblocking(int fd)
{
    int flags = fcntl(fd, F_GETFL);
    if (flags < 0)
        return -1;
    return fcntl(fd, F_SETFL, flags | O_NONBLOCK);
}

static int emit_result(const struct config *config, struct output_buffer *out,
                       struct output_buffer *err, int child_status, bool reaped,
                       bool timed_out, const struct child_error *child_error,
                       size_t child_error_bytes, long long elapsed_ms)
{
    const char *status = "exit";
    int exit_code = -1;
    int signal_number = -1;
    char setup_message[256] = "";

    if (timed_out) {
        status = "timeout";
        exit_code = 124;
    } else if (child_error_bytes == sizeof(*child_error)) {
        char stage[80];
        stage_name(child_error->stage, stage, sizeof(stage));
        (void)snprintf(setup_message, sizeof(setup_message), "%s: %s", stage,
                       strerror(child_error->error_number));
        status = child_error->stage == STAGE_EXEC ? "exec_error" : "setup_error";
        exit_code = child_error->stage == STAGE_EXEC ? 127 : 125;
    } else if (reaped && WIFEXITED(child_status)) {
        exit_code = WEXITSTATUS(child_status);
    } else if (reaped && WIFSIGNALED(child_status)) {
        status = "signal";
        signal_number = WTERMSIG(child_status);
        exit_code = 128 + signal_number;
    } else {
        status = "wait_error";
        exit_code = 125;
    }

    printf("{\"status\":\"%s\",\"exit_code\":%d,\"signal\":", status, exit_code);
    if (signal_number >= 0)
        printf("%d", signal_number);
    else
        fputs("null", stdout);
    printf(",\"timed_out\":%s,\"elapsed_ms\":%lld,\"stdout\":",
           timed_out ? "true" : "false", elapsed_ms);
    json_string(stdout, out->data, out->length);
    fputs(",\"stderr\":", stdout);
    json_string(stdout, err->data, err->length);
    printf(",\"stdout_truncated\":%s,\"stderr_truncated\":%s,\"setup_error\":",
           out->truncated ? "true" : "false", err->truncated ? "true" : "false");
    if (setup_message[0] != '\0')
        json_string(stdout, (const unsigned char *)setup_message, strlen(setup_message));
    else
        fputs("null", stdout);
    printf(",\"limits\":{\"cpu_seconds\":%llu,\"address_space_bytes\":%llu,"
           "\"nofile\":%llu,\"nproc\":%llu}}\n",
           (unsigned long long)config->cpu.rlim_cur,
           (unsigned long long)config->address_space.rlim_cur,
           (unsigned long long)config->nofile.rlim_cur,
           (unsigned long long)config->nproc.rlim_cur);
    return exit_code;
}

int main(int argc, char **argv)
{
    struct config config;
    char root[PATH_MAX];
    char cwd[PATH_MAX];
    char path_error[256];
    int stdout_pipe[2] = {-1, -1};
    int stderr_pipe[2] = {-1, -1};
    int error_pipe[2] = {-1, -1};
    pid_t child;
    pid_t runner_pid = getpid();
    struct output_buffer out = {.fd = -1};
    struct output_buffer err = {.fd = -1};
    struct child_error child_error = {0, 0};
    size_t child_error_bytes = 0;
    bool timed_out = false;
    bool reaped = false;
    bool timeout_escalated = false;
    int child_status = 0;
    struct timespec start;
    struct timespec now;
    struct timespec timeout_signal_time = {0, 0};

    if (parse_options(argc, argv, &config) != 0) {
        usage(stderr);
        emit_cli_error("invalid arguments; explicit non-root identity and all four limits are required");
        return 2;
    }
    if (geteuid() != 0) {
        emit_cli_error("runner must start with effective uid 0 to drop to the configured identity");
        return 2;
    }
    if (canonical_paths(&config, root, cwd, path_error, sizeof(path_error)) != 0) {
        emit_cli_error(path_error);
        return 2;
    }
    if (pipe2(stdout_pipe, O_CLOEXEC) != 0 ||
        pipe2(stderr_pipe, O_CLOEXEC) != 0 ||
        pipe2(error_pipe, O_CLOEXEC) != 0) {
        emit_cli_error("cannot create runner pipes");
        goto cli_failure;
    }
    if (set_nonblocking(stdout_pipe[0]) != 0 || set_nonblocking(stderr_pipe[0]) != 0 ||
        set_nonblocking(error_pipe[0]) != 0) {
        emit_cli_error("cannot configure runner pipes");
        goto cli_failure;
    }
    if (clock_gettime(CLOCK_MONOTONIC, &start) != 0) {
        emit_cli_error("cannot read monotonic clock");
        goto cli_failure;
    }
    child = fork();
    if (child < 0) {
        emit_cli_error("cannot fork child");
        goto cli_failure;
    }
    if (child == 0) {
        close(stdout_pipe[0]);
        close(stderr_pipe[0]);
        close(error_pipe[0]);
        if (config.namespace_isolation) {
            int parent_alive_fd = namespace_supervise(runner_pid, stdout_pipe[1],
                                                      stderr_pipe[1], error_pipe[1]);
            child_main(&config, root, cwd, stdout_pipe[1], stderr_pipe[1],
                       error_pipe[1], parent_alive_fd);
        } else {
            child_main(&config, root, cwd, stdout_pipe[1], stderr_pipe[1], error_pipe[1], -1);
        }
        child_exec(&config, argv, error_pipe[1]);
        _exit(127);
    }
    close(stdout_pipe[1]); stdout_pipe[1] = -1;
    close(stderr_pipe[1]); stderr_pipe[1] = -1;
    close(error_pipe[1]); error_pipe[1] = -1;
    (void)setpgid(child, child);
    out.fd = stdout_pipe[0]; stdout_pipe[0] = -1;
    err.fd = stderr_pipe[0]; stderr_pipe[0] = -1;

    while (!reaped || out.fd >= 0 || err.fd >= 0 || error_pipe[0] >= 0) {
        struct pollfd descriptors[3];
        nfds_t descriptor_count = 0;
        int poll_timeout = 25;
        if (out.fd >= 0) {
            descriptors[descriptor_count].fd = out.fd;
            descriptors[descriptor_count].events = POLLIN | POLLHUP | POLLERR;
            descriptors[descriptor_count++].revents = 0;
        }
        if (err.fd >= 0) {
            descriptors[descriptor_count].fd = err.fd;
            descriptors[descriptor_count].events = POLLIN | POLLHUP | POLLERR;
            descriptors[descriptor_count++].revents = 0;
        }
        if (error_pipe[0] >= 0) {
            descriptors[descriptor_count].fd = error_pipe[0];
            descriptors[descriptor_count].events = POLLIN | POLLHUP | POLLERR;
            descriptors[descriptor_count++].revents = 0;
        }
        if (clock_gettime(CLOCK_MONOTONIC, &now) == 0) {
            long long elapsed = elapsed_milliseconds(&start, &now);
            if (!timed_out && elapsed >= (long long)config.timeout_ms && !reaped) {
                timed_out = true;
                if (config.namespace_isolation) {
                    timeout_signal_time = now;
                    (void)kill(child, SIGTERM);
                } else {
                    (void)kill(-child, SIGKILL);
                    (void)kill(child, SIGKILL);
                }
            } else if (timed_out && config.namespace_isolation && !timeout_escalated &&
                       elapsed_milliseconds(&timeout_signal_time, &now) >= 250) {
                (void)kill(-child, SIGKILL);
                (void)kill(child, SIGKILL);
                timeout_escalated = true;
            }
            if (!timed_out && !reaped) {
                long long remaining = (long long)config.timeout_ms - elapsed;
                if (remaining < poll_timeout)
                    poll_timeout = remaining > 0 ? (int)remaining : 0;
            }
        }
        if (descriptor_count > 0)
            (void)poll(descriptors, descriptor_count, poll_timeout);
        else
            (void)poll(NULL, 0, poll_timeout);
        if (out.fd >= 0)
            drain_output(&out);
        if (err.fd >= 0)
            drain_output(&err);
        if (error_pipe[0] >= 0) {
            for (;;) {
                unsigned char *target = (unsigned char *)&child_error + child_error_bytes;
                ssize_t got = read(error_pipe[0], target,
                                   sizeof(child_error) - child_error_bytes);
                if (got > 0) {
                    child_error_bytes += (size_t)got;
                    if (child_error_bytes == sizeof(child_error))
                        break;
                } else if (got == 0) {
                    close_fd(&error_pipe[0]);
                    break;
                } else if (errno == EINTR) {
                    continue;
                } else {
                    break;
                }
            }
        }
        if (!reaped) {
            pid_t waited = waitpid(child, &child_status, WNOHANG);
            if (waited == child)
                reaped = true;
            else if (waited < 0 && errno == ECHILD)
                reaped = true;
        }
        if (reaped && out.fd >= 0)
            drain_output(&out);
        if (reaped && err.fd >= 0)
            drain_output(&err);
    }

    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0)
        now = start;
    long long elapsed = elapsed_milliseconds(&start, &now);
    return emit_result(&config, &out, &err, child_status, reaped, timed_out,
                       &child_error, child_error_bytes, elapsed);

cli_failure:
    close_fd(&stdout_pipe[0]);
    close_fd(&stdout_pipe[1]);
    close_fd(&stderr_pipe[0]);
    close_fd(&stderr_pipe[1]);
    close_fd(&error_pipe[0]);
    close_fd(&error_pipe[1]);
    return 2;
}
