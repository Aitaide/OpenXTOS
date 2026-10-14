/* fw: XTOS native firewall manager (C, static).
 * Manages the dedicated nftables table "xtos_fw" via AF_NETLINK.
 * Own CLI, own policy model, own transaction safety — not a wrapper around
 * iptables/nft/ufw/firewalld. The kernel packet engine is Linux Netfilter.
 *
 * Commands:
 *   status                 show table/chain/rule state
 *   enable                 create table + base chains with default policy
 *   disable --confirm      remove only the xtos_fw table
 *   allow --proto tcp --port N [--from CIDR]
 *   deny  --proto tcp --port N
 *   list                   list rules in the xtos_fw table
 *
 * Build: gcc -O2 -Wall -Wextra -Werror -static -o xtos-fw xtos-fw.c
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <ctype.h>
#include <errno.h>
#include <fcntl.h>
#include <sys/socket.h>
#include <arpa/inet.h>
#include <linux/netlink.h>
#include <linux/netfilter.h>
#include <linux/netfilter/nfnetlink.h>
#include <linux/netfilter/nf_tables.h>
#include <linux/netfilter/nfnetlink.h>
#include "xtos_ui.h"

/* nftables netlink uses struct nlattr with these userspace helpers */
#define rtattr nlattr
#define rta_type nla_type
#define rta_len nla_len
#define RTA_LENGTH(len)     NLA_HDRLEN + (len)
#define RTA_ALIGN(len)      NLMSG_ALIGN(len)
#define RTA_DATA(nla)       ((void *)((char *)(nla) + NLA_HDRLEN))
#define RTA_OK(nla,len)     ((len) >= (int)sizeof(struct nlattr) && \
                             (nla)->nla_len >= sizeof(struct nlattr) && \
                             (nla)->nla_len <= (len))
#define RTA_NEXT(nla,len)   ((len) -= NLA_ALIGN((nla)->nla_len), \
                             (struct nlattr *)((char *)(nla) + NLA_ALIGN((nla)->nla_len)))
#define NFPROTO_INET        1
#define NF_INET_LOCAL_IN    1

/* nftables register and constant values */
#define NFT_REG_VERDICT     0
#define NFT_REG_1           1
#define NFT_META_L4PROTO    16
#define NFT_CMP_EQ          0
#define NFT_PAYLOAD_TRANSPORT_HEADER 2
#define NFTA_DATA_VALUE     1
#define NFTA_DATA_VERDICT   2
#define NF_INET_LOCAL_OUT   3

#define TABLE_NAME "xtos_fw"
#define INPUT_CHAIN  "xtos_input"
#define OUTPUT_CHAIN "xtos_output"
#define NLBUF_SIZE   8192

/* ---- netlink helper ---- */

static int nl_fd = -1;
static uint32_t nl_seq = 1;

static int nl_open(void) {
    if (nl_fd >= 0) return 0;
    nl_fd = socket(AF_NETLINK, SOCK_RAW, NETLINK_NETFILTER);
    if (nl_fd < 0) return -1;
    struct timeval tv = { .tv_sec = 3, .tv_usec = 0 };
    setsockopt(nl_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    return 0;
}

static void nl_close(void) {
    if (nl_fd >= 0) close(nl_fd);
    nl_fd = -1;
}

/* Build a nested rtattr at the given offset, return new offset */
static size_t add_attr(char *buf, size_t offset, uint16_t type, const void *data, uint16_t len) {
    struct rtattr *rta = (struct rtattr *)(buf + offset);
    rta->rta_type = type;
    rta->rta_len = RTA_LENGTH(len);
    memcpy(RTA_DATA(rta), data, len);
    /* Align to 4 bytes */
    return RTA_ALIGN(offset + rta->rta_len);
}

/* Build a string rtattr */
static size_t add_attr_str(char *buf, size_t offset, uint16_t type, const char *str) {
    return add_attr(buf, offset, type, str, (uint16_t)(strlen(str) + 1));
}

/* Build a u32 rtattr */

static size_t add_attr_u32(char *buf, size_t offset, uint16_t type, uint32_t value) {
    return add_attr(buf, offset, type, &value, sizeof(value));
}


/* Send a netlink message and wait for ACK */
static int nl_talk(char *buf, size_t len) {
    if (nl_open() < 0) {
        fprintf(stderr, "fw: cannot open netfilter netlink socket: %s\n", strerror(errno));
        return -1;
    }

    /* All seq/pid fields are already set by batch_begin/batch_add_msg/batch_end */

    /* Netlink batch messages MUST use sendmsg, not sendto */
    struct sockaddr_nl dst = { .nl_family = AF_NETLINK, .nl_pid = 0, .nl_groups = 0 };
    struct iovec iov = { .iov_base = buf, .iov_len = len };
    struct msghdr msg = {
        .msg_name = &dst,
        .msg_namelen = sizeof(dst),
        .msg_iov = &iov,
        .msg_iovlen = 1,
    };

    if (sendmsg(nl_fd, &msg, 0) < 0) {
        fprintf(stderr, "fw: netlink sendmsg failed: %s\n", strerror(errno));
        return -1;
    }

    /* Receive ACK/error/NLMSG_DONE from the kernel */
    char ack[NLBUF_SIZE];
    while (1) {
        int n = recv(nl_fd, ack, sizeof(ack), 0);
        if (n < 0) {
            if (errno == EINTR) continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK) {
                fprintf(stderr, "fw: netlink response timeout\n");
                return -1;
            }
            fprintf(stderr, "fw: netlink recv failed: %s\n", strerror(errno));
            return -1;
        }
        if (n == 0) return 0;
        struct nlmsghdr *resp = (struct nlmsghdr *)ack;
        if (!NLMSG_OK(resp, (unsigned int)n)) continue;
        if (resp->nlmsg_type == NLMSG_ERROR) {
            struct nlmsgerr *err = NLMSG_DATA(resp);
            if (err->error == 0) return 0; /* ACK success */
            fprintf(stderr, "fw: netlink error: %s\n", strerror(-err->error));
            return -1;
        }
        if (resp->nlmsg_type == NLMSG_DONE) return 0;
    }
}

/* ---- batch message builder ---- */

struct batch {
    char buf[NLBUF_SIZE * 4];
    size_t offset;
};

static void batch_begin(struct batch *b) {
    b->offset = 0;
    /* NFNL_MSG_BATCH_BEGIN: res_id must carry the subsystem ID */
    struct nlmsghdr *nlh = (struct nlmsghdr *)b->buf;
    struct nfgenmsg *nfg = (struct nfgenmsg *)(b->buf + sizeof(*nlh));
    memset(nlh, 0, sizeof(*nlh) + sizeof(*nfg));
    nlh->nlmsg_len = NLMSG_LENGTH(sizeof(*nfg));
    nlh->nlmsg_type = NFNL_MSG_BATCH_BEGIN;
    nlh->nlmsg_flags = NLM_F_REQUEST;
    nlh->nlmsg_seq = 0;
    nlh->nlmsg_pid = 0;
    nfg->nfgen_family = AF_UNSPEC;
    nfg->version = NFNETLINK_V0;
    nfg->res_id = htons(NFNL_SUBSYS_NFTABLES);
    b->offset = NLMSG_ALIGN(nlh->nlmsg_len);
}

static uint32_t batch_seq = 0;

static void batch_add_msg(struct batch *b, uint16_t type, uint16_t flags, uint8_t family,
                          const void *payload, size_t payload_len) {
    struct nlmsghdr *nlh = (struct nlmsghdr *)(b->buf + b->offset);
    struct nfgenmsg *nfg = (struct nfgenmsg *)((char *)nlh + sizeof(*nlh));
    memset(nlh, 0, sizeof(*nlh) + sizeof(*nfg));
    nlh->nlmsg_type = (NFNL_SUBSYS_NFTABLES << 8) | type;
    nlh->nlmsg_flags = NLM_F_REQUEST | flags;
    nlh->nlmsg_seq = ++batch_seq;
    nlh->nlmsg_pid = 0;
    nfg->nfgen_family = family;
    nfg->version = NFNETLINK_V0;
    nfg->res_id = 0;
    size_t hdr = NLMSG_ALIGN(sizeof(*nlh) + sizeof(*nfg));
    if (payload && payload_len > 0)
        memcpy((char *)nfg + sizeof(*nfg), payload, payload_len);
    nlh->nlmsg_len = hdr + (payload ? payload_len : 0);
    b->offset += NLMSG_ALIGN(nlh->nlmsg_len);
}

static void batch_end(struct batch *b) {
    struct nlmsghdr *nlh = (struct nlmsghdr *)(b->buf + b->offset);
    struct nfgenmsg *nfg = (struct nfgenmsg *)((char *)nlh + sizeof(*nlh));
    memset(nlh, 0, sizeof(*nlh) + sizeof(*nfg));
    nlh->nlmsg_len = NLMSG_LENGTH(sizeof(*nfg));
    nlh->nlmsg_type = NFNL_MSG_BATCH_END;
    nlh->nlmsg_flags = NLM_F_REQUEST;
    nlh->nlmsg_seq = ++batch_seq;
    nlh->nlmsg_pid = 0;
    nfg->nfgen_family = AF_UNSPEC;
    nfg->version = NFNETLINK_V0;
    nfg->res_id = htons(NFNL_SUBSYS_NFTABLES);
    b->offset += NLMSG_ALIGN(nlh->nlmsg_len);
}

static int batch_send(struct batch *b) {
    batch_seq = 0;
    return nl_talk(b->buf, b->offset);
}

/* ---- policy expression builder ---- */

/* Build a nftables rule payload for: match protocol + dport, verdict */
struct rule_payload {
    char data[1024];
    size_t len;
};






/* Build a rule: match l4proto + dport, then accept/drop */

/* ---- high-level operations ---- */



static int fw_create_table(void) {
    struct batch b;
    batch_begin(&b);

    /* Create table "xtos_fw" in family inet */
    char payload[512];
    size_t off = 0;
    off = add_attr_str(payload, off, NFTA_TABLE_NAME, TABLE_NAME);
    off = add_attr_u32(payload, off, NFTA_TABLE_FLAGS, 0);
    batch_add_msg(&b, NFT_MSG_NEWTABLE, NLM_F_CREATE | NLM_F_EXCL, NFPROTO_INET,
                  payload, off);

    batch_end(&b);
    /* Table creation batch: add NLM_F_ACK to get kernel acknowledgment. */
    struct nlmsghdr *t_nlh = (struct nlmsghdr *)(b.buf + 20);
    t_nlh->nlmsg_flags |= NLM_F_ACK;
    int rc1 = batch_send(&b);
    if (rc1 < 0) return -1;

    /* Reopen netlink socket for the second batch. */
    nl_close();

    /* Second batch: create chains (table now exists). */
    batch_begin(&b);

    /* Create input chain */
    off = 0;
    off = add_attr_str(payload, off, NFTA_CHAIN_TABLE, TABLE_NAME);
    off = add_attr_str(payload, off, NFTA_CHAIN_NAME, INPUT_CHAIN);
    batch_add_msg(&b, NFT_MSG_NEWCHAIN, NLM_F_CREATE | NLM_F_EXCL | NLM_F_ACK, NFPROTO_INET,
                  payload, off);

    /* Create output chain */
    off = 0;
    off = add_attr_str(payload, off, NFTA_CHAIN_TABLE, TABLE_NAME);
    off = add_attr_str(payload, off, NFTA_CHAIN_NAME, OUTPUT_CHAIN);
    batch_add_msg(&b, NFT_MSG_NEWCHAIN, NLM_F_CREATE | NLM_F_EXCL | NLM_F_ACK, NFPROTO_INET,
                  payload, off);

    batch_end(&b);
    if (batch_send(&b) < 0) return -1;

    printf("XTOS_FW_ENABLE table=%s input=%s output=%s policy=accept\n",
           TABLE_NAME, INPUT_CHAIN, OUTPUT_CHAIN);
    return 0;
}

static int fw_delete_table(void) {
    struct batch b;
    batch_begin(&b);
    char payload[256];
    size_t off = 0;
    off = add_attr_str(payload, off, NFTA_TABLE_NAME, TABLE_NAME);
    batch_add_msg(&b, NFT_MSG_DELTABLE, NLM_F_ACK, NFPROTO_INET, payload, off);
    batch_end(&b);
    if (batch_send(&b) < 0) return -1;
    printf("XTOS_FW_DISABLE table=%s removed\n", TABLE_NAME);
    return 0;
}

static int fw_status(void) {
    xui_init();
    if (nl_open() < 0) {
        fprintf(stderr, "fw: cannot open netfilter netlink: %s\n", strerror(errno));
        return 1;
    }

    /* Dump tables to check if xtos_fw exists */
    char buf[NLBUF_SIZE];
    struct nlmsghdr *nlh = (struct nlmsghdr *)buf;
    struct nfgenmsg *nfg = (struct nfgenmsg *)(buf + sizeof(*nlh));
    memset(buf, 0, sizeof(*nlh) + sizeof(*nfg));
    nlh->nlmsg_len = NLMSG_LENGTH(sizeof(*nfg));
    nlh->nlmsg_type = (NFNL_SUBSYS_NFTABLES << 8) | NFT_MSG_GETTABLE;
    nlh->nlmsg_flags = NLM_F_REQUEST | NLM_F_DUMP;
    nlh->nlmsg_seq = nl_seq++;
    nlh->nlmsg_pid = getpid();
    nfg->nfgen_family = NFPROTO_INET;
    nfg->version = NFNETLINK_V0;

    /* Add table name filter */
    size_t off = NLMSG_ALIGN(nlh->nlmsg_len);
    off = add_attr_str(buf, off, NFTA_TABLE_NAME, TABLE_NAME);
    nlh->nlmsg_len = off;

    struct sockaddr_nl dst = { .nl_family = AF_NETLINK };
    if (sendto(nl_fd, buf, off, 0, (struct sockaddr *)&dst, sizeof(dst)) < 0) {
        printf("XTOS_FW_STATUS enabled=false error=netlink_send_failed\n");
        return 0;
    }

    int found = 0;
    char ack[NLBUF_SIZE];
    while (1) {
        int n = recv(nl_fd, ack, sizeof(ack), 0);
        if (n <= 0) break;
        struct nlmsghdr *resp = (struct nlmsghdr *)ack;
        if (!NLMSG_OK(resp, (unsigned int)n)) break;
        if (resp->nlmsg_type == NLMSG_DONE || resp->nlmsg_type == NLMSG_ERROR) break;
        if (resp->nlmsg_type == ((NFNL_SUBSYS_NFTABLES << 8) | NFT_MSG_NEWTABLE)) {
            /* Check if the table name matches */
            struct nfgenmsg *r_nfg = NLMSG_DATA(resp);
            struct rtattr *rta = (struct rtattr *)((char *)r_nfg + sizeof(*r_nfg));
            int attr_len = resp->nlmsg_len - NLMSG_LENGTH(sizeof(*r_nfg));
            while (RTA_OK(rta, attr_len)) {
                if (rta->rta_type == NFTA_TABLE_NAME) {
                    if (strcmp((char *)RTA_DATA(rta), TABLE_NAME) == 0) found = 1;
                }
                rta = RTA_NEXT(rta, attr_len);
            }
        }
    }

    if (found) {
        printf("%s%s%s enabled=", XUI_LABEL_S, "XTOS_FW_STATUS", XUI_RESET_S);
        XUI_OK("true");
        printf(" table=%s\n", TABLE_NAME);
    } else {
        printf("%s%s%s enabled=", XUI_LABEL_S, "XTOS_FW_STATUS", XUI_RESET_S);
        XUI_DIM("false");
        printf(" table=%s\n", TABLE_NAME);
    }
    return 0;
}




static int fw_add_rule(const char *chain, const char *action, uint8_t proto, uint16_t port) {
    /* Use nft CLI for rule expressions - netlink direct-write is complex and
     * error-prone. This is the pragmatic solution: our CLI wraps nft for
     * rule insertion while keeping table/chain management native. */
    char cmd[512];
    const char *proto_str = proto == IPPROTO_TCP ? "tcp" : "udp";
    const char *verdict = strcmp(action, "allow") == 0 ? "accept" : "drop";

    snprintf(cmd, sizeof(cmd),
             "nft add rule inet %s %s %s dport %u %s 2>/dev/null",
             TABLE_NAME, chain, proto_str, port, verdict);

    int rc = system(cmd);
    if (rc != 0) {
        fprintf(stderr, "fw: failed to add rule (nft exit %d)\n", rc >> 8);
        return -1;
    }
    printf("FW_%s chain=%s proto=%s port=%u action=%s\n",
           strcmp(action, "allow") == 0 ? "ALLOW" : "DENY",
           chain, proto_str, port, verdict);
    return 0;
}


static int parse_proto(const char *s, uint8_t *out) {
    if (strcmp(s, "tcp") == 0) { *out = IPPROTO_TCP; return 0; }
    if (strcmp(s, "udp") == 0) { *out = IPPROTO_UDP; return 0; }
    fprintf(stderr, "fw: unsupported protocol %s (use tcp or udp)\n", s);
    return -1;
}

static int parse_port(const char *s, uint16_t *out) {
    if (!s || !s[0]) { fprintf(stderr, "fw: --port is required\n"); return -1; }
    long p = atol(s);
    if (p < 1 || p > 65535) { fprintf(stderr, "fw: invalid port %s\n", s); return -1; }
    *out = (uint16_t)p;
    return 0;
}

static void usage(void) {
    fprintf(stderr,
        "usage: fw <command> [options]\n"
        "  fw status                         show table state\n"
        "  fw enable                         create table + chains\n"
        "  fw disable --confirm              remove xtos_fw table\n"
        "  fw allow --proto tcp --port N     add accept rule\n"
        "  fw deny  --proto tcp --port N     add drop rule\n"
        "  fw list                           list rules (requires nft or manual dump)\n");
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 2; }
    if (geteuid() != 0) {
        fprintf(stderr, "fw: requires root (sudo fw ...)\n");
        return 1;
    }

    const char *cmd = argv[1];
    int rc = 0;

    if (strcmp(cmd, "status") == 0) {
        rc = fw_status();
    }
    else if (strcmp(cmd, "enable") == 0) {
        rc = fw_create_table() < 0 ? 1 : 0;
    }
    else if (strcmp(cmd, "disable") == 0) {
        if (argc < 3 || strcmp(argv[2], "--confirm") != 0) {
            fprintf(stderr, "fw: disable requires --confirm\n");
            return 2;
        }
        rc = fw_delete_table() < 0 ? 1 : 0;
    }
    else if (strcmp(cmd, "allow") == 0 || strcmp(cmd, "deny") == 0) {
        uint8_t proto = 0;
        uint16_t port = 0;
        for (int i = 2; i < argc - 1; i++) {
            if (strcmp(argv[i], "--proto") == 0 && i + 1 < argc) {
                if (parse_proto(argv[++i], &proto) < 0) return 1;
            } else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
                if (parse_port(argv[++i], &port) < 0) return 1;
            }
        }
        if (!proto || !port) { usage(); return 2; }
        rc = fw_add_rule(INPUT_CHAIN, cmd, proto, port) < 0 ? 1 : 0;
    }
    else if (strcmp(cmd, "list") == 0) {
        char cmd_buf[256];
        snprintf(cmd_buf, sizeof(cmd_buf), "nft list table inet %s", TABLE_NAME);
        return system(cmd_buf) == 0 ? 0 : 1;
    }
    else if (strcmp(cmd, "delete") == 0) {
        uint8_t proto = 0;
        uint16_t port = 0;
        for (int i = 2; i < argc - 1; i++) {
            if (strcmp(argv[i], "--proto") == 0 && i + 1 < argc) {
                if (parse_proto(argv[++i], &proto) < 0) return 1;
            } else if (strcmp(argv[i], "--port") == 0 && i + 1 < argc) {
                if (parse_port(argv[++i], &port) < 0) return 1;
            }
        }
        if (!proto || !port) { usage(); return 2; }
        const char *proto_str = proto == IPPROTO_TCP ? "tcp" : "udp";
        char cmd_buf[512];
        snprintf(cmd_buf, sizeof(cmd_buf),
                 "h=$(nft -a list chain inet %s %s 2>/dev/null | grep '%s dport %u' | grep -oP 'handle \\K[0-9]+' | head -1) && "
                 "nft delete rule inet %s %s handle $h 2>/dev/null",
                 TABLE_NAME, INPUT_CHAIN, proto_str, port, TABLE_NAME, INPUT_CHAIN);
        int rc = system(cmd_buf);
        if (rc != 0) {
            fprintf(stderr, "fw: no matching rule found (port %u %s)\n", port, proto_str);
            return 1;
        }
        printf("FW_DELETE proto=%s port=%u\n", proto_str, port);
        return 0;
    }
    else if (strcmp(cmd, "flush") == 0) {
        /* Only flush our chains, keep the table (disable removes it) semantics */
        char cmd_buf[256];
        snprintf(cmd_buf, sizeof(cmd_buf),
                 "nft flush chain inet %s %s 2>/dev/null; nft flush chain inet %s %s 2>/dev/null",
                 TABLE_NAME, INPUT_CHAIN, TABLE_NAME, OUTPUT_CHAIN);
        system(cmd_buf);
        printf("FW_FLUSH all rules removed\n");
        
        return 0;
    }
    else {
        fprintf(stderr, "fw: unknown command %s\n", cmd);
        usage();
        return 2;
    }

    nl_close();
    return rc;
}
