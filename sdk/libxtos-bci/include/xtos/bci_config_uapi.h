/* Versioned device readback; independent of Linux internal headers. */
#ifndef XTOS_BCI_CONFIG_UAPI_H
#define XTOS_BCI_CONFIG_UAPI_H

#include <stdint.h>
#include <sys/ioctl.h>

#define XTOS_BCI_CONFIG_VERSION 1u
#define XTOS_BCI_CONFIG_READBACK 1u
#define XTOS_BCI_FIRMWARE_READBACK 2u
#define XTOS_BCI_GAIN_CONTROL 4u
#define XTOS_BCI_IMPEDANCE 8u
#define XTOS_BCI_SELF_TEST 16u

/* Request version=1, everything else zero. Firmware identity is not a unique ID. */
struct xtos_bci_config_v1 {
	uint32_t version;
	uint32_t flags;
	uint32_t channels;
	uint32_t sample_rate_hz;
	uint32_t enabled_mask;
	uint32_t capabilities;
	uint32_t reserved0[2];
	uint64_t generation;
	uint32_t gain[8];
	uint8_t registers[24];
	char firmware[256];
	uint32_t reserved[8];
};

#define XTOS_BCI_GET_CONFIG _IOWR('X', 16, struct xtos_bci_config_v1)

#endif
