/* SPDX-License-Identifier: GPL-2.0 WITH Linux-syscall-note */
/*
 * XTOS BCI (Brain-Computer Interface) User API
 * 
 * User-space API for BCI devices
 * Copyright (C) 2026 XTOS Project
 */

#ifndef _UAPI_LINUX_XTOS_BCI_H
#define _UAPI_LINUX_XTOS_BCI_H

#include <linux/types.h>
#include <linux/ioctl.h>

/* ioctl magic number */
#define XTOS_BCI_IOC_MAGIC 'X'

/* Device information structure */
struct xtos_bci_info {
	char name[32];              /* Device name */
	__u32 dev_id;               /* Device ID */
	__u32 num_channels;         /* Number of channels */
	__u32 sampling_rate;        /* Sampling rate (Hz) */
	__u32 resolution_bits;      /* ADC resolution */
	__u32 input_range_mv;       /* Input range (mV, integer) */
	__u32 interface;            /* Interface type */
};

/* Channel configuration */
struct xtos_bci_channel_config {
	__u32 channel;              /* Channel index */
	__u32 enabled;              /* Enable/disable */
	__u32 gain;                 /* Gain setting */
};

/* Statistics */
struct xtos_bci_stats {
	__u64 samples_read;         /* Total samples read */
	__u64 samples_dropped;      /* Samples dropped (overflow) */
	__u64 errors;               /* Error count */
	__u64 overruns;             /* Ring buffer overruns */
};

/* ioctl commands */

/* Get device information */
#define XTOS_BCI_GET_INFO       _IOR(XTOS_BCI_IOC_MAGIC, 1, struct xtos_bci_info)

/* Data stream control */
#define XTOS_BCI_START_STREAM   _IO(XTOS_BCI_IOC_MAGIC, 2)
#define XTOS_BCI_STOP_STREAM    _IO(XTOS_BCI_IOC_MAGIC, 3)

/* Parameter configuration */
#define XTOS_BCI_SET_RATE       _IOW(XTOS_BCI_IOC_MAGIC, 4, __u32)
#define XTOS_BCI_SET_CHANNEL    _IOW(XTOS_BCI_IOC_MAGIC, 5, struct xtos_bci_channel_config)
#define XTOS_BCI_SET_CHANNELS   _IOW(XTOS_BCI_IOC_MAGIC, 6, __u32)

/* Calibration */
#define XTOS_BCI_CALIBRATE      _IO(XTOS_BCI_IOC_MAGIC, 7)
#define XTOS_BCI_CHECK_IMP      _IOR(XTOS_BCI_IOC_MAGIC, 8, __u32[32])

/* Statistics */
#define XTOS_BCI_GET_STATS      _IOR(XTOS_BCI_IOC_MAGIC, 9, struct xtos_bci_stats)
#define XTOS_BCI_RESET_STATS    _IO(XTOS_BCI_IOC_MAGIC, 10)

/* Buffer size control */
#define XTOS_BCI_GET_BUFSIZE    _IOR(XTOS_BCI_IOC_MAGIC, 11, __u32)
#define XTOS_BCI_SET_BUFSIZE    _IOW(XTOS_BCI_IOC_MAGIC, 12, __u32)

#endif /* _UAPI_LINUX_XTOS_BCI_H */
