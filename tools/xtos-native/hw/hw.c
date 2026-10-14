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
#include <sys/utsname.h>
#include <sys/sysinfo.h>
#include "xtos_ui.h"

#define LINE_MAX_LEN 512

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

    if (json) {
        printf("{\n");
        printf("  \"kernel\": \"%s\",\n", uts.release);
        printf("  \"arch\": \"%s\",\n", uts.machine);
        printf("  \"cpus\": %d,\n", si.procs);
        printf("  \"memory_mb\": %lu,\n", si.totalram / (1024 * 1024));
        printf("  \"uptime_seconds\": %ld\n", si.uptime);
        printf("}\n");
    } else {
        xui_init();
        XUI_TITLE("XTOS Hardware Summary\n");
        XUI_LABEL("Kernel:  "); printf("%s %s\n", uts.release, uts.machine);
        XUI_LABEL("CPUs:    "); printf("%d\n", si.procs);
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
    if (strcmp(cmd, "link") == 0) {
        if (argc <= argstart + 1) { usage(); return 2; }
        return show_link(argv[argstart + 1], json);
    }
    if (strcmp(cmd, "summary") == 0) return show_summary(json);

    fprintf(stderr, "hw: unknown command %s\n", cmd);
    usage();
    return 2;
}
