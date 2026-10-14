/* net: XTOS native network interface management (C, static).
 * show/up/down/addr-add/addr-flush via ioctl + AF_NETLINK.
 * Build: gcc -O2 -Wall -Wextra -Werror -static -o xtos-net xtos-net.c
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <ctype.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <net/if.h>
#include <net/if_arp.h>
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <linux/netlink.h>
#include <linux/rtnetlink.h>
#include <dirent.h>
#include "xtos_ui.h"

#define NLMSG_SIZE  4096

static int validate_name(const char *name) {
    if (!name || !name[0] || strcmp(name, "lo") == 0 || strlen(name) >= IFNAMSIZ)
        return -1;
    if (!isalnum((unsigned char)name[0]))
        return -1;
    for (const char *p = name; *p; p++) {
        if (!isalnum((unsigned char)*p) && *p != '_' && *p != '.' && *p != '-')
            return -1;
    }
    char path[256];
    snprintf(path, sizeof(path), "/sys/class/net/%s", name);
    return access(path, F_OK) == 0 ? 0 : -1;
}

static int get_flags(const char *name, short *flags) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) return -1;
    struct ifreq ifr;
    memset(&ifr, 0, sizeof(ifr));
    strncpy(ifr.ifr_name, name, IFNAMSIZ - 1);
    int rc = ioctl(fd, SIOCGIFFLAGS, &ifr);
    close(fd);
    if (rc == 0 && flags) *flags = ifr.ifr_flags;
    return rc;
}

static int set_flags(const char *name, short flags) {
    int fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (fd < 0) return -1;
    struct ifreq ifr;
    memset(&ifr, 0, sizeof(ifr));
    strncpy(ifr.ifr_name, name, IFNAMSIZ - 1);
    ifr.ifr_flags = flags;
    int rc = ioctl(fd, SIOCSIFFLAGS, &ifr);
    close(fd);
    return rc;
}

static int iface_up(const char *name) {
    short flags;
    if (get_flags(name, &flags) < 0) return -1;
    return set_flags(name, flags | IFF_UP);
}

static int iface_down(const char *name) {
    short flags;
    if (get_flags(name, &flags) < 0) return -1;
    return set_flags(name, flags & ~IFF_UP);
}

/* Netlink address operation */
static int netlink_addr_op(int op, const char *ifname, struct in_addr *addr, int prefix) {
    int fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
    if (fd < 0) return -1;

    unsigned int ifindex = if_nametoindex(ifname);
    if (ifindex == 0) { close(fd); return -1; }

    char buf[NLMSG_SIZE];
    memset(buf, 0, sizeof(buf));

    struct nlmsghdr *nl = (struct nlmsghdr *)buf;
    nl->nlmsg_len = NLMSG_LENGTH(sizeof(struct ifaddrmsg) + RTA_LENGTH(sizeof(struct in_addr)));
    nl->nlmsg_type = op;
    nl->nlmsg_flags = NLM_F_REQUEST | NLM_F_ACK | NLM_F_CREATE;
    nl->nlmsg_seq = 1;
    nl->nlmsg_pid = getpid();

    struct ifaddrmsg *ifa = NLMSG_DATA(nl);
    ifa->ifa_family = AF_INET;
    ifa->ifa_prefixlen = (unsigned char)prefix;
    ifa->ifa_scope = RT_SCOPE_UNIVERSE;
    ifa->ifa_index = ifindex;

    struct rtattr *rta = IFA_RTA(ifa);
    rta->rta_type = IFA_LOCAL;
    rta->rta_len = RTA_LENGTH(sizeof(struct in_addr));
    memcpy(RTA_DATA(rta), addr, sizeof(struct in_addr));

    struct sockaddr_nl nladdr;
    memset(&nladdr, 0, sizeof(nladdr));
    nladdr.nl_family = AF_NETLINK;

    if (sendto(fd, buf, nl->nlmsg_len, 0, (struct sockaddr *)&nladdr, sizeof(nladdr)) < 0) {
        close(fd);
        return -1;
    }

    int rc = 0;
    while (1) {
        int n = recv(fd, buf, sizeof(buf), 0);
        if (n < 0) { rc = -1; break; }
        struct nlmsghdr *resp = (struct nlmsghdr *)buf;
        if (resp->nlmsg_type == NLMSG_ERROR) {
            struct nlmsgerr *err = NLMSG_DATA(resp);
            if (err->error != 0) rc = err->error;
            break;
        }
        if (resp->nlmsg_type == NLMSG_DONE) break;
    }
    close(fd);
    return rc;
}

static int addr_add(const char *ifname, const char *cidr) {
    char ipbuf[64];
    strncpy(ipbuf, cidr, sizeof(ipbuf) - 1);
    ipbuf[sizeof(ipbuf) - 1] = '\0';
    char *slash = strchr(ipbuf, '/');
    if (!slash) {
        fprintf(stderr, "net: address must be in CIDR format (e.g. 192.168.1.10/24)\n");
        return 1;
    }
    *slash = '\0';
    int prefix = atoi(slash + 1);
    if (prefix < 0 || prefix > 32) {
        fprintf(stderr, "net: invalid prefix length %d\n", prefix);
        return 1;
    }
    struct in_addr addr;
    if (inet_pton(AF_INET, ipbuf, &addr) != 1) {
        fprintf(stderr, "net: invalid IPv4 address %s\n", ipbuf);
        return 1;
    }
    int rc = netlink_addr_op(RTM_NEWADDR, ifname, &addr, prefix);
    if (rc != 0) {
        fprintf(stderr, "net: addr add failed: %s\n", strerror(-rc));
        return 1;
    }
    printf("XTOS_NET_ADDR_ADD interface=%s address=%s\n", ifname, cidr);
    return 0;
}

static int addr_flush(const char *ifname) {
    struct ifaddrs *ifap;
    if (getifaddrs(&ifap) != 0) {
        fprintf(stderr, "net: getifaddrs failed\n");
        return 1;
    }
    int errors = 0;
    for (struct ifaddrs *ifa = ifap; ifa; ifa = ifa->ifa_next) {
        if (strcmp(ifa->ifa_name, ifname) != 0 || !ifa->ifa_addr) continue;
        if (ifa->ifa_addr->sa_family != AF_INET) continue;
        struct sockaddr_in *sin = (struct sockaddr_in *)ifa->ifa_addr;
        int prefix = 32;
        if (ifa->ifa_netmask) {
            struct sockaddr_in *mask = (struct sockaddr_in *)ifa->ifa_netmask;
            prefix = __builtin_popcount(ntohl(mask->sin_addr.s_addr));
        }
        int rc = netlink_addr_op(RTM_DELADDR, ifname, &sin->sin_addr, prefix);
        if (rc != 0) errors++;
    }
    freeifaddrs(ifap);
    if (errors > 0) {
        fprintf(stderr, "net: %d address(es) could not be removed\n", errors);
        return 1;
    }
    printf("XTOS_NET_ADDR_FLUSH interface=%s\n", ifname);
    return 0;
}

static int read_sysfs(const char *ifname, const char *attr, char *buf, size_t size) {
    char path[512];
    snprintf(path, sizeof(path), "/sys/class/net/%s/%s", ifname, attr);
    FILE *f = fopen(path, "r");
    if (!f) { buf[0] = '\0'; return -1; }
    if (fgets(buf, (int)size, f)) buf[strcspn(buf, "\n")] = '\0';
    else buf[0] = '\0';
    fclose(f);
    return 0;
}

static int show_interfaces(const char *specific, int json) {
    struct ifaddrs *ifap;
    if (getifaddrs(&ifap) != 0) {
        fprintf(stderr, "net: getifaddrs failed\n");
        return 1;
    }

    /* Unique names */
    char names[16][IFNAMSIZ];
    int nifaces = 0;
    for (struct ifaddrs *ifa = ifap; ifa; ifa = ifa->ifa_next) {
        if (strcmp(ifa->ifa_name, "lo") == 0) continue;
        if (specific && strcmp(ifa->ifa_name, specific) != 0) continue;
        int found = 0;
        for (int i = 0; i < nifaces; i++)
            if (strcmp(names[i], ifa->ifa_name) == 0) { found = 1; break; }
        if (!found && nifaces < 16)
            strncpy(names[nifaces++], ifa->ifa_name, IFNAMSIZ - 1);
    }

    if (!json) printf("%s%s%s\n", XUI_LABEL_S, "NAME       STATE   MTU    SPEED     ADDRESSES", XUI_RESET_S);
    else printf("[");

    for (int i = 0; i < nifaces; i++) {
        const char *name = names[i];
        char state[32], speedbuf[32];
        short flags = 0;
        get_flags(name, &flags);
        int up = (flags & IFF_UP) != 0;
        read_sysfs(name, "operstate", state, sizeof(state));
        read_sysfs(name, "speed", speedbuf, sizeof(speedbuf));
        long speed = atol(speedbuf);
        int mtu = 0;
        {
            int fd = socket(AF_INET, SOCK_DGRAM, 0);
            if (fd >= 0) {
                struct ifreq ifr;
                memset(&ifr, 0, sizeof(ifr));
                memcpy(ifr.ifr_name, name, strnlen(name, IFNAMSIZ - 1));
                if (ioctl(fd, SIOCGIFMTU, &ifr) == 0) mtu = ifr.ifr_mtu;
                close(fd);
            }
        }

        /* Addresses */
        char addrs[512] = "";
        size_t alen = 0;
        for (struct ifaddrs *ifa = ifap; ifa; ifa = ifa->ifa_next) {
            if (strcmp(ifa->ifa_name, name) != 0 || !ifa->ifa_addr) continue;
            if (ifa->ifa_addr->sa_family != AF_INET) continue;
            char ab[INET_ADDRSTRLEN];
            struct sockaddr_in *sin = (struct sockaddr_in *)ifa->ifa_addr;
            inet_ntop(AF_INET, &sin->sin_addr, ab, sizeof(ab));
            if (alen > 0 && alen < sizeof(addrs) - 3) {
                addrs[alen++] = ','; addrs[alen++] = ' ';
            }
            size_t sl = strlen(ab);
            if (alen + sl < sizeof(addrs) - 1) {
                memcpy(addrs + alen, ab, sl); alen += sl;
            }
        }
        addrs[alen] = '\0';

        if (json) {
            printf("%s\n  {\"name\": \"%s\", \"up\": %s, \"oper_state\": \"%s\", \"mtu\": %d, \"speed_mbps\": %ld, \"addresses\": \"%s\"}",
                   i > 0 ? "," : "", name, up ? "true" : "false", state, mtu, speed, addrs);
        } else {
            printf("%-10s ", name);
            if (up) printf("%sup     %s", XUI_OK_S, XUI_RESET_S);
            else printf("%sdown   %s", XUI_DIM_S, XUI_RESET_S);
            printf("%-6d %-9s %s\n", mtu,
                   speed > 0 ? speedbuf : "-", addrs[0] ? addrs : "-");
        }
    }
    if (json) printf("\n]\n");
    freeifaddrs(ifap);
    return 0;
}


static int show_routes(void) {
    /* Dump IPv4 routing table via rtnetlink */
    int fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_ROUTE);
    if (fd < 0) { perror("net: socket"); return 1; }
    struct sockaddr_nl sa = { .nl_family = AF_NETLINK };
    if (bind(fd, (struct sockaddr *)&sa, sizeof(sa)) < 0) { perror("net: bind"); close(fd); return 1; }

    char buf[16384];
    struct nlmsghdr *nlh = (struct nlmsghdr *)buf;
    memset(buf, 0, sizeof(buf));
    struct rtmsg *rtm = (struct rtmsg *)(buf + sizeof(*nlh));
    nlh->nlmsg_len = NLMSG_LENGTH(sizeof(*rtm));
    nlh->nlmsg_type = RTM_GETROUTE;
    nlh->nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP;
    nlh->nlmsg_seq = 1;
    rtm->rtm_family = AF_INET;
    if (send(fd, nlh, nlh->nlmsg_len, 0) < 0) { perror("net: send"); close(fd); return 1; }

    printf("%s%-18s %-15s %-15s%s %s\n", XUI_LABEL_S, "DESTINATION", "GATEWAY", "GENMASK", XUI_RESET_S, "IFACE");
    int n = 0;
    for (;;) {
        ssize_t len = recv(fd, buf, sizeof(buf), 0);
        if (len <= 0) break;
        struct nlmsghdr *h = (struct nlmsghdr *)buf;
        for (; NLMSG_OK(h, (unsigned)len); h = NLMSG_NEXT(h, len)) {
            if (h->nlmsg_type == NLMSG_DONE) goto done;
            if (h->nlmsg_type == NLMSG_ERROR) { fprintf(stderr, "net: route dump error\n"); goto done; }
            struct rtmsg *r = NLMSG_DATA(h);
            if (r->rtm_family != AF_INET) continue;
            if (r->rtm_flags & RTM_F_CLONED) continue;
            char dst[32] = "0.0.0.0", gw[32] = "0.0.0.0", mask[32] = "0.0.0.0";
            int dstlen = r->rtm_dst_len;
            struct rtattr *a = RTM_RTA(r);
            int alen = RTM_PAYLOAD(h);
            char ifname[IFNAMSIZ] = "?";
            for (; RTA_OK(a, alen); a = RTA_NEXT(a, alen)) {
                if (a->rta_type == RTA_DST)
                    inet_ntop(AF_INET, RTA_DATA(a), dst, sizeof(dst));
                else if (a->rta_type == RTA_GATEWAY)
                    inet_ntop(AF_INET, RTA_DATA(a), gw, sizeof(gw));
                else if (a->rta_type == RTA_OIF) {
                    int idx = *(int *)RTA_DATA(a);
                    if_indextoname(idx, ifname);
                }
            }
            /* Compute mask from prefix length */
            if (dstlen > 0 && dstlen <= 32) {
                struct in_addr m = { .s_addr = htonl(dstlen == 32 ? 0xffffffffu : ~((1u << (32 - dstlen)) - 1u)) };
                inet_ntop(AF_INET, &m, mask, sizeof(mask));
            }
            printf("%-18s %-15s %-15s %s\n", dst, gw, mask, ifname);
            n++;
        }
    }
done:
    close(fd);
    return 0;
}

static void usage(void) {
    fprintf(stderr,
        "usage: net [command]\n"
        "  net show [interface]    list interfaces\n"
        "  net up <interface>      bring interface up\n"
        "  net down <interface>    take interface down\n"
        "  net addr add <iface> <cidr>   assign IPv4 address\n"
        "  net addr flush <iface>        remove all IPv4 addresses\n"
        "  net route                     show IPv4 routing table\n"
        "  --json                        machine-readable output\n");
}

int main(int argc, char **argv) {
    int json = 0;
    int argstart = 1;
    if (argc > 1 && strcmp(argv[1], "--json") == 0) { json = 1; argstart = 2; }
    if (argc <= argstart) {
        return show_interfaces(NULL, json);
    }

    const char *cmd = argv[argstart];
    if (strcmp(cmd, "show") == 0) {
        if (argc > argstart + 1) {
            if (validate_name(argv[argstart + 1]) < 0) {
                fprintf(stderr, "net: invalid interface\n");
                return 1;
            }
            return show_interfaces(argv[argstart + 1], json);
        }
        return show_interfaces(NULL, json);
    }
    if (strcmp(cmd, "up") == 0 || strcmp(cmd, "down") == 0) {
        if (argc <= argstart + 1) { usage(); return 2; }
        const char *ifname = argv[argstart + 1];
        if (validate_name(ifname) < 0) {
            fprintf(stderr, "net: invalid interface %s\n", ifname);
            return 1;
        }
        int rc = (cmd[0] == 'u') ? iface_up(ifname) : iface_down(ifname);
        if (rc < 0) {
            fprintf(stderr, "net: %s %s failed\n", cmd, ifname);
            return 1;
        }
        printf("XTOS_NET_%s interface=%s\n", cmd[0] == 'u' ? "UP" : "DOWN", ifname);
        return 0;
    }
    if (strcmp(cmd, "route") == 0) {
        return show_routes();
    }
    if (strcmp(cmd, "addr") == 0) {
        if (argc <= argstart + 1) { usage(); return 2; }
        const char *sub = argv[argstart + 1];
        if (strcmp(sub, "add") == 0 && argc == argstart + 4) {
            const char *ifname = argv[argstart + 2];
            if (validate_name(ifname) < 0) {
                fprintf(stderr, "net: invalid interface %s\n", ifname);
                return 1;
            }
            return addr_add(ifname, argv[argstart + 3]);
        }
        if (strcmp(sub, "flush") == 0 && argc == argstart + 3) {
            const char *ifname = argv[argstart + 2];
            if (validate_name(ifname) < 0) {
                fprintf(stderr, "net: invalid interface %s\n", ifname);
                return 1;
            }
            return addr_flush(ifname);
        }
        usage();
        return 2;
    }
    usage();
    return 2;
}
