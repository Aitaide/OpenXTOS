/* info: XTOS native system information command (C, static).
 * Reads /etc/xtos-release, /proc, /sys to produce a single system summary.
 * Build: gcc -O2 -Wall -Wextra -static -o xtos-info xtos-info.c
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/utsname.h>
#include <sys/sysinfo.h>
#include <sys/socket.h>
#include <sys/ioctl.h>
#include <net/if.h>
#include <arpa/inet.h>
#include <ifaddrs.h>
#include <time.h>
#include <dirent.h>
#include <ctype.h>

#define LINE_MAX_LEN 512

static char *read_first_line(const char *path, char *buf, size_t size) {
    buf[0] = '\0';
    FILE *f = fopen(path, "r");
    if (!f) return buf;
    if (fgets(buf, (int)size, f)) {
        buf[strcspn(buf, "\n")] = '\0';
        /* Trim trailing whitespace */
        size_t len = strlen(buf);
        while (len > 0 && isspace((unsigned char)buf[len-1])) buf[--len] = '\0';
    }
    fclose(f);
    return buf;
}

static long read_int_from(const char *path) {
    char buf[LINE_MAX_LEN];
    read_first_line(path, buf, sizeof(buf));
    return atol(buf);
}

static long meminfo_value(const char *key) {
    FILE *f = fopen("/proc/meminfo", "r");
    if (!f) return 0;
    char line[LINE_MAX_LEN];
    size_t keylen = strlen(key);
    long result = 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, key, keylen) == 0 && line[keylen] == ':') {
            result = atol(line + keylen + 1);
            break;
        }
    }
    fclose(f);
    return result;
}

static int count_cpus(void) {
    FILE *f = fopen("/proc/cpuinfo", "r");
    if (!f) return 0;
    char line[LINE_MAX_LEN];
    int count = 0;
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "processor", 9) == 0) count++;
    }
    fclose(f);
    return count;
}


static void sanitize_ascii(char *s) {
    for (; *s; s++)
        if ((unsigned char)*s < 0x20 || (unsigned char)*s > 0x7e) *s = '?';
}

/* CPU model name from /proc/cpuinfo */
static void cpu_model(char *out, size_t size) {
    out[0] = '\0';
    FILE *f = fopen("/proc/cpuinfo", "r");
    if (!f) return;
    char line[LINE_MAX_LEN];
    while (fgets(line, sizeof(line), f)) {
        if (strncmp(line, "model name", 10) == 0) {
            char *p = strchr(line, ':');
            if (p) {
                p++;
                while (*p == ' ') p++;
                char *end = strchr(p, '\n');
                if (end) *end = '\0';
                snprintf(out, size, "%s", p);
            }
            break;
        }
    }
    fclose(f);
    sanitize_ascii(out);
}

/* DMI product identity (board/vendor/model) */
static void dmi_value(const char *field, char *out, size_t size) {
    out[0] = '\0';
    char path[512];
    snprintf(path, sizeof(path), "/sys/class/dmi/id/%s", field);
    read_first_line(path, out, size);
    sanitize_ascii(out);
}

/* Count distinct NUMA nodes from /sys/devices/system/node/ */
static int count_numa_nodes(void) {
    DIR *d = opendir("/sys/devices/system/node");
    if (!d) return 0;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(d)))
        if (strncmp(e->d_name, "node", 4) == 0 && isdigit((unsigned char)e->d_name[4])) n++;
    closedir(d);
    return n;
}

/* Block devices with sizes */
static void list_block_devices(int json, int *first) {
    DIR *d = opendir("/sys/block");
    if (!d) return;
    if (!json) printf("Storage:\n");
    else printf(",\n  \"block_devices\": [");
    struct dirent *e;
    int n = 0;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        char path[512], model[LINE_MAX_LEN] = "";
        snprintf(path, sizeof(path), "/sys/block/%s/size", e->d_name);
        long sectors = read_int_from(path);
        snprintf(path, sizeof(path), "/sys/block/%s/removable", e->d_name);
        long removable = read_int_from(path);
        snprintf(path, sizeof(path), "/sys/block/%s/device/model", e->d_name);
        read_first_line(path, model, sizeof(model));
        double gb = (double)sectors * 512 / (1024*1024*1024);
        if (json) {
            printf("%s\n    {\"name\": \"%s\", \"gb\": %.1f, \"removable\": %s, \"model\": \"%s\"}",
                   n ? "," : "", e->d_name, gb, removable ? "true" : "false",
                   model[0] ? model : "-");
        } else {
            printf("  %-8s %8.1f GB  %s  %s\n", e->d_name, gb,
                   removable ? "removable" : "fixed    ", model[0] ? model : "-");
        }
        n++;
    }
    closedir(d);
    if (json) printf("\n  ]");
    (void)first;
}

static void print_json_string(const char *key, const char *value, int comma) {
    printf("%s  \"%s\": \"%s\"", comma ? ",\n" : "", key, value);
}

static void print_json_int(const char *key, long value, int comma) {
    printf("%s  \"%s\": %ld", comma ? ",\n" : "", key, value);
}

/* Parse key=value from xtos-release */
struct xtos_identity {
    char version[128];
    char label[128];
    char release_name[128];
    char image_type[64];
    char build_id[256];
    char source_state[32];
};

static void load_identity(struct xtos_identity *id) {
    memset(id, 0, sizeof(*id));
    FILE *f = fopen("/etc/xtos-release", "r");
    if (f) {
        char line[LINE_MAX_LEN];
        while (fgets(line, sizeof(line), f)) {
            line[strcspn(line, "\n")] = '\0';
            if (strncmp(line, "XTOS_VERSION=", 13) == 0)
                { size_t n = strlen(line + 13); if (n >= sizeof(id->version)) n = sizeof(id->version) - 1; memcpy(id->version, line + 13, n); id->version[n] = '\0'; }
            else if (strncmp(line, "XTOS_LABEL=", 11) == 0)
                { size_t n = strlen(line + 11); if (n >= sizeof(id->label)) n = sizeof(id->label) - 1; memcpy(id->label, line + 11, n); id->label[n] = '\0'; }
            else if (strncmp(line, "XTOS_RELEASE_NAME=", 18) == 0)
                { size_t n = strlen(line + 18); if (n >= sizeof(id->release_name)) n = sizeof(id->release_name) - 1; memcpy(id->release_name, line + 18, n); id->release_name[n] = '\0'; }
            else if (strncmp(line, "XTOS_IMAGE_TYPE=", 16) == 0)
                { size_t n = strlen(line + 16); if (n >= sizeof(id->image_type)) n = sizeof(id->image_type) - 1; memcpy(id->image_type, line + 16, n); id->image_type[n] = '\0'; }
        }
        fclose(f);
    }
    /* Build ID from xtos-build.json (simple grep for build_id) */
    f = fopen("/etc/xtos-build.json", "r");
    if (f) {
        char line[LINE_MAX_LEN];
        while (fgets(line, sizeof(line), f)) {
            char *p = strstr(line, "\"build_id\"");
            if (p) {
                p = strchr(p + 11, '"');
                if (p) {
                    p++;
                    char *end = strchr(p, '"');
                    if (end) {
                        size_t len = (size_t)(end - p);
                        if (len < sizeof(id->build_id))
                            snprintf(id->build_id, len + 1, "%s", p);
                    }
                }
            }
            p = strstr(line, "\"source_state\"");
            if (p) {
                p = strchr(p + 15, '"');
                if (p) {
                    p++;
                    char *end = strchr(p, '"');
                    if (end) {
                        size_t len = (size_t)(end - p);
                        if (len < sizeof(id->source_state))
                            snprintf(id->source_state, len + 1, "%s", p);
                    }
                }
            }
        }
        fclose(f);
    }
}

/* List network interfaces (excluding lo) with IPv4 addresses */
static void list_interfaces(int json, int *first) {
    struct ifaddrs *ifap, *ifa;
    if (getifaddrs(&ifap) != 0) return;

    /* Collect unique interface names */
    char names[16][IFNAMSIZ];
    int nifaces = 0;
    for (ifa = ifap; ifa; ifa = ifa->ifa_next) {
        if (strcmp(ifa->ifa_name, "lo") == 0) continue;
        int found = 0;
        for (int i = 0; i < nifaces; i++) {
            if (strcmp(names[i], ifa->ifa_name) == 0) { found = 1; break; }
        }
        if (!found && nifaces < 16) {
            strncpy(names[nifaces], ifa->ifa_name, IFNAMSIZ - 1);
            nifaces++;
        }
    }

    if (!json) printf("Network:\n");
    else printf(",\n  \"interfaces\": [");

    for (int i = 0; i < nifaces; i++) {
        char syspath[512];
        char buf[LINE_MAX_LEN];
        const char *name = names[i];

        /* State */
        snprintf(syspath, sizeof(syspath), "/sys/class/net/%s/operstate", name);
        const char *state = read_first_line(syspath, buf, sizeof(buf));
        int up = (strcmp(state, "up") == 0);

        /* Speed */
        snprintf(syspath, sizeof(syspath), "/sys/class/net/%s/speed", name);
        long speed = read_int_from(syspath);

        /* Addresses */
        char addrs[512] = "";
        size_t addrs_len = 0;
        for (ifa = ifap; ifa; ifa = ifa->ifa_next) {
            if (strcmp(ifa->ifa_name, name) != 0 || !ifa->ifa_addr) continue;
            if (ifa->ifa_addr->sa_family != AF_INET) continue;
            char addrbuf[INET_ADDRSTRLEN];
            struct sockaddr_in *sin = (struct sockaddr_in *)ifa->ifa_addr;
            inet_ntop(AF_INET, &sin->sin_addr, addrbuf, sizeof(addrbuf));
            if (addrs_len > 0 && addrs_len < sizeof(addrs) - 2) {
                addrs[addrs_len++] = ',';
                addrs[addrs_len++] = ' ';
            }
            size_t alen = strlen(addrbuf);
            if (addrs_len + alen < sizeof(addrs) - 1) {
                memcpy(addrs + addrs_len, addrbuf, alen);
                addrs_len += alen;
            }
        }
        addrs[addrs_len] = '\0';

        if (json) {
            printf("%s\n    {\"name\": \"%s\", \"up\": %s, \"speed_mbps\": %ld, \"addresses\": \"%s\"}",
                   (*first) ? "" : ",", name, up ? "true" : "false", speed, addrs);
            *first = 0;
        } else {
            printf("  %-10s %-6s", name, up ? "up" : "down");
            if (speed > 0) printf("%ldMb/s", speed);
            else printf("%-9s", "-");
            printf(" %s\n", addrs[0] ? addrs : "no address");
        }
    }
    if (json) printf("\n  ]");
    freeifaddrs(ifap);
}

int main(int argc, char **argv) {
    int json = (argc > 1 && strcmp(argv[1], "--json") == 0);

    struct xtos_identity id;
    load_identity(&id);

    struct utsname uts;
    uname(&uts);
    int realtime = (int)read_int_from("/sys/kernel/realtime");
    int cpus = count_cpus();
    long mem_total = meminfo_value("MemTotal");
    long mem_avail = meminfo_value("MemAvailable");
    char hostname[128] = "";
    gethostname(hostname, sizeof(hostname) - 1);
    struct sysinfo si;
    sysinfo(&si);

    if (json) {
        printf("{");
        print_json_string("product_name", "XTOS", 0);
        print_json_string("product_label", id.label, 1);
        print_json_string("version", id.version, 1);
        print_json_string("release_name", id.release_name, 1);
        print_json_string("image_type", id.image_type, 1);
        print_json_string("kernel_release", uts.release, 1);
        print_json_string("kernel_arch", uts.machine, 1);
        printf(",\n  \"realtime_active\": %s", realtime == 1 ? "true" : "false");
        char model[LINE_MAX_LEN];
        cpu_model(model, sizeof(model));
        print_json_string("cpu_model", model, 1);
        print_json_int("cpu_cores", cpus, 1);
        print_json_int("numa_nodes", count_numa_nodes(), 1);
        char vendor[LINE_MAX_LEN], product[LINE_MAX_LEN], board[LINE_MAX_LEN];
        dmi_value("sys_vendor", vendor, sizeof(vendor));
        dmi_value("product_name", product, sizeof(product));
        dmi_value("board_name", board, sizeof(board));
        print_json_string("dmi_vendor", vendor, 1);
        print_json_string("dmi_product", product, 1);
        print_json_string("dmi_board", board, 1);
        print_json_int("memory_total_kb", mem_total, 1);
        print_json_int("memory_avail_kb", mem_avail, 1);
        print_json_string("storage", "Live overlay (RAM), persistence=0", 1);
        print_json_string("hostname", hostname, 1);
        print_json_int("uptime_seconds", si.uptime, 1);
        print_json_string("build_id", id.build_id, 1);
        print_json_string("source_state", id.source_state, 1);
        int first = 1;
        list_block_devices(1, &first);
        list_interfaces(1, &first);
        printf("\n}\n");
    } else {
        if (id.label[0]) printf("%s", id.label);
        else printf("XTOS");
        if (id.version[0]) printf(" %s", id.version);
        if (id.release_name[0]) printf(" (%s)", id.release_name);
        printf("\n");
        printf("Kernel:    %s %s\n", uts.release, uts.machine);
        printf("Realtime:  %s\n", realtime == 1 ? "PREEMPT_RT active" : "inactive");
        char model[LINE_MAX_LEN];
        cpu_model(model, sizeof(model));
        printf("CPU:       %d cores, %s%s%s\n", cpus, uts.machine,
               model[0] ? ", " : "", model[0] ? model : "");
        int nodes = count_numa_nodes();
        if (nodes > 1) printf("NUMA:      %d nodes\n", nodes);
        char vendor[LINE_MAX_LEN], product[LINE_MAX_LEN], board[LINE_MAX_LEN];
        dmi_value("sys_vendor", vendor, sizeof(vendor));
        dmi_value("product_name", product, sizeof(product));
        dmi_value("board_name", board, sizeof(board));
        if (vendor[0] || product[0])
            printf("Model:     %s %s%s%s\n", vendor, product,
                   board[0] ? " (" : "", board[0] ? board : "");
        if (board[0] && !product[0]) printf("Board:     %s\n", board);
        if (mem_total > 0)
            printf("Memory:    %ld MB total, %ld MB available\n", mem_total / 1024, mem_avail / 1024);
        {
            int first = 1;
            list_block_devices(0, &first);
        }
        printf("Storage:   Live overlay (RAM), persistence=0\n");
        printf("Hostname:  %s\n", hostname);
        printf("Uptime:    %ldm %lds\n", si.uptime / 60, si.uptime % 60);
        int first = 1;
        list_interfaces(0, &first);
        if (id.build_id[0])
            printf("Build:     %s (source: %s)\n", id.build_id,
                   id.source_state[0] ? id.source_state : "unknown");
    }
    return 0;
}
