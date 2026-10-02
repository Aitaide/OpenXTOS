#ifndef XTOS_BCI_UAPI_H
#define XTOS_BCI_UAPI_H

#include <stdint.h>
#include <sys/ioctl.h>

#define XTOS_BCI_IOC_MAGIC 'X'

struct xtos_bci_info {
    char name[32];
    uint32_t dev_id;
    uint32_t num_channels;
    uint32_t sampling_rate;
    uint32_t resolution_bits;
    uint32_t input_range_mv;   /* mV, integer */
    uint32_t interface;
};

struct xtos_bci_channel_config {
    uint32_t channel;
    uint32_t enabled;
    uint32_t gain;
};

struct xtos_bci_stats {
    uint64_t samples_read;
    uint64_t samples_dropped;
    uint64_t errors;
    uint64_t overruns;
};

#define XTOS_BCI_GET_INFO       _IOR(XTOS_BCI_IOC_MAGIC, 1, struct xtos_bci_info)
#define XTOS_BCI_START_STREAM   _IO(XTOS_BCI_IOC_MAGIC, 2)
#define XTOS_BCI_STOP_STREAM    _IO(XTOS_BCI_IOC_MAGIC, 3)
#define XTOS_BCI_SET_RATE       _IOW(XTOS_BCI_IOC_MAGIC, 4, uint32_t)
#define XTOS_BCI_SET_CHANNEL    _IOW(XTOS_BCI_IOC_MAGIC, 5, struct xtos_bci_channel_config)
#define XTOS_BCI_SET_CHANNELS   _IOW(XTOS_BCI_IOC_MAGIC, 6, uint32_t)
#define XTOS_BCI_CALIBRATE      _IO(XTOS_BCI_IOC_MAGIC, 7)
#define XTOS_BCI_CHECK_IMP      _IOR(XTOS_BCI_IOC_MAGIC, 8, uint32_t[32])
#define XTOS_BCI_GET_STATS      _IOR(XTOS_BCI_IOC_MAGIC, 9, struct xtos_bci_stats)
#define XTOS_BCI_RESET_STATS    _IO(XTOS_BCI_IOC_MAGIC, 10)
#define XTOS_BCI_GET_BUFSIZE    _IOR(XTOS_BCI_IOC_MAGIC, 11, uint32_t)
#define XTOS_BCI_SET_BUFSIZE    _IOW(XTOS_BCI_IOC_MAGIC, 12, uint32_t)

#endif
