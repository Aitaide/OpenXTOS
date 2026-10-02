// SPDX-License-Identifier: GPL-2.0
/*
 * OpenBCI Cyton Device Driver
 * 
 * Driver for OpenBCI Cyton 8-channel EEG board
 * - 8 channels, 24-bit ADC (ADS1299)
 * - Serial interface at 115200 baud
 * - 250 Hz default sampling rate
 * 
 * Copyright (C) 2026 XTOS Project
 */

#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/slab.h>
#include <linux/tty.h>
#include <linux/serial.h>
#include <linux/serdev.h>
#include <linux/of.h>
#include <linux/workqueue.h>
#include <linux/delay.h>
#include <linux/xtos_bci.h>

#define OPENBCI_PACKET_SIZE 33
#define OPENBCI_START_BYTE 0xA0
#define OPENBCI_NUM_CHANNELS 8
#define OPENBCI_DEFAULT_RATE 250
#define OPENBCI_VREF_MV 4500
#define OPENBCI_VREF_NV 4500000000LL
#define OPENBCI_GAIN 24
#define OPENBCI_ADC_BITS 24

/* OpenBCI private data */
struct openbci_priv {
	struct serdev_device *serdev;
	
	/* Packet parsing state */
	unsigned char packet_buf[OPENBCI_PACKET_SIZE];
	int packet_pos;
	
	/* Work queue for data processing */
	struct work_struct read_work;
	struct workqueue_struct *workqueue;
	
	/* Device state */
	bool receiving;
	
	/* Statistics */
	unsigned long packets_received;
	unsigned long packets_dropped;
};

/* Global device instance */
static struct xtos_bci_dev openbci_cyton_dev;
static int openbci_stop_stream(struct xtos_bci_dev *dev);

/**
 * openbci_process_packet - Parse and process OpenBCI data packet
 * @dev: BCI device
 * @packet: Raw packet data (33 bytes)
 *
 * OpenBCI packet format:
 *   Byte 0: Start byte (0xA0)
 *   Bytes 1-24: 8 channels × 3 bytes/channel (24-bit ADC)
 *   Bytes 25-30: Auxiliary data
 *   Bytes 31-32: End bytes (0xCX where X cycles 0-F)
 */
static void openbci_process_packet(struct xtos_bci_dev *dev, 
                                   unsigned char *packet)
{
	struct openbci_priv *priv = dev->hw_private;
	s32 samples[OPENBCI_NUM_CHANNELS];
	int i;

	/* Verify start byte */
	if (packet[0] != OPENBCI_START_BYTE) {
		pr_debug("openbci: Invalid start byte: 0x%02x\n", packet[0]);
		priv->packets_dropped++;
		return;
	}

	/* Convert 24-bit ADC values to microvolts */
	for (i = 0; i < OPENBCI_NUM_CHANNELS; i++) {
		int32_t raw;
		
		/* Extract 3 bytes as 24-bit signed integer */
		raw = ((int32_t)packet[1 + i*3] << 16) |
		      ((int32_t)packet[2 + i*3] << 8) |
		      ((int32_t)packet[3 + i*3]);
		
		/* Sign extension for 24-bit value */
		if (raw & 0x800000)
			raw |= 0xFF000000;
		
		/* Convert to nanovolts using integer arithmetic.  The
		 * userspace API converts nV to floating-point units as needed. */
		samples[i] = (s32)(((s64)raw * OPENBCI_VREF_NV) /
				((s64)OPENBCI_GAIN * (1LL << 23)));
	}

	/* Write to ring buffer */
	if (dev->ringbuf) {
		int ret = xtos_bci_ringbuf_write(dev->ringbuf, samples, 
		                                 sizeof(samples));
		if (ret < 0) {
			pr_err_ratelimited("openbci: Failed to write to ring buffer: %d\n", 
			                   ret);
			dev->samples_dropped += OPENBCI_NUM_CHANNELS;
		} else {
			dev->samples_read += OPENBCI_NUM_CHANNELS;
		}
	}

	priv->packets_received++;
}

/**
 * openbci_receive_buf - Serial data receive callback
 */
static int openbci_receive_buf(struct serdev_device *serdev,
                              const unsigned char *data,
                              size_t count)
{
	struct xtos_bci_dev *dev = serdev_device_get_drvdata(serdev);
	struct openbci_priv *priv = dev->hw_private;
	size_t i;

	if (!priv->receiving)
		return count;

	for (i = 0; i < count; i++) {
		unsigned char byte = data[i];

		if (priv->packet_pos == 0 && byte == OPENBCI_START_BYTE) {
			/* Start of new packet */
			priv->packet_buf[priv->packet_pos++] = byte;
		} else if (priv->packet_pos > 0 && priv->packet_pos < OPENBCI_PACKET_SIZE) {
			/* Continue building packet */
			priv->packet_buf[priv->packet_pos++] = byte;

			/* Complete packet received */
			if (priv->packet_pos == OPENBCI_PACKET_SIZE) {
				openbci_process_packet(dev, priv->packet_buf);
				priv->packet_pos = 0;
			}
		} else if (priv->packet_pos >= OPENBCI_PACKET_SIZE) {
			/* Should never happen - reset */
			pr_warn("openbci: Packet buffer overflow, resetting\n");
			priv->packet_pos = 0;
		}
	}

	return count;
}

static const struct serdev_device_ops openbci_serdev_ops = {
	.receive_buf = openbci_receive_buf,
};

/**
 * openbci_send_command - Send single-character command
 */
static int openbci_send_command(struct openbci_priv *priv, char cmd)
{
	int ret;

	ret = serdev_device_write(priv->serdev, &cmd, 1, HZ);
	if (ret < 0) {
		pr_err("openbci: Failed to send command '%c': %d\n", cmd, ret);
		return ret;
	}

	/* Small delay for command processing */
	msleep(10);

	return 0;
}

/**
 * Device operation callbacks
 */
static int openbci_init(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;
	int ret;

	pr_info("openbci: Initializing device\n");

	/* Send soft reset */
	ret = openbci_send_command(priv, 'v');  /* Request board info */
	if (ret)
		return ret;

	msleep(100);

	/* Allocate ring buffer (2 seconds of data) */
	dev->ringbuf = xtos_bci_ringbuf_alloc(
		&priv->serdev->dev,
		dev->num_channels * dev->sampling_rate * sizeof(float) * 2
	);
	if (!dev->ringbuf) {
		pr_err("openbci: Failed to allocate ring buffer\n");
		return -ENOMEM;
	}

	priv->packet_pos = 0;
	priv->receiving = false;
	priv->packets_received = 0;
	priv->packets_dropped = 0;

	pr_info("openbci: Device initialized\n");

	return 0;
}

static void openbci_cleanup(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;

	pr_info("openbci: Cleaning up device\n");

	/* Stop streaming if active */
	if (dev->streaming)
		openbci_stop_stream(dev);

	/* Free ring buffer */
	if (dev->ringbuf) {
		xtos_bci_ringbuf_free(&priv->serdev->dev, dev->ringbuf);
		dev->ringbuf = NULL;
	}
}

static int openbci_start_stream(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;
	int ret;

	pr_info("openbci: Starting data stream\n");

	priv->receiving = true;
	priv->packet_pos = 0;

	/* Send 'b' command to start streaming */
	ret = openbci_send_command(priv, 'b');
	if (ret) {
		priv->receiving = false;
		return ret;
	}

	dev->streaming = true;

	pr_info("openbci: Data stream started\n");

	return 0;
}

static int openbci_stop_stream(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;
	int ret;

	pr_info("openbci: Stopping data stream\n");

	dev->streaming = false;
	priv->receiving = false;

	/* Send 's' command to stop streaming */
	ret = openbci_send_command(priv, 's');

	pr_info("openbci: Data stream stopped (received %lu packets, dropped %lu)\n",
	        priv->packets_received, priv->packets_dropped);

	return ret;
}

static int openbci_set_sampling_rate(struct xtos_bci_dev *dev, int rate)
{
	struct openbci_priv *priv = dev->hw_private;
	char cmd;

	/* OpenBCI Cyton supports specific sample rates via tilde commands */
	switch (rate) {
	case 250:
		cmd = '~';  /* ~6 = 250 Hz */
		serdev_device_write(priv->serdev, "~6", 2, HZ);
		break;
	case 500:
		cmd = '~';  /* ~5 = 500 Hz */
		serdev_device_write(priv->serdev, "~5", 2, HZ);
		break;
	case 1000:
		cmd = '~';  /* ~4 = 1000 Hz */
		serdev_device_write(priv->serdev, "~4", 2, HZ);
		break;
	case 2000:
		cmd = '~';  /* ~3 = 2000 Hz */
		serdev_device_write(priv->serdev, "~3", 2, HZ);
		break;
	default:
		pr_err("openbci: Unsupported sampling rate: %d Hz\n", rate);
		return -EINVAL;
	}

	dev->sampling_rate = rate;
	pr_info("openbci: Sampling rate set to %d Hz\n", rate);

	return 0;
}

static int openbci_set_channel_enable(struct xtos_bci_dev *dev, 
                                     int channel, 
                                     bool enable)
{
	struct openbci_priv *priv = dev->hw_private;
	char cmd[2];

	if (channel < 0 || channel >= OPENBCI_NUM_CHANNELS)
		return -EINVAL;

	/* OpenBCI channel control: 1-8 = disable, ! = enable channel 1, etc */
	if (enable) {
		cmd[0] = '!' + channel;  /* '!' to '(' for channels 1-8 */
	} else {
		cmd[0] = '1' + channel;  /* '1' to '8' for channels 1-8 */
	}

	return openbci_send_command(priv, cmd[0]);
}

static int openbci_calibrate(struct xtos_bci_dev *dev)
{
	pr_info("openbci: Calibration not implemented\n");
	return 0;
}

static int openbci_impedance_check(struct xtos_bci_dev *dev, __u32 *impedances_kohm)
{
	pr_info("openbci: Impedance check not implemented\n");
	return -EOPNOTSUPP;
}

/* Device operations table */
static const struct xtos_bci_device_ops openbci_ops = {
	.init = openbci_init,
	.cleanup = openbci_cleanup,
	.start_stream = openbci_start_stream,
	.stop_stream = openbci_stop_stream,
	.set_sampling_rate = openbci_set_sampling_rate,
	.set_channel_enable = openbci_set_channel_enable,
	.calibrate = openbci_calibrate,
	.impedance_check = openbci_impedance_check,
};

/**
 * Serial device probe/remove
 */
static int openbci_probe(struct serdev_device *serdev)
{
	struct openbci_priv *priv;
	int ret;

	pr_info("openbci: Probing device\n");

	/* Allocate private data */
	priv = devm_kzalloc(&serdev->dev, sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;

	priv->serdev = serdev;

	/* Configure serial device */
	serdev_device_set_drvdata(serdev, &openbci_cyton_dev);
	serdev_device_set_client_ops(serdev, &openbci_serdev_ops);

	ret = serdev_device_open(serdev);
	if (ret) {
		pr_err("openbci: Failed to open serial device: %d\n", ret);
		return ret;
	}

	/* Configure serial parameters: 115200 8N1 */
	serdev_device_set_baudrate(serdev, 115200);
	serdev_device_set_flow_control(serdev, false);

	/* Initialize device descriptor */
	strncpy(openbci_cyton_dev.name, "OpenBCI Cyton", 
	        sizeof(openbci_cyton_dev.name) - 1);
	openbci_cyton_dev.num_channels = OPENBCI_NUM_CHANNELS;
	openbci_cyton_dev.sampling_rate = OPENBCI_DEFAULT_RATE;
	openbci_cyton_dev.resolution_bits = OPENBCI_ADC_BITS;
	openbci_cyton_dev.input_range_mv = OPENBCI_VREF_MV;
	openbci_cyton_dev.interface = XTOS_BCI_SERIAL;
	openbci_cyton_dev.hw_private = priv;
	openbci_cyton_dev.ops = &openbci_ops;

	/* Register with BCI core */
	ret = xtos_bci_register_device(&openbci_cyton_dev);
	if (ret) {
		pr_err("openbci: Failed to register device: %d\n", ret);
		serdev_device_close(serdev);
		return ret;
	}

	pr_info("openbci: Device probed successfully\n");

	return 0;
}

static void openbci_remove(struct serdev_device *serdev)
{
	pr_info("openbci: Removing device\n");

	xtos_bci_unregister_device(&openbci_cyton_dev);
	serdev_device_close(serdev);

	pr_info("openbci: Device removed\n");
}

static const struct of_device_id openbci_of_match[] = {
	{ .compatible = "openbci,cyton" },
	{ }
};
MODULE_DEVICE_TABLE(of, openbci_of_match);

static struct serdev_device_driver openbci_driver = {
	.probe = openbci_probe,
	.remove = openbci_remove,
	.driver = {
		.name = "openbci-cyton",
		.of_match_table = openbci_of_match,
	},
};

module_serdev_device_driver(openbci_driver);

MODULE_LICENSE("GPL v2");
MODULE_AUTHOR("XTOS Project");
MODULE_DESCRIPTION("OpenBCI Cyton 8-Channel EEG Driver");
MODULE_VERSION("1.0");
