/* hw: XTOS native hardware diagnostics (C, static).
 * pci/usb/link/summary via sysfs — replaces lspci, lsusb, ethtool basics.
 * Build: gcc -O2 -Wall -Wextra -Werror -static -o xtos-hw xtos-hw.c
 */
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <ctype.h>
#include <dirent.h>
#include <errno.h>
#include <limits.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <sys/utsname.h>
#include <sys/sysinfo.h>
#include "xtos_gpu.h"
#include "xtos_ui.h"

#define LINE_MAX_LEN 512

#ifndef XTOS_SYS_CLASS_DRM
#define XTOS_SYS_CLASS_DRM "/sys/class/drm"
#endif
#ifndef XTOS_DEV_DRI
#define XTOS_DEV_DRI "/dev/dri"
#endif
#ifndef XTOS_PROC_IRQ
#define XTOS_PROC_IRQ "/proc/irq"
#endif

#define GPU_DRIVER_MAX 65536
#define GPU_PATH_MAX 4096

struct gpu_irq {
    int number;
    char requested[LINE_MAX_LEN];
    char effective[LINE_MAX_LEN];
};

struct gpu_info {
    char name[NAME_MAX + 1];
    char node_path[GPU_PATH_MAX];
    char open_path[GPU_PATH_MAX];
    int character_device;
    int openable;
    int open_errno;
    char driver[GPU_DRIVER_MAX];
    int have_pci;
    char pci_address[NAME_MAX + 1];
    char pci_vendor[LINE_MAX_LEN];
    char pci_device[LINE_MAX_LEN];
    struct gpu_irq *irqs;
    size_t irq_count;
};

static int join_path(char *dst, size_t cap, const char *left, const char *right) {
    size_t left_len = strlen(left);
    size_t right_len = strlen(right);
    int slash = left_len != 0 && left[left_len - 1] != '/';
    if (left_len + (size_t)slash + right_len + 1 > cap) return -1;
    memcpy(dst, left, left_len);
    if (slash) dst[left_len++] = '/';
    memcpy(dst + left_len, right, right_len);
    dst[left_len + right_len] = '\0';
    return 0;
}

static void set_text(char *dst, size_t cap, const char *src) {
    size_t n = strlen(src);
    if (n >= cap) n = cap - 1;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static int gpu_node_name(const char *name) {
    const char *digits = NULL;
    if (strncmp(name, "card", 4) == 0) digits = name + 4;
    else if (strncmp(name, "renderD", 7) == 0) digits = name + 7;
    else return 0;
    if (*digits == '\0') return 0;
    for (const char *p = digits; *p != '\0'; ++p) {
        if (!isdigit((unsigned char)*p)) return 0;
    }
    return 1;
}

static int compare_names(const void *left, const void *right) {
    const char *const *a = left;
    const char *const *b = right;
    return strcmp(*a, *b);
}

static int compare_irqs(const void *left, const void *right) {
    const struct gpu_irq *a = left;
    const struct gpu_irq *b = right;
    return (a->number > b->number) - (a->number < b->number);
}

static void free_gpu_info(struct gpu_info *info) {
    free(info->irqs);
    info->irqs = NULL;
    info->irq_count = 0;
}

static char *read_first_line(const char *path, char *buf, size_t size) {
    buf[0] = '\0';
    FILE *f = fopen(path, "r");
    if (!f) return buf;
    if (fgets(buf, (int)size, f)) {
        buf[strcspn(buf, "\n")] = '\0';
        size_t len = strlen(buf);
        while (len > 0 && isspace((unsigned char)buf[len-1])) buf[--len] = '\0';
    }
    fclose(f);
    return buf;
}

static int parse_nonnegative_int(const char *text, int *value) {
    char *end = NULL;
    long parsed;
    if (text[0] == '\0') return -1;
    errno = 0;
    parsed = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || parsed < 0 || parsed > INT_MAX) {
        return -1;
    }
    *value = (int)parsed;
    return 0;
}

static int add_gpu_irq(struct gpu_info *info, int irq) {
    struct gpu_irq *new_irqs;
    for (size_t i = 0; i < info->irq_count; ++i) {
        if (info->irqs[i].number == irq) return 0;
    }
    new_irqs = realloc(info->irqs, (info->irq_count + 1) * sizeof(*new_irqs));
    if (!new_irqs) return -1;
    info->irqs = new_irqs;
    info->irqs[info->irq_count].number = irq;
    set_text(info->irqs[info->irq_count].requested, LINE_MAX_LEN, "unknown");
    set_text(info->irqs[info->irq_count].effective, LINE_MAX_LEN, "unknown");
    info->irq_count++;
    return 0;
}

static void read_affinity_value(char *destination, const char *root,
                                const char *list_leaf, const char *mask_leaf) {
    char path[GPU_PATH_MAX];
    char value[LINE_MAX_LEN];
    if (join_path(path, sizeof(path), root, list_leaf) != 0) return;
    read_first_line(path, value, sizeof(value));
    if (value[0] != '\0') {
        set_text(destination, LINE_MAX_LEN, value);
        return;
    }
    if (join_path(path, sizeof(path), root, mask_leaf) != 0) return;
    read_first_line(path, value, sizeof(value));
    size_t length = strlen(value);
    if (length == 0 || length >= LINE_MAX_LEN - 5) return;
    memcpy(destination, "mask:", 5);
    memcpy(destination + 5, value, length + 1);
}

static void read_irq_affinity(struct gpu_irq *irq) {
    char irq_name[32];
    char irq_root[GPU_PATH_MAX];
    int written = snprintf(irq_name, sizeof(irq_name), "%d", irq->number);
    if (written < 0 || (size_t)written >= sizeof(irq_name)
            || join_path(irq_root, sizeof(irq_root), XTOS_PROC_IRQ, irq_name) != 0) {
        return;
    }
    read_affinity_value(irq->requested, irq_root, "smp_affinity_list", "smp_affinity");
    read_affinity_value(irq->effective, irq_root, "effective_affinity_list", "effective_affinity");
}

static int collect_gpu_irqs(struct gpu_info *info) {
    char class_path[GPU_PATH_MAX];
    char device_path[GPU_PATH_MAX];
    char msi_path[GPU_PATH_MAX];
    char irq_path[GPU_PATH_MAX];
    char irq_value[LINE_MAX_LEN];
    DIR *msi_dir;
    struct dirent *entry;
    int irq;

    if (strncmp(info->name, "card", 4) != 0
            || join_path(class_path, sizeof(class_path), XTOS_SYS_CLASS_DRM, info->name) != 0
            || join_path(device_path, sizeof(device_path), class_path, "device") != 0) {
        return 0;
    }
    if (join_path(msi_path, sizeof(msi_path), device_path, "msi_irqs") == 0) {
        msi_dir = opendir(msi_path);
        if (msi_dir) {
            for (;;) {
                errno = 0;
                entry = readdir(msi_dir);
                if (!entry) {
                    int error = errno;
                    closedir(msi_dir);
                    if (error) {
                        errno = error;
                        return -1;
                    }
                    break;
                }
                if (entry->d_name[0] == '.') continue;
                if (parse_nonnegative_int(entry->d_name, &irq) == 0
                        && add_gpu_irq(info, irq) != 0) {
                    int error = errno;
                    closedir(msi_dir);
                    errno = error;
                    return -1;
                }
            }
        }
    }
    if (join_path(irq_path, sizeof(irq_path), device_path, "irq") == 0) {
        read_first_line(irq_path, irq_value, sizeof(irq_value));
        if (parse_nonnegative_int(irq_value, &irq) == 0) {
            if (add_gpu_irq(info, irq) != 0) return -1;
        }
    }
    if (info->irq_count > 1) {
        qsort(info->irqs, info->irq_count, sizeof(*info->irqs), compare_irqs);
    }
    for (size_t i = 0; i < info->irq_count; ++i) {
        read_irq_affinity(&info->irqs[i]);
    }
    return 0;
}

static int is_pci_address(const char *name) {
    int saw_colon = 0;
    int saw_dot = 0;
    if (name[0] == '\0' || strlen(name) > NAME_MAX) return 0;
    for (const char *p = name; *p != '\0'; ++p) {
        if (*p == ':') saw_colon = 1;
        else if (*p == '.') saw_dot = 1;
        else if (!isxdigit((unsigned char)*p)) return 0;
    }
    return saw_colon && saw_dot;
}

static void read_pci_metadata(struct gpu_info *info) {
    char class_path[GPU_PATH_MAX];
    char device_link[GPU_PATH_MAX];
    char resolved[PATH_MAX];
    char path[GPU_PATH_MAX];
    char *base;

    if (join_path(class_path, sizeof(class_path), XTOS_SYS_CLASS_DRM, info->name) != 0
            || join_path(device_link, sizeof(device_link), class_path, "device") != 0
            || realpath(device_link, resolved) == NULL) {
        return;
    }
    base = strrchr(resolved, '/');
    if (!base || !is_pci_address(base + 1)) return;
    set_text(info->pci_address, sizeof(info->pci_address), base + 1);
    if (join_path(path, sizeof(path), resolved, "vendor") != 0) return;
    read_first_line(path, info->pci_vendor, sizeof(info->pci_vendor));
    if (join_path(path, sizeof(path), resolved, "device") != 0) return;
    read_first_line(path, info->pci_device, sizeof(info->pci_device));
    if (info->pci_vendor[0] != '\0' && info->pci_device[0] != '\0') {
        info->have_pci = 1;
    }
}

static void drm_driver_name(int fd, char *driver, size_t driver_cap) {
    struct xtos_drm_version version;
    char *name;
    size_t length;

    memset(&version, 0, sizeof(version));
    if (ioctl(fd, DRM_IOCTL_VERSION, &version) != 0
            || version.name_len == 0 || version.name_len >= GPU_DRIVER_MAX) {
        return;
    }
    length = version.name_len;
    name = calloc(length + 1, sizeof(*name));
    if (!name) return;
    memset(&version, 0, sizeof(version));
    version.name_len = length;
    version.name = name;
    if (ioctl(fd, DRM_IOCTL_VERSION, &version) == 0) {
        if (version.name_len <= length) {
            name[version.name_len] = '\0';
            if (name[0] != '\0') set_text(driver, driver_cap, name);
        }
    }
    free(name);
}

static int inspect_gpu(struct gpu_info *info) {
    struct stat st;
    int fd;

    set_text(info->driver, sizeof(info->driver), "unknown");
    if (stat(info->open_path, &st) == 0 && S_ISCHR(st.st_mode)) {
        info->character_device = 1;
    }
    fd = open(info->open_path, O_RDWR | O_CLOEXEC);
    if (fd < 0) {
        info->open_errno = errno;
    } else {
        info->openable = 1;
        if (info->character_device) drm_driver_name(fd, info->driver, sizeof(info->driver));
        close(fd);
    }
    read_pci_metadata(info);
    return collect_gpu_irqs(info);
}

static void json_string(const char *text) {
    const unsigned char *p = (const unsigned char *)text;
    putchar('"');
    while (*p) {
        unsigned char c = *p++;
        switch (c) {
            case '"': fputs("\\\"", stdout); break;
            case '\\': fputs("\\\\", stdout); break;
            case '\b': fputs("\\b", stdout); break;
            case '\f': fputs("\\f", stdout); break;
            case '\n': fputs("\\n", stdout); break;
            case '\r': fputs("\\r", stdout); break;
            case '\t': fputs("\\t", stdout); break;
            default:
                if (c < 0x20 || c >= 0x7f) printf("\\u%04x", (unsigned int)c);
                else putchar(c);
                break;
        }
    }
    putchar('"');
}

static void output_gpu_json(const struct gpu_info *info, int *first) {
    if (!*first) puts(",");
    *first = 0;
    printf("  {\"node\":");
    json_string(info->node_path);
    printf(",\"character_device\":%s,\"drm_driver\":",
           info->character_device ? "true" : "false");
    json_string(info->driver);
    printf(",\"openable\":%s,\"open_errno\":",
           info->openable ? "true" : "false");
    if (info->openable) puts("null");
    else printf("%d", info->open_errno);
    if (info->have_pci) {
        printf(",\"pci\":{\"address\":");
        json_string(info->pci_address);
        printf(",\"vendor\":");
        json_string(info->pci_vendor);
        printf(",\"device\":");
        json_string(info->pci_device);
        putchar('}');
    }
    printf(",\"irqs\":[");
    for (size_t i = 0; i < info->irq_count; ++i) {
        if (i != 0) putchar(',');
        printf("{\"irq\":%d,\"requested_affinity\":", info->irqs[i].number);
        json_string(info->irqs[i].requested);
        printf(",\"effective_affinity\":");
        json_string(info->irqs[i].effective);
        putchar('}');
    }
    puts("]}");
}

static void output_gpu_human(const struct gpu_info *info) {
    printf("Node:             %s\n", info->node_path);
    printf("Character device: %s\n", info->character_device ? "yes" : "no");
    printf("DRM driver:       %s\n", info->driver);
    if (info->openable) printf("O_RDWR access:    yes\n");
    else printf("O_RDWR access:    no (errno=%d: %s)\n", info->open_errno,
                strerror(info->open_errno));
    if (info->have_pci) {
        printf("PCI:              %s vendor=%s device=%s\n",
               info->pci_address, info->pci_vendor, info->pci_device);
    }
    if (info->irq_count == 0) printf("IRQ affinity:     unknown/not exposed\n");
    for (size_t i = 0; i < info->irq_count; ++i) {
        printf("IRQ %d:            requested=%s effective=%s\n",
               info->irqs[i].number, info->irqs[i].requested,
               info->irqs[i].effective);
    }
}

static int list_gpu(int json) {
    DIR *dir = opendir(XTOS_SYS_CLASS_DRM);
    char **names = NULL;
    size_t count = 0;
    struct dirent *entry;
    int first = 1;
    int error = 0;

    if (!dir && errno != ENOENT) {
        int error = errno;
        fprintf(stderr, "hw gpu: cannot open %s (errno=%d: %s)\n",
                XTOS_SYS_CLASS_DRM, error, strerror(error));
        return 1;
    }
    if (dir) {
        for (;;) {
            char **grown;
            errno = 0;
            entry = readdir(dir);
            if (!entry) {
                error = errno;
                break;
            }
            if (!gpu_node_name(entry->d_name)) continue;
            grown = realloc(names, (count + 1) * sizeof(*grown));
            if (!grown) {
                error = errno;
                break;
            }
            names = grown;
            names[count] = strdup(entry->d_name);
            if (!names[count]) {
                error = errno;
                break;
            }
            count++;
        }
        closedir(dir);
    }
    if (error) {
        for (size_t i = 0; i < count; ++i) free(names[i]);
        free(names);
        fprintf(stderr, "hw gpu: cannot enumerate %s (errno=%d: %s)\n",
                XTOS_SYS_CLASS_DRM, error, strerror(error));
        return 1;
    }
    if (json) putchar('[');
    else {
        xui_init();
        XUI_TITLE("XTOS GPU diagnostics (read-only)\n");
    }
    if (count > 1) qsort(names, count, sizeof(*names), compare_names);
    for (size_t i = 0; i < count; ++i) {
        struct gpu_info info;
        memset(&info, 0, sizeof(info));
        set_text(info.name, sizeof(info.name), names[i]);
        if (join_path(info.node_path, sizeof(info.node_path), "/dev/dri", info.name) != 0
                || join_path(info.open_path, sizeof(info.open_path), XTOS_DEV_DRI,
                             info.name) != 0) {
            free(names[i]);
            continue;
        }
        if (inspect_gpu(&info) != 0) {
            error = errno;
            free_gpu_info(&info);
            for (size_t j = i; j < count; ++j) free(names[j]);
            free(names);
            fprintf(stderr, "hw gpu: cannot collect IRQs (errno=%d: %s)\n",
                    error, strerror(error));
            return 1;
        }
        if (json) output_gpu_json(&info, &first);
        else {
            if (!first) putchar('\n');
            output_gpu_human(&info);
        }
        first = 0;
        free_gpu_info(&info);
        free(names[i]);
    }
    free(names);
    if (json) puts("]");
    else if (first) puts("XTOS_HW_GPU not present");
    else puts("XTOS_HW_GPU diagnostics complete; inference readiness and real-time tuning not assessed");
    return 0;
}

static long read_int(const char *path) {
    char buf[LINE_MAX_LEN];
    read_first_line(path, buf, sizeof(buf));
    return atol(buf);
}

static int list_pci(int json) {
    DIR *dir = opendir("/sys/bus/pci/devices");
    if (!dir) {
        fprintf(stderr, "hw: cannot open /sys/bus/pci/devices\n");
        return 1;
    }
    if (!json) printf("%s%s%s\n", XUI_LABEL_S, "PCI_ADDRESS  VENDOR  DEVICE  CLASS     DRIVER", XUI_RESET_S);
    else printf("[");
    struct dirent *entry;
    int first = 1;
    while ((entry = readdir(dir))) {
        if (entry->d_name[0] == '.') continue;
        char base[512];
        snprintf(base, sizeof(base), "/sys/bus/pci/devices/%s", entry->d_name);

        char vendor[LINE_MAX_LEN], device[LINE_MAX_LEN];
        char class_name[LINE_MAX_LEN] = "", driver[LINE_MAX_LEN] = "";
        {
            char path[600];
            snprintf(path, sizeof(path), "%s/vendor", base);
            read_first_line(path, vendor, sizeof(vendor));
            snprintf(path, sizeof(path), "%s/device", base);
            read_first_line(path, device, sizeof(device));
            snprintf(path, sizeof(path), "%s/class", base);
            char classcode[LINE_MAX_LEN];
            read_first_line(path, classcode, sizeof(classcode));
            /* Parse class: 0x020000 -> class code 02 (network) */
            unsigned long cc = strtoul(classcode, NULL, 0);
            const char *classname = "";
            switch ((cc >> 16) & 0xFF) {
                case 0x00: classname = "legacy"; break;
                case 0x01: classname = "storage"; break;
                case 0x02: classname = "network"; break;
                case 0x03: classname = "display"; break;
                case 0x04: classname = "multimedia"; break;
                case 0x05: classname = "memory"; break;
                case 0x06: classname = "bridge"; break;
                case 0x07: classname = "comm"; break;
                case 0x08: classname = "system"; break;
                case 0x09: classname = "input"; break;
                case 0x0C: classname = "serial"; break;
                case 0x0B: classname = "processor"; break;
                default: classname = "other"; break;
            }
            snprintf(class_name, sizeof(class_name), "%s", classname);
            snprintf(path, sizeof(path), "%s/driver", base);
            /* driver is a symlink; readlink to get name */
            char link[512];
            ssize_t n = readlink(path, link, sizeof(link) - 1);
            if (n > 0) { link[n] = '\0'; snprintf(driver, sizeof(driver), "%s", strrchr(link, '/') + 1); }
            else driver[0] = '\0';
        }
        if (json) {
            printf("%s\n  {\"address\": \"%s\", \"vendor\": \"%s\", \"device\": \"%s\", \"class\": \"%s\", \"driver\": \"%s\"}",
                   first ? "" : ",", entry->d_name, vendor, device, class_name, driver);
            first = 0;
        } else {
            printf("%-12s %-7s %-7s %-9s %s\n", entry->d_name, vendor, device, class_name, driver);
        }
    }
    closedir(dir);
    if (json) printf("\n]\n");
    return 0;
}

static int list_usb(int json) {
    DIR *dir = opendir("/sys/bus/usb/devices");
    if (!dir) {
        fprintf(stderr, "hw: cannot open /sys/bus/usb/devices\n");
        return 1;
    }
    if (!json) printf("%s%s%s\n", XUI_LABEL_S, "USB_PATH  VENDOR  PRODUCT  BUS  DEV  CLASS", XUI_RESET_S);
    else printf("[");
    struct dirent *entry;
    int first = 1;
    while ((entry = readdir(dir))) {
        /* Only top-level devices (not interface entries with ':') */
        if (entry->d_name[0] == '.' || strchr(entry->d_name, ':')) continue;
        char base[512];
        snprintf(base, sizeof(base), "/sys/bus/usb/devices/%s", entry->d_name);

        char vendor[LINE_MAX_LEN] = "", product[LINE_MAX_LEN] = "";
        char busnum[LINE_MAX_LEN] = "", devnum[LINE_MAX_LEN] = "";
        char devclass[LINE_MAX_LEN] = "";
        {
            char path[600];
            snprintf(path, sizeof(path), "%s/idVendor", base);
            read_first_line(path, vendor, sizeof(vendor));
            snprintf(path, sizeof(path), "%s/idProduct", base);
            read_first_line(path, product, sizeof(product));
            snprintf(path, sizeof(path), "%s/busnum", base);
            read_first_line(path, busnum, sizeof(busnum));
            snprintf(path, sizeof(path), "%s/devnum", base);
            read_first_line(path, devnum, sizeof(devnum));
            snprintf(path, sizeof(path), "%s/bDeviceClass", base);
            char rawclass[LINE_MAX_LEN];
            read_first_line(path, rawclass, sizeof(rawclass));
            unsigned long dc = strtoul(rawclass, NULL, 16);
            if (dc == 0x00) snprintf(devclass, sizeof(devclass), "if-specific");
            else if (dc == 0x09) snprintf(devclass, sizeof(devclass), "hub");
            else if (dc == 0x08) snprintf(devclass, sizeof(devclass), "storage");
            else if (dc == 0x03) snprintf(devclass, sizeof(devclass), "hid");
            else if (dc == 0x02) snprintf(devclass, sizeof(devclass), "comm");
            else if (dc == 0xE0) snprintf(devclass, sizeof(devclass), "wireless");
            else if (dc == 0xEF) snprintf(devclass, sizeof(devclass), "misc");
            else snprintf(devclass, sizeof(devclass), "0x%02lx", dc);
        }
        if (vendor[0] == '\0') continue;
        if (json) {
            printf("%s\n  {\"path\": \"%s\", \"vendor\": \"%s\", \"product\": \"%s\", \"bus\": \"%s\", \"dev\": \"%s\", \"class\": \"%s\"}",
                   first ? "" : ",", entry->d_name, vendor, product, busnum, devnum, devclass);
            first = 0;
        } else {
            printf("%-9s %-7s %-8s %-4s %-4s %s\n", entry->d_name, vendor, product, busnum, devnum, devclass);
        }
    }
    closedir(dir);
    if (json) printf("\n]\n");
    return 0;
}

static int show_link(const char *ifname, int json) {
    char base[256];
    snprintf(base, sizeof(base), "/sys/class/net/%s", ifname);
    if (access(base, F_OK) != 0) {
        fprintf(stderr, "hw: interface %s does not exist\n", ifname);
        return 1;
    }
    char path[512];
    snprintf(path, sizeof(path), "%s/operstate", base);
    char operstate[LINE_MAX_LEN];
    read_first_line(path, operstate, sizeof(operstate));
    snprintf(path, sizeof(path), "%s/speed", base);
    long speed = read_int(path);
    snprintf(path, sizeof(path), "%s/carrier", base);
    long carrier = read_int(path);
    snprintf(path, sizeof(path), "%s/duplex", base);
    char duplex[LINE_MAX_LEN];
    read_first_line(path, duplex, sizeof(duplex));
    snprintf(path, sizeof(path), "%s/address", base);
    char mac[LINE_MAX_LEN];
    read_first_line(path, mac, sizeof(mac));
    snprintf(path, sizeof(path), "%s/mtu", base);
    long mtu = read_int(path);

    if (json) {
        printf("{\n  \"interface\": \"%s\",\n  \"oper_state\": \"%s\",\n  \"speed_mbps\": %ld,\n  \"carrier\": %s,\n  \"duplex\": \"%s\",\n  \"mac\": \"%s\",\n  \"mtu\": %ld\n}\n",
               ifname, operstate, speed, carrier == 1 ? "true" : "false", duplex, mac, mtu);
    } else {
        xui_init();
        XUI_LABEL("Interface:  "); printf("%s\n", ifname);
        XUI_LABEL("State:      ");
        if (strcmp(operstate, "up") == 0) XUI_OK("%s\n", operstate);
        else XUI_DIM("%s\n", operstate);
        XUI_LABEL("Speed:      "); printf("%ld Mbps\n", speed);
        XUI_LABEL("Duplex:     "); printf("%s\n", duplex);
        XUI_LABEL("Carrier:    ");
        if (carrier == 1) XUI_OK("yes\n");
        else XUI_BAD("no\n");
        XUI_LABEL("MAC:        "); printf("%s\n", mac);
        XUI_LABEL("MTU:        "); printf("%ld\n", mtu);
    }
    return 0;
}

static int show_summary(int json) {
    struct utsname uts;
    uname(&uts);
    struct sysinfo si;
    sysinfo(&si);
    long online_cpus = sysconf(_SC_NPROCESSORS_ONLN);
    if (online_cpus < 1) online_cpus = 1;

    if (json) {
        printf("{\n");
        printf("  \"kernel\": \"%s\",\n", uts.release);
        printf("  \"arch\": \"%s\",\n", uts.machine);
        printf("  \"cpus\": %ld,\n", online_cpus);
        printf("  \"memory_mb\": %lu,\n", si.totalram / (1024 * 1024));
        printf("  \"uptime_seconds\": %ld\n", si.uptime);
        printf("}\n");
    } else {
        xui_init();
        XUI_TITLE("XTOS Hardware Summary\n");
        XUI_LABEL("Kernel:  "); printf("%s %s\n", uts.release, uts.machine);
        XUI_LABEL("CPUs:    "); printf("%ld\n", online_cpus);
        XUI_LABEL("Memory:  "); printf("%lu MB\n", si.totalram / (1024 * 1024));
        XUI_LABEL("Uptime:  "); printf("%lds\n", si.uptime);

        /* Block devices */
        printf("\n%s%s%s\n", XUI_LABEL_S, "Block devices:", XUI_RESET_S);
        DIR *dir = opendir("/sys/block");
        if (dir) {
            struct dirent *entry;
            while ((entry = readdir(dir))) {
                if (entry->d_name[0] == '.') continue;
                char path[512];
                snprintf(path, sizeof(path), "/sys/block/%s/size", entry->d_name);
                long sectors = read_int(path);
                snprintf(path, sizeof(path), "/sys/block/%s/removable", entry->d_name);
                long removable = read_int(path);
                snprintf(path, sizeof(path), "/sys/block/%s/device/model", entry->d_name);
                char model[LINE_MAX_LEN] = "";
                read_first_line(path, model, sizeof(model));
                if (!model[0]) snprintf(model, sizeof(model), "-");
                printf("  %-8s %6.1f GB  %s  %s\n", entry->d_name,
                       (double)sectors * 512 / (1024*1024*1024),
                       removable ? "removable" : "fixed    ", model);
            }
            closedir(dir);
        }

        /* Network interfaces */
        printf("\n%s%s%s\n", XUI_LABEL_S, "Network:", XUI_RESET_S);
        dir = opendir("/sys/class/net");
        if (dir) {
            struct dirent *entry;
            while ((entry = readdir(dir))) {
                if (entry->d_name[0] == '.') continue;
                char path[512];
                snprintf(path, sizeof(path), "/sys/class/net/%s/operstate", entry->d_name);
                char state[LINE_MAX_LEN];
                read_first_line(path, state, sizeof(state));
                snprintf(path, sizeof(path), "/sys/class/net/%s/speed", entry->d_name);
                long speed = read_int(path);
                char speedbuf[LINE_MAX_LEN] = "";
                if (speed > 0) snprintf(speedbuf, sizeof(speedbuf), "%ldMb/s", speed);
                printf("  %-12s %-8s %s\n", entry->d_name, state, speedbuf);
            }
            closedir(dir);
        }
    }
    return 0;
}

static void usage(void) {
    fprintf(stderr,
        "usage: hw <command>\n"
        "  hw pci              list PCI devices\n"
        "  hw usb              list USB devices\n"
        "  hw gpu              read-only DRM nodes, driver, access and IRQs\n"
        "  hw link <interface> show link details\n"
        "  hw summary          system + storage + network summary\n"
        "  --json                   machine-readable output\n");
}

int main(int argc, char **argv) {
    int json = 0;
    int argstart = 1;
    if (argc > 1 && strcmp(argv[1], "--json") == 0) { json = 1; argstart = 2; }

    if (argc <= argstart) { usage(); return 2; }

    const char *cmd = argv[argstart];
    if (strcmp(cmd, "pci") == 0) return list_pci(json);
    if (strcmp(cmd, "usb") == 0) return list_usb(json);
    if (strcmp(cmd, "gpu") == 0) return list_gpu(json);
    if (strcmp(cmd, "link") == 0) {
        if (argc <= argstart + 1) { usage(); return 2; }
        return show_link(argv[argstart + 1], json);
    }
    if (strcmp(cmd, "summary") == 0) return show_summary(json);

    fprintf(stderr, "hw: unknown command %s\n", cmd);
    usage();
    return 2;
}
