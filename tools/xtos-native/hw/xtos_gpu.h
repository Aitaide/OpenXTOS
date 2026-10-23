#ifndef XTOS_GPU_H
#define XTOS_GPU_H

#include <sys/ioctl.h>
#include <stddef.h>

/*
 * Linux DRM UAPI ABI, kept minimal so XTOS does not depend on libdrm-dev.
 * This is the layout of struct drm_version in <drm/drm.h>; the ioctl is
 * DRM_IOCTL_VERSION (number 0x00, read/write, DRM ioctl type 'd').
 */
struct xtos_drm_version {
    int version_major;
    int version_minor;
    int version_patchlevel;
    size_t name_len;
    char *name;
    size_t date_len;
    char *date;
    size_t desc_len;
    char *desc;
};

#define DRM_IOCTL_BASE 'd'
#define DRM_IOCTL_VERSION _IOWR(DRM_IOCTL_BASE, 0x00, struct xtos_drm_version)

#endif
