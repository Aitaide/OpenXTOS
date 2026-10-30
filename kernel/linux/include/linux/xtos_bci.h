/* SPDX-License-Identifier: GPL-2.0 */
/*
 * XTOS BCI (Brain-Computer Interface) Driver Framework
 * 
 * Unified abstraction layer for EEG/brain sensing devices
 * Copyright (C) 2026 XTOS Project
 */

#ifndef _LINUX_XTOS_BCI_H
#define _LINUX_XTOS_BCI_H

#include <linux/types.h>
#include <linux/device.h>
#include <linux/cdev.h>
#include <linux/list.h>
#include <linux/spinlock.h>
#include <linux/wait.h>
#include <linux/dma-mapping.h>
#include <linux/mutex.h>
#include <linux/kref.h>

/* BCI device interface types */
enum xtos_bci_interface {
	XTOS_BCI_SERIAL,
	XTOS_BCI_USB,
	XTOS_BCI_BLUETOOTH,
	XTOS_BCI_NETWORK
};

/* Forward declarations */
struct xtos_bci_dev;
struct xtos_bci_ringbuf;
struct xtos_bci_config_v1;
struct xtos_bci_frame_v1;
struct xtos_bci_frame_ring;

/* Device operation callbacks */
struct xtos_bci_device_ops {
	/* Wake in-flight operations before unregister waits for op_lock. */
	void (*cancel)(struct xtos_bci_dev *dev);
	/* Initialization and cleanup */
	int (*init)(struct xtos_bci_dev *dev);
	void (*cleanup)(struct xtos_bci_dev *dev);
	
	/* Data acquisition */
	int (*start_stream)(struct xtos_bci_dev *dev);
	int (*stop_stream)(struct xtos_bci_dev *dev);
	int (*read_samples)(struct xtos_bci_dev *dev,
	                    s32 *buffer,
	                    size_t num_samples);
	
	/* Configuration. Cyton accepts 250Hz only as a stopped readback assertion;
	 * its firmware rate command resets unrelated ADS settings. */
	int (*set_sampling_rate)(struct xtos_bci_dev *dev, int rate);
	int (*set_channel_enable)(struct xtos_bci_dev *dev, 
	                          int channel, 
	                          bool enable);
	int (*set_gain)(struct xtos_bci_dev *dev, 
	                int channel, 
	                int gain);
	
	int (*set_channel_config)(struct xtos_bci_dev *dev, int channel,
				  bool enabled, int gain);
	int (*get_config)(struct xtos_bci_dev *dev,
			  struct xtos_bci_config_v1 *config);

	/* Calibration */
	int (*calibrate)(struct xtos_bci_dev *dev);
	int (*impedance_check)(struct xtos_bci_dev *dev, 
	                      __u32 *impedances_kohm);
};

/* BCI device descriptor */
struct xtos_bci_dev {
	/* Device identification */
	char name[32];              /* Device name (e.g., "OpenBCI Cyton") */
	int dev_id;                 /* Unique device ID */
	
	/* Hardware parameters */
	int num_channels;           /* Number of channels */
	int sampling_rate;          /* Sampling rate (Hz) */
	int resolution_bits;        /* ADC resolution (bits) */
	int input_range_mv;         /* Input range (millivolts, integer) */
	
	/* Connection information */
	enum xtos_bci_interface interface;
	void *hw_private;           /* Hardware private data */
	
	/* Operation callbacks */
	const struct xtos_bci_device_ops *ops;
	struct module *owner;       /* Callback/free code, pinned by each open FD */
	void (*release)(struct xtos_bci_dev *dev); /* Final allocation destructor */
	struct kref ref;
	struct mutex op_lock;       /* Controls/open/cleanup, never held while waiting */
	struct mutex read_lock;     /* Serialize read snapshot/commit */
	bool disconnected;
	bool registered;
	
	/* Ring buffer */
	struct xtos_bci_ringbuf *ringbuf;
	struct xtos_bci_frame_ring *frames; /* CPU-owned, final device put frees. */
	
	/* Character device */
	struct cdev *cdev;          /* Independently refcounted by VFS */
	struct device *device;
	dev_t devt;                 /* Device number (major:minor) */
	
	/* Statistics: RX and core GET/RESET_STATS all hold lock below. */
	unsigned long samples_read;
	unsigned long samples_dropped;
	unsigned long errors;
	
	/* State */
	bool opened;
	bool streaming;
	int acquisition_error;       /* Negative errno; buffered bytes drain first. */
	spinlock_t lock;
	
	/* Global device list */
	struct list_head list;
};

/* Zero-copy ring buffer */
struct xtos_bci_ringbuf {
	void *buffer;               /* DMA buffer */
	dma_addr_t dma_handle;      /* DMA physical address */
	struct device *dma_dev;     /* Retained original allocation device */
	size_t size;                /* Buffer size (bytes) */
	
	unsigned long write_pos;    /* Write pointer */
	unsigned long read_pos;     /* Read pointer */
	
	spinlock_t lock;            /* Spinlock */
	wait_queue_head_t wait;     /* Wait queue */
	
	/* Performance statistics */
	unsigned long overruns;     /* Overflow count */
	unsigned long max_fill;     /* Maximum fill level */
};

/* Core API functions */
/* init gives the caller one reference; register holds another until unregister.
 * Caller closes/quiesces transport before dropping its reference. Final put
 * frees the ring and invokes release; successful open holds ref + owner pin.
 */
void xtos_bci_device_init(struct xtos_bci_dev *dev);
void xtos_bci_get_device(struct xtos_bci_dev *dev);
void xtos_bci_put_device(struct xtos_bci_dev *dev);
int xtos_bci_register_device(struct xtos_bci_dev *dev);
void xtos_bci_unregister_device(struct xtos_bci_dev *dev);

/* Ring buffer API */
struct xtos_bci_ringbuf *xtos_bci_ringbuf_alloc(struct device *dev, size_t size);
void xtos_bci_ringbuf_free(struct device *dev, struct xtos_bci_ringbuf *rb);
int xtos_bci_ringbuf_write(struct xtos_bci_ringbuf *rb, 
                           const void *data, 
                           size_t len);
int xtos_bci_ringbuf_read(struct xtos_bci_ringbuf *rb, 
                          void *data, 
                          size_t len);
size_t xtos_bci_ringbuf_available(struct xtos_bci_ringbuf *rb);

/* Enable in sleepable initialization before admitting RX. Producer lifetime and
 * current-generation admission belong to the driver; publish never allocates. */
int xtos_bci_device_frames_enable(struct xtos_bci_dev *dev, unsigned int capacity);
int xtos_bci_device_frame_publish(struct xtos_bci_dev *dev,
                                const struct xtos_bci_frame_v1 *frame,
                                const struct xtos_bci_config_v1 *config);
void xtos_bci_device_frames_wake(struct xtos_bci_dev *dev);

/* Device registration macro */
#define XTOS_BCI_DEVICE_REGISTER(name, ops_ptr) \
	static int __init name##_init(void) { \
		return xtos_bci_register_device(&name##_dev); \
	} \
	static void __exit name##_exit(void) { \
		xtos_bci_unregister_device(&name##_dev); \
	} \
	module_init(name##_init); \
	module_exit(name##_exit);

#endif /* _LINUX_XTOS_BCI_H */
