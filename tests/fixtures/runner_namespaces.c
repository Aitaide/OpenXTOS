#define _GNU_SOURCE
/* Disposable-guest probe only; never run on the control host as a policy test. */
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <sys/un.h>
#include <sys/syscall.h>
#include <unistd.h>

static int ns(const char *name, char *out, size_t size) {
    char path[128];
    int used = snprintf(path, sizeof(path), "/proc/self/ns/%s", name);
    if (used < 0 || (size_t)used >= sizeof(path)) return -1;
    ssize_t count = readlink(path, out, size - 1);
    if (count < 0) return -1;
    out[count] = '\0';
    return 0;
}

/* Return 1 for a usable default route, 0 for another valid row, -1 when the
 * kernel's route row cannot be parsed. The guest gate must not infer safety
 * from an unparsed row. */
static int ipv4_default_route(const char *line) {
    char iface[64];
    unsigned destination = 0, gateway = 0, flags = 0, mask = 0;
    int parsed = sscanf(line, "%63s %x %x %x %*u %*u %*u %x %*u %*u %*u",
                        iface, &destination, &gateway, &flags, &mask);
    (void)gateway;
    if (parsed != 5) return -1;
    return destination == 0 && mask == 0 &&
           (flags & 0x1U) != 0 && (flags & 0x200U) == 0;
}

static int ipv6_default_route(const char *line) {
    char destination[33], source[33], next_hop[33], iface[64];
    unsigned destination_prefix = 0, source_prefix = 0;
    unsigned metric = 0, refcount = 0, use = 0, flags = 0;
    int parsed = sscanf(line,
        "%32s %x %32s %x %32s %x %x %x %x %63s",
        destination, &destination_prefix, source, &source_prefix, next_hop,
        &metric, &refcount, &use, &flags, iface);
    (void)destination;
    (void)source;
    (void)next_hop;
    (void)source_prefix;
    (void)metric;
    (void)refcount;
    (void)use;
    (void)iface;
    if (parsed != 10) return -1;
    return destination_prefix == 0 && (flags & 0x1U) != 0 &&
           (flags & 0x200U) == 0;
}

/* The exact kernel header of /proc/net/route; an empty routing table has no
 * header row at all, so the header is optional and only ever skipped when it
 * matches this prefix exactly. */
static int ipv4_route_header(const char *line) {
    return strncmp(line, "Iface\tDestination\t", 18) == 0;
}

/* A fresh, unconfigured network namespace contains only the devices the
 * kernel auto-creates: "lo", plus "sit0" when CONFIG_IPV6_SIT=y (guest
 * run-02 observed exactly lo and sit0, both down and unconfigured). sit0
 * cannot be removed and needs an explicit tunnel endpoint before it can
 * carry traffic; the confined target cannot configure it because every
 * socket() family including AF_NETLINK is denied. Any other device name
 * means the namespace is not fresh and must fail the probe. */
static int net_interface_allowed(const char *name) {
    return strcmp(name, "lo") == 0 || strcmp(name, "sit0") == 0;
}

int main(int argc, char **argv) {
    const char *names[] = {"mnt", "net", "pid"};
    if (argc == 2 && strcmp(argv[1], "--launch") == 0) {
        char parent[3][128];
        for (int i = 0; i < 3; i++) if (ns(names[i], parent[i], sizeof(parent[i])) != 0) return 2;
        char *args[] = {"/bin/runner", "--namespace-isolation", "--root", "/sandbox", "--cwd", "/sandbox",
            "--supp-groups-clear", "--cpu-sec", "2", "--as-bytes", "268435456",
            "--nofile", "64", "--nproc", "16", "--timeout-ms", "5000", "--",
            "/namespace_probe", "--probe", parent[0], parent[1], parent[2], NULL};
        /* Intentionally no --deny-socket: default policy must deny all families. */
        execv(args[0], args); return 3;
    }
    if (argc != 5 || strcmp(argv[1], "--probe") != 0) return 4;
    int failed = 0;
    for (int i = 0; i < 3; i++) {
        char current[128];
        int rc = ns(names[i], current, sizeof(current));
        int different = rc == 0 && strcmp(current, argv[i + 2]) != 0;
        printf("namespace_%s_parent=%s child=%s different=%d\n", names[i],
               argv[i + 2], rc == 0 ? current : "UNAVAILABLE", different);
        if (!different) failed = 1;
    }
    int families[] = {AF_UNIX, AF_INET, AF_INET6};
    const char *labels[] = {"unix", "inet", "inet6"};
    for (int i = 0; i < 3; i++) {
        errno = 0;
        long fd = syscall(SYS_socket, families[i], SOCK_STREAM, 0);
        int error = errno;
        printf("socket_%s=%ld errno=%d\n", labels[i], fd, error);
        if (fd >= 0) (void)close((int)fd);
        if (fd != -1 || error != EPERM) failed = 1;
        /* Even invalid/inherited descriptor attempts must hit seccomp EPERM,
         * not EBADF: connect itself is forbidden, independent of family. */
        struct sockaddr_storage address;
        memset(&address, 0, sizeof(address));
        address.ss_family = (sa_family_t)families[i];
        socklen_t size = families[i] == AF_UNIX ? sizeof(struct sockaddr_un) :
            families[i] == AF_INET ? sizeof(struct sockaddr_in) : sizeof(struct sockaddr_in6);
        errno = 0;
        long connected = syscall(SYS_connect, -1, &address, size);
        error = errno;
        printf("connect_%s=%ld errno=%d\n", labels[i], connected, error);
        if (connected != -1 || error != EPERM) failed = 1;
        int pair[2] = {-1, -1};
        errno = 0;
        long paired = syscall(SYS_socketpair, families[i], SOCK_STREAM, 0, pair);
        error = errno;
        printf("socketpair_%s=%ld errno=%d\n", labels[i], paired, error);
        if (paired == 0) {
            (void)close(pair[0]);
            (void)close(pair[1]);
        }
        if (paired != -1 || error != EPERM) failed = 1;
    }
    FILE *net = fopen("/proc/net/dev", "r");
    char line[1024];
    int network_ok = net != NULL;
    if (net != NULL) {
        while (fgets(line, sizeof(line), net) != NULL) {
            char *colon = strchr(line, ':');
            if (colon != NULL) {
                *colon = '\0';
                char *name = line;
                while (*name == ' ') name++;
                if (!net_interface_allowed(name)) network_ok = 0;
            }
        }
        if (ferror(net)) network_ok = 0;
        (void)fclose(net);
    }
    const char *routes[] = {"/proc/net/route", "/proc/net/ipv6_route"};
    for (int i = 0; i < 2; i++) {
        net = fopen(routes[i], "r");
        if (net == NULL) { network_ok = 0; continue; }
        /* An empty routing table legitimately has no header row at all; a
         * non-empty one starts with the exact kernel header, which carries no
         * route semantics and is skipped only on that exact match. */
        while (fgets(line, sizeof(line), net) != NULL) {
            if (i == 0 && ipv4_route_header(line)) continue;
            int route = i == 0 ? ipv4_default_route(line) : ipv6_default_route(line);
            if (route < 0) network_ok = 0;
            else if (route > 0) network_ok = 0;
        }
        if (ferror(net)) network_ok = 0;
        (void)fclose(net);
    }
    printf("network_no_usable_route=%d\n", network_ok);
    if (!network_ok) failed = 1;
    long pid = syscall(SYS_getpid);
    printf("namespace_pid_value=%ld\n", pid);
    if (pid != 1) failed = 1;
    printf("NAMESPACE_PROBE=%s\n", failed ? "FAIL" : "PASS");
    return failed ? 1 : 0;
}
