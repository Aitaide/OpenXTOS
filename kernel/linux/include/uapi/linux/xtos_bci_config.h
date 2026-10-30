/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
#ifndef XTOS_BCI_CONFIG_UAPI_H
#define XTOS_BCI_CONFIG_UAPI_H

#include <linux/types.h>
#include <linux/ioctl.h>

#define XTOS_BCI_CONFIG_VERSION 1u
#define XTOS_BCI_CONFIG_READBACK 1u
#define XTOS_BCI_FIRMWARE_READBACK 2u
#define XTOS_BCI_GAIN_CONTROL 4u
#define XTOS_BCI_IMPEDANCE 8u
#define XTOS_BCI_SELF_TEST 16u

/* Request version=1, everything else zero. Firmware identity is not a unique ID. */
struct xtos_bci_config_v1 {
	__u32 version;
	__u32 flags;
	__u32 channels;
	__u32 sample_rate_hz;
	__u32 enabled_mask;
	__u32 capabilities;
	__u32 reserved0[2];
	__aligned_u64 generation;
	__u32 gain[8];
	__u8 registers[24];
	char firmware[256];
	__u32 reserved[8];
};

#define XTOS_BCI_GET_CONFIG _IOWR('X', 16, struct xtos_bci_config_v1)

#endif
