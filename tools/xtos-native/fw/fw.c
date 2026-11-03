/* XTOS firewall policy manager. nft is an explicit runtime dependency.
 * Only the inet xtos_fw table is owned here. No shell, PATH lookup or host-wide
 * flush: scripts are submitted to nft -f - as single kernel transactions.
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <signal.h>
#include <sys/wait.h>
#include <arpa/inet.h>
#include "xtos_ui.h"

#define TABLE "xtos_fw"
#define INPUT "xtos_input"
#define OUTPUT "xtos_output"
#define USER "xtos_user"
#define CAPTURE (256 * 1024)

/* Bounded scripts are smaller than PIPE_BUF. Output is drained before wait. */
static int nft(const char *script, int listing, char *output, size_t capacity) {
    int in[2], out[2];
    if (pipe(in) < 0) return -1;
    if (pipe(out) < 0) { close(in[0]); close(in[1]); return -1; }
    pid_t pid = fork();
    if (pid < 0) { close(in[0]); close(in[1]); close(out[0]); close(out[1]); return -1; }
    if (!pid) {
        if (dup2(in[0], STDIN_FILENO) < 0 || dup2(out[1], STDOUT_FILENO) < 0) _exit(126);
        close(in[0]); close(in[1]); close(out[0]); close(out[1]);
        char *apply[] = {"nft", "-f", "-", NULL};
        char *list[] = {"nft", "-a", "list", "table", "inet", TABLE, NULL};
        char *tables[] = {"nft", "list", "tables", NULL};
        const char *paths[] = {"/usr/sbin/nft", "/sbin/nft", "/usr/bin/nft", "/bin/nft", NULL};
        for (int i = 0; paths[i]; ++i) execv(paths[i], listing == 2 ? tables : listing ? list : apply);
        perror("fw: exec nft");
        _exit(127);
    }
    close(in[0]); close(out[1]);
    size_t sent = 0, length = script ? strlen(script) : 0;
    int failed = 0;
    while (sent < length) {
        ssize_t n = write(in[1], script + sent, length - sent);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) { failed = 1; break; }
        sent += (size_t)n;
    }
    close(in[1]);
    size_t used = 0;
    char chunk[4096];
    ssize_t n;
    while ((n = read(out[0], chunk, sizeof(chunk))) != 0) {
        if (n < 0) { if (errno == EINTR) continue; failed = 1; break; }
        if (output) {
            if ((size_t)n >= capacity - used) { failed = 1; continue; }
            memcpy(output + used, chunk, (size_t)n); used += (size_t)n;
        } else if (fwrite(chunk, 1, (size_t)n, stdout) != (size_t)n) failed = 1;
    }
    close(out[0]);
    if (output && capacity) output[used] = 0;
    int status;
    while (waitpid(pid, &status, 0) < 0) { if (errno != EINTR) return -1; }
    if (failed || !WIFEXITED(status) || WEXITSTATUS(status)) return -1;
    return 0;
}

static const char create[] =
    "create table inet " TABLE "\n"
    "add chain inet " TABLE " " INPUT " { type filter hook input priority 0; policy drop; }\n"
    "add chain inet " TABLE " " OUTPUT " { type filter hook output priority 0; policy accept; }\n"
    "add chain inet " TABLE " " USER "\n"
    "add rule inet " TABLE " " INPUT " iifname lo accept\n"
    "add rule inet " TABLE " " INPUT " ct state established,related accept\n"
    "add rule inet " TABLE " " INPUT " udp sport 67 udp dport 68 accept\n"
    "add rule inet " TABLE " " INPUT " udp sport 547 udp dport 546 accept\n"
    "add rule inet " TABLE " " INPUT " icmpv6 type { destination-unreachable, packet-too-big, time-exceeded, parameter-problem } accept\n"
    "add rule inet " TABLE " " INPUT " ip6 hoplimit 255 icmpv6 type { nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept\n"
    "add rule inet " TABLE " " INPUT " jump " USER "\n";

/* Fail closed on unknown/modified base rules, including an accept inserted
 * before the owned jump. nft renders symbolic output under LC_ALL=C. */
static int chain_matches(const char *text, const char *name, const char *const *rules) {
    char header[80]; snprintf(header,sizeof(header),"\n\tchain %s {",name);
    const char *p = strstr(text,header);
    if (!p || !(p = strchr(p + 1,'\n'))) return 0;
    for (int i = 0; ; ++i) {
        ++p;
        while (*p == '\t' || *p == ' ') ++p;
        if (!rules[i]) return *p == '}' && p[1] == '\n';
        const char *end = strchr(p,'\n');
        if (!end) return 0;
        const char *handle = strstr(p," # handle ");
        size_t n = (size_t)((handle && handle < end ? handle : end) - p);
        if (strlen(rules[i]) != n || memcmp(p,rules[i],n)) return 0;
        p = end;
    }
}
static int user_chain_matches(const char *text);

static int healthy(const char *text) {
    const char *const input[] = {
        "type filter hook input priority filter; policy drop;",
        "iifname \"lo\" accept", "ct state established,related accept",
        "udp sport 67 udp dport 68 accept", "udp sport 547 udp dport 546 accept",
        "icmpv6 type { destination-unreachable, packet-too-big, time-exceeded, parameter-problem } accept",
        "ip6 hoplimit 255 icmpv6 type { nd-router-advert, nd-neighbor-solicit, nd-neighbor-advert } accept",
        "jump " USER, NULL
    };
    const char *const output[] = {"type filter hook output priority filter; policy accept;",NULL};
    return chain_matches(text,INPUT,input) && chain_matches(text,OUTPUT,output)
        && user_chain_matches(text);
}

static int parse_port(const char *s, uint16_t *port) {
    if (!s || !*s) return -1;
    /* Strict full-token decimal, including overflow rejection. */
    unsigned long n = 0;
    for (; *s; ++s) {
        if (*s < '0' || *s > '9') return -1;
        n = n * 10 + (unsigned)(*s - '0');
        if (n > 65535) return -1;
    }
    if (!n) return -1;
    *port = (uint16_t)n;
    return 0;
}

static int parse_source(const char *s, char *out, size_t size, const char **family) {
    char address[INET6_ADDRSTRLEN], canonical[INET6_ADDRSTRLEN];
    size_t n = strcspn(s, "/");
    if (!n || n >= sizeof(address)) return -1;
    memcpy(address, s, n); address[n] = 0;
    unsigned char raw[16];
    int af = strchr(address, ':') ? AF_INET6 : AF_INET;
    if (inet_pton(af, address, raw) != 1) return -1;
    unsigned bits = af == AF_INET ? 32 : 128;
    if (s[n]) {
        const char *p = s + n + 1;
        if (!*p) return -1;
        unsigned prefix = 0;
        for (; *p; ++p) {
            if (*p < '0' || *p > '9') return -1;
            prefix = prefix * 10 + (unsigned)(*p - '0');
            if (prefix > bits) return -1;
        }
        bits = prefix;
    }
    /* Normalize host bits, so delete identifies the exact same owner scope. */
    unsigned bytes = af == AF_INET ? 4 : 16;
    for (unsigned i = 0; i < bytes; ++i) {
        unsigned remain = bits > i * 8 ? bits - i * 8 : 0;
        raw[i] &= remain >= 8 ? 255 : remain ? (unsigned char)(255 << (8 - remain)) : 0;
    }
    if (!inet_ntop(af, raw, canonical, sizeof(canonical))) return -1;
    if (snprintf(out, size, "%s/%u", canonical, bits) >= (int)size) return -1;
    *family = af == AF_INET ? "ip" : "ip6";
    return 0;
}

/* Recognize only CLI-owned user rules, not arbitrary nft syntax. Rebuild the
 * canonical owner and rendered expression; unknown conditions/verdicts fail. */
static int user_rule_matches(char *line) {
    const char marker[] = " comment \"xtos:rule:";
    char *comment = strstr(line,marker);
    if (!comment) return 0;
    char *proto = comment + strlen(marker);
    char *port_text = strchr(proto,':');
    if (!port_text) return 0;
    *port_text++ = 0;
    if (strcmp(proto,"tcp") && strcmp(proto,"udp")) return 0;
    char *scope = strchr(port_text,':');
    if (!scope) return 0;
    *scope++ = 0;
    uint16_t port;
    if (parse_port(port_text,&port)) return 0;
    char decimal[8]; snprintf(decimal,sizeof(decimal),"%u",port);
    if (strcmp(decimal,port_text)) return 0;
    char *quote = strchr(scope,'\"');
    if (!quote || quote[1]) return 0;
    *quote = 0;
    char source[INET6_ADDRSTRLEN + 8] = "";
    const char *family = NULL;
    if (strcmp(scope,"any")) {
        if (parse_source(scope,source,sizeof(source),&family) || strcmp(source,scope)) return 0;
        /* nft omits /32 and /128 for host matches, not in the owner tag. */
        char *slash = strrchr(source,'/');
        if ((!strcmp(family,"ip") && !strcmp(slash,"/32")) ||
            (!strcmp(family,"ip6") && !strcmp(slash,"/128"))) *slash = 0;
    }
    *comment = 0;
    const char *verdicts[] = {"accept","drop"};
    for (unsigned i = 0; i < sizeof(verdicts)/sizeof(verdicts[0]); ++i) {
        char expected[256];
        snprintf(expected,sizeof(expected),"%s%s%s%s%s dport %u %s",
                 family ? family : "", family ? " saddr " : "", source,
                 family ? " " : "",proto,port,verdicts[i]);
        if (!strcmp(line,expected)) return 1;
    }
    return 0;
}

static int user_chain_matches(const char *text) {
    const char *p = strstr(text,"\n\tchain " USER " {");
    if (!p || !(p = strchr(p + 1,'\n'))) return 0;
    for (;;) {
        ++p;
        while (*p == '\t' || *p == ' ') ++p;
        if (*p == '}' && p[1] == '\n') return 1;
        const char *end = strchr(p,'\n');
        if (!end) return 0;
        const char *handle = strstr(p," # handle ");
        size_t n = (size_t)((handle && handle < end ? handle : end) - p);
        char line[512];
        if (n >= sizeof(line)) return 0;
        memcpy(line,p,n); line[n] = 0;
        if (!user_rule_matches(line)) return 0;
        p = end;
    }
}

static void usage(void) {
    fprintf(stderr, "usage: fw status|enable|list|flush|disable --confirm\n"
            "       fw allow|deny|delete --proto tcp|udp --port N [--from IP/CIDR]\n");
}

int main(int argc, char **argv) {
    if (argc < 2) { usage(); return 2; }
    if (geteuid() != 0) { fprintf(stderr, "fw: requires root\n"); return 1; }
    signal(SIGPIPE, SIG_IGN);
    const char *cmd = argv[1];
    char script[4096], source[INET6_ADDRSTRLEN + 8] = "", tag[160];
    const char *proto = NULL, *family = NULL;
    uint16_t port = 0;
    if (!strcmp(cmd, "allow") || !strcmp(cmd, "deny") || !strcmp(cmd, "delete")) {
        for (int i = 2; i < argc; ++i) {
            if (i + 1 >= argc) goto invalid;
            const char *key = argv[i++], *value = argv[i];
            if (!strcmp(key, "--proto") && !proto && (!strcmp(value,"tcp") || !strcmp(value,"udp"))) proto = value;
            else if (!strcmp(key,"--port") && !port && !parse_port(value,&port)) { }
            else if (!strcmp(key,"--from") && !family && !parse_source(value,source,sizeof(source),&family)) { }
            else goto invalid;
        }
        if (!proto || !port) goto invalid;
        if (!strcmp(cmd,"allow") && !strcmp(proto,"tcp") && port == 22 && !family) {
            fprintf(stderr,"fw: SSH allow requires explicit management --from IP/CIDR\n"); return 1;
        }
        snprintf(tag,sizeof(tag),"xtos:rule:%s:%u:%s",proto,port,family ? source : "any");
        if (!strcmp(cmd,"delete")) {
            char *text = malloc(CAPTURE);
            if (!text) return 1;
            if (nft(NULL,1,text,CAPTURE)) { free(text); goto failed; }
            char *start = strstr(text,"chain " USER " {");
            char *end = start ? strstr(start,"\n\t}") : NULL;
            char needle[192]; snprintf(needle,sizeof(needle),"comment \"%s\" # handle ",tag);
            char *p = start ? strstr(start,needle) : NULL;
            if (!p || !end || p >= end) { free(text); fprintf(stderr,"fw: no matching owned rule\n"); return 1; }
            p += strlen(needle);
            char *tail;
            errno = 0;
            unsigned long long handle = strtoull(p,&tail,10);
            if (errno || p == tail || !handle || (*tail != '\n' && *tail != '\r')) { free(text); goto failed; }
            snprintf(script,sizeof(script),"delete rule inet " TABLE " " USER " handle %llu\n",handle);
            free(text);
        } else {
            snprintf(script,sizeof(script),"%s rule inet " TABLE " " USER " %s%s%s %s dport %u %s comment \"%s\"\n",
                !strcmp(cmd,"deny") ? "insert" : "add", family ? family : "", family ? " saddr " : "", source,
                proto,port,!strcmp(cmd,"allow") ? "accept" : "drop",tag);
        }
        if (nft(script,0,NULL,0)) goto failed;
        printf("FW_%s proto=%s port=%u from=%s\n",!strcmp(cmd,"allow")?"ALLOW":!strcmp(cmd,"deny")?"DENY":"DELETE",proto,port,family?source:"any");
        return 0;
    }
    if (!strcmp(cmd,"disable")) {
        if (argc != 3 || strcmp(argv[2],"--confirm")) goto invalid;
        if (nft("delete table inet " TABLE "\n",0,NULL,0)) goto failed;
        puts("XTOS_FW_DISABLE table=" TABLE " removed"); return 0;
    }
    if (argc != 2) goto invalid;
    if (!strcmp(cmd,"enable")) {
        if (nft(create,0,NULL,0)) goto failed;
        puts("XTOS_FW_ENABLE table=" TABLE " input=" INPUT " output=" OUTPUT " policy=drop"); return 0;
    }
    if (!strcmp(cmd,"flush")) {
        if (nft("flush chain inet " TABLE " " USER "\n",0,NULL,0)) goto failed;
        puts("FW_FLUSH user rules removed; system policy retained"); return 0;
    }
    if (!strcmp(cmd,"list")) return nft(NULL,1,NULL,0) ? 1 : 0;
    if (!strcmp(cmd,"status")) {
        xui_init();
        char *text = malloc(CAPTURE);
        if (!text) return 1;
        /* First distinguish an absent table from a netlink/query failure. */
        if (nft(NULL,2,text,CAPTURE)) {
            free(text);
            XUI_BAD("XTOS_FW_STATUS enabled=false error=query_failed\n"); return 1;
        }
        int present = 0;
        for (char *line = strtok(text,"\n"); line; line = strtok(NULL,"\n"))
            if (!strcmp(line,"table inet " TABLE)) present = 1;
        if (!present) {
            free(text); XUI_WARN("XTOS_FW_STATUS enabled=false table=" TABLE "\n"); return 0;
        }
        if (nft(NULL,1,text,CAPTURE)) {
            free(text);
            XUI_BAD("XTOS_FW_STATUS enabled=false error=query_failed\n"); return 1;
        }
        int enabled = healthy(text); free(text);
        if (enabled) XUI_OK("XTOS_FW_STATUS enabled=true table=" TABLE " policy=drop\n");
        else XUI_BAD("XTOS_FW_STATUS enabled=false table=" TABLE " policy=invalid\n");
        return enabled ? 0 : 1;
    }
invalid:
    fprintf(stderr,"fw: invalid command or option\n"); usage(); return 1;
failed:
    fprintf(stderr,"fw: nft operation failed\n"); return 1;
}
