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

#ifdef XTOS_CYTON_HOST_TEST
#include "shim.h"
#else
#include <linux/module.h>
#include <linux/kernel.h>
#include <linux/slab.h>
#include <linux/tty.h>
#include <linux/serial.h>
#include <linux/serdev.h>
#include <linux/of.h>
#include <linux/workqueue.h>
#include <linux/delay.h>
#include <linux/mutex.h>
#include <linux/xtos_bci.h>
#include <linux/xtos_bci_config.h>
#include <linux/jiffies.h>
#include <linux/ktime.h>
#include <linux/xtos_bci_frame.h>
#endif
#include "../xtos_cyton_packet.h"
#include "../xtos_cyton_config.h"

#define OPENBCI_REPLY_MAX 8192
#define OPENBCI_REPLY_TIMEOUT (3 * HZ)

#define OPENBCI_DEFAULT_RATE 250
#define OPENBCI_ADC_BITS 24
#define OPENBCI_VREF_MV 4500

/* OpenBCI private data */
struct openbci_priv {
	struct serdev_device *serdev;

	/* Incremental packet parser shared with host regression/fuzz tests. */
	struct xtos_cyton_parser parser;

	/* Serialize host controls against start/stop and one another. */
	struct mutex control_lock;
	struct mutex state_lock;

	/* Device state */
	bool receiving;
	bool config_valid, cancelled;
	struct xtos_cyton_config config;
	u64 generation;
	u64 stream_generation;
	unsigned int pending_quality;
	char firmware[256];
	char *reply;
	size_t reply_length, reply_limit;
	bool reply_pending, reply_done;
	int reply_error;
	wait_queue_head_t reply_wait;
	
	/* Statistics */
	unsigned long packets_received;
	unsigned long packets_dropped;
};

static int openbci_stop_stream(struct xtos_bci_dev *dev);
static int openbci_stop_locked(struct xtos_bci_dev *dev);
static int openbci_config_locked(struct xtos_bci_dev *dev,
			       struct xtos_bci_config_v1 *out);

/**
 * openbci_process_packet - Parse and process OpenBCI data packet
 * @dev: BCI device
 * @packet: Raw packet data (33 bytes)
 *
 * OpenBCI packet format:
 *   Byte 0: Start byte (0xA0)
 *   Byte 1: sample counter (not channel data)
 *   Bytes 2-25: 8 channels × 3 bytes/channel (24-bit ADC)
 *   Bytes 26-31: Auxiliary data
 *   Byte 32: End byte (0xC0-0xCF)
 */
static void openbci_process_packet(struct xtos_bci_dev *dev,
                                   const s32 samples[XTOS_CYTON_CHANNELS])
{
	struct openbci_priv *priv = dev->hw_private;
	unsigned long flags;
	int ret;
	int expected = sizeof(s32) * XTOS_CYTON_CHANNELS;

	if (!dev->ringbuf)
		return;
	ret = xtos_bci_ringbuf_write(dev->ringbuf, samples, expected);
	spin_lock_irqsave(&dev->lock, flags);
	if (ret != expected) {
		if (ret < 0)
			dev->errors++;
		dev->samples_dropped += XTOS_CYTON_CHANNELS;
	} else {
		dev->samples_read += XTOS_CYTON_CHANNELS;
	}
	spin_unlock_irqrestore(&dev->lock, flags);
	priv->packets_received++;
}

/* state_lock held; no TX/wait and no ring lock nesting. */
static void openbci_rx_fault(struct xtos_bci_dev *dev, int error)
{
	struct openbci_priv *priv = dev->hw_private;
	unsigned long flags;
	priv->config_valid = false;
	priv->receiving = false;
	WRITE_ONCE(dev->streaming, false);
	WRITE_ONCE(dev->acquisition_error, error < 0 ? error : -EIO);
	spin_lock_irqsave(&dev->lock, flags);
	dev->errors++;
	spin_unlock_irqrestore(&dev->lock, flags);
}

static int openbci_receive_buf(struct serdev_device *serdev,
                              const unsigned char *data,
                              size_t count)
{
	struct xtos_bci_dev *dev = serdev_device_get_drvdata(serdev);
	struct openbci_priv *priv;
	s32 samples[XTOS_CYTON_CHANNELS];
	size_t i;
	u64 host_rx_ns;

	if (!dev)
		return count;
	priv = dev->hw_private;
	mutex_lock(&priv->state_lock);
	host_rx_ns = ktime_get_ns();
	if (priv->reply_pending && !READ_ONCE(priv->cancelled)) {
		for (i = 0; i < count && !priv->reply_done; i++) {
			unsigned char c = data[i];
			if (priv->reply_length >= priv->reply_limit) {
				priv->reply_error = -EMSGSIZE;
				WRITE_ONCE(priv->reply_done, true);
				break;
			}
			if (c != '\r' && c != '\n' && c != '	' && (c < 32 || c > 126)) {
				priv->reply_error = -EPROTO;
				WRITE_ONCE(priv->reply_done, true);
				break;
			}
			priv->reply[priv->reply_length++] = c;
			if (priv->reply_length >= 3 &&
			    !memcmp(priv->reply + priv->reply_length - 3, "$$$", 3))
				WRITE_ONCE(priv->reply_done, true);
		}
		mutex_unlock(&priv->state_lock);
		wake_up_interruptible(&priv->reply_wait);
		return count;
	}
	if (!priv->receiving || READ_ONCE(dev->disconnected)) {
		mutex_unlock(&priv->state_lock);
		return count;
	}

	for (i = 0; i < count; i++) {
		unsigned long before = priv->parser.missing_samples;
		unsigned int counter_valid = priv->parser.counter_valid;
		unsigned char previous_counter = priv->parser.last_counter;
		unsigned long flags;
		int result;
		if (!priv->parser.packet_pos && data[i] != XTOS_CYTON_START_BYTE)
			priv->pending_quality |= XTOS_BCI_FRAME_QUALITY_RESYNC;
		result = xtos_cyton_parser_feed(&priv->parser, data[i], samples);

		/* Count sequence gaps once, in channel samples, independently of
		 * rejected candidates and ringbuffer delivery errors. */
		if (result != 0) {
			spin_lock_irqsave(&dev->lock, flags);
			dev->samples_dropped +=
				(priv->parser.missing_samples - before) * XTOS_CYTON_CHANNELS;
			if (result < 0)
				dev->errors++;
			spin_unlock_irqrestore(&dev->lock, flags);
		}
		if (result < 0) {
			priv->packets_dropped++;
			priv->pending_quality |= XTOS_BCI_FRAME_QUALITY_RESYNC |
				XTOS_BCI_FRAME_QUALITY_CONTINUITY_UNKNOWN;
		}
		else if (result > 0) {
			unsigned int ch;
			struct xtos_bci_config_v1 config;
			struct xtos_bci_frame_v1 frame = {
				.version = XTOS_BCI_FRAME_VERSION,
				.record_bytes = sizeof(frame),
				.channels = XTOS_CYTON_CHANNELS,
				.sample_rate_hz = OPENBCI_DEFAULT_RATE,
				.unit = XTOS_BCI_UNIT_NANOVOLT,
				.host_clock = XTOS_BCI_FRAME_CLOCK_MONOTONIC,
				.quality_flags = XTOS_BCI_FRAME_QUALITY_CONFIG_VERIFIED |
					priv->pending_quality,
				.source_flags = XTOS_BCI_FRAME_SOURCE_SERDEV,
				.host_rx_ns = host_rx_ns,
				.stream_generation = priv->stream_generation,
				.config_generation = priv->generation,
				.device_counter = priv->parser.packet[1],
				.counter_bits = 8,
				.enabled_mask = priv->config.enabled_mask,
			};
			if (counter_valid) {
				unsigned int delta = (frame.device_counter - previous_counter) & 0xff;
				if (delta >= 2 && delta <= 127) {
					frame.missing_before = delta - 1;
					frame.quality_flags |= XTOS_BCI_FRAME_QUALITY_COUNTER_GAP;
				}
			}
			for (ch = 0; ch < XTOS_CYTON_CHANNELS; ch++) {
				unsigned int at = 2 + ch * 3;
				s32 raw = ((s32)priv->parser.packet[at] << 16) |
					((s32)priv->parser.packet[at + 1] << 8) |
					priv->parser.packet[at + 2];
				s64 value;
				if (raw == 0x7fffff || raw == 0x800000)
					frame.quality_flags |= XTOS_BCI_FRAME_QUALITY_ADC_RAIL;
				if (raw & 0x800000) raw -= 1 << 24;
				value = (s64)raw * XTOS_CYTON_VREF_NV /
					((s64)priv->config.gain[ch] * 8388607);
				if (value < -2147483647LL - 1 || value > 2147483647LL) {
					openbci_rx_fault(dev, -ERANGE);
					break;
				}
				samples[ch] = (s32)value;
			}
			if (ch != XTOS_CYTON_CHANNELS) break;
			memcpy(frame.samples_nv, samples, sizeof(samples));
			result = openbci_config_locked(dev, &config);
			if (!result) result = xtos_bci_device_frame_publish(dev, &frame, &config);
			if (result) { openbci_rx_fault(dev, result); break; }
			priv->pending_quality = 0;
			openbci_process_packet(dev, samples);
		}
	}
	mutex_unlock(&priv->state_lock);
	if (READ_ONCE(dev->acquisition_error)) {
		if (dev->ringbuf) wake_up_interruptible(&dev->ringbuf->wait);
		xtos_bci_device_frames_wake(dev);
	}

	return count;
}

static const struct serdev_device_ops openbci_serdev_ops = {
	.receive_buf = openbci_receive_buf,
	.write_wakeup = serdev_device_write_wakeup,
};

/**
 * openbci_send_command - Send single-character command
 */
static int openbci_send_command(struct openbci_priv *priv, char cmd)
{
	int ret;

	if (READ_ONCE(priv->cancelled))
		return -ENODEV;
	ret = serdev_device_write(priv->serdev, &cmd, 1, HZ);
	if (ret != 1) {
		if (ret >= 0)
			ret = -EIO;
		pr_err("openbci: Failed to send command '%c': %d\n", cmd, ret);
		return ret;
	}

	/* Small delay for command processing */
	msleep(10);

	return 0;
}

/* control_lock owns a transaction; RX only owns state_lock. Arm before TX. */
static int openbci_transaction(struct openbci_priv *priv, const char *command,
			       size_t length, size_t limit)
{
	unsigned long deadline = jiffies + OPENBCI_REPLY_TIMEOUT;
	unsigned long now, remaining;
	long waited;
	int ret;
	mutex_lock(&priv->state_lock);
	priv->reply_length = 0;
	priv->reply_limit = limit;
	priv->reply_error = 0;
	WRITE_ONCE(priv->reply_done, false);
	priv->reply_pending = true;
	mutex_unlock(&priv->state_lock);
	if (READ_ONCE(priv->cancelled)) { ret = -ENODEV; goto out; }
	ret = serdev_device_write(priv->serdev, command, length, HZ);
	if (ret < 0 || (size_t)ret != length) { if (ret >= 0) ret = -EIO; goto out; }
	/* Snapshot once: a second jiffies read can cross deadline and wrap
	 * the unsigned subtraction into an unbounded wait. */
	now = jiffies;
	if (time_after_eq(now, deadline)) { ret = -ETIMEDOUT; goto out; }
	remaining = deadline - now;
	waited = wait_event_interruptible_timeout(priv->reply_wait,
		READ_ONCE(priv->reply_done) || READ_ONCE(priv->cancelled),
		remaining);
	ret = waited < 0 ? (int)waited : waited == 0 ? -ETIMEDOUT : 0;
out:
	mutex_lock(&priv->state_lock);
	priv->reply_pending = false;
	if (READ_ONCE(priv->cancelled)) ret = -ENODEV;
	if (!ret) ret = priv->reply_error;
	priv->reply[priv->reply_length] = 0;
	mutex_unlock(&priv->state_lock);
	return ret;
}

static void openbci_invalidate(struct xtos_bci_dev *dev, int error)
{
	struct openbci_priv *priv = dev->hw_private;
	mutex_lock(&priv->state_lock);
	priv->config_valid = false;
	priv->receiving = false;
	WRITE_ONCE(dev->streaming, false);
	WRITE_ONCE(dev->acquisition_error, error);
	mutex_unlock(&priv->state_lock);
	if (dev->ringbuf) wake_up_interruptible(&dev->ringbuf->wait);
	xtos_bci_device_frames_wake(dev);
}

static int openbci_read_config(struct openbci_priv *priv,
			       struct xtos_cyton_config *config)
{
	int ret = openbci_transaction(priv, "?", 1, OPENBCI_REPLY_MAX);
	if (ret) return ret;
	ret = xtos_cyton_config_parse(priv->reply, priv->reply_length - 3, config);
	if (ret) return ret;
	if (config->register_mask != 0xffffff || config->registers[0] != 0x3e ||
	    (config->registers[1] & 7) != 6) return -EPROTO;
	return 0;
}

static int openbci_commit_config(struct openbci_priv *priv,
				struct xtos_cyton_config *config)
{
	int ret = 0;
	mutex_lock(&priv->state_lock);
	if (READ_ONCE(priv->cancelled)) ret = -ENODEV;
	else if (priv->generation == ~(u64)0) ret = -EOVERFLOW;
	else { priv->config = *config; priv->generation++; priv->config_valid = true; }
	mutex_unlock(&priv->state_lock);
	return ret;
}

/* Bounded banner body excludes $$$. Required identities are entire logical
 * lines; version and other printable firmware lines remain unconstrained. */
static bool openbci_banner_valid(const char *body, size_t n)
{
	size_t i, start = 0;
	bool board = false, ads = false;
	static const char board_line[] = "OpenBCI V3 8-16 channel";
	static const char ads_line[] = "On Board ADS1299 Device ID: 0x3E";
	if (!n || n > 255) return false;
	for (i = 0; i <= n; i++) {
		if (i < n) {
			unsigned char c = body[i];
			if (c != '\r' && c != '\n' && (c < 32 || c > 126)) return false;
			if (i + 5 <= n && !memcmp(body + i, "Daisy", 5)) return false;
			if (c != '\r' && c != '\n') continue;
		}
		if (i - start == sizeof(board_line) - 1 &&
		    !memcmp(body + start, board_line, sizeof(board_line) - 1)) board = true;
		if (i - start == sizeof(ads_line) - 1 &&
		    !memcmp(body + start, ads_line, sizeof(ads_line) - 1)) ads = true;
		start = i + 1;
	}
	return board && ads;
}

/**
 * Device operation callbacks
 */
static int openbci_init(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;
	struct xtos_bci_ringbuf *ringbuf;
	int ret = 0;

	mutex_lock(&priv->control_lock);
	mutex_lock(&priv->state_lock);
	priv->config_valid = false;
	priv->receiving = false;
	WRITE_ONCE(dev->streaming, false);
	xtos_cyton_parser_init(&priv->parser);
	mutex_unlock(&priv->state_lock);

	ret = openbci_send_command(priv, 's');
	if (!ret) ret = openbci_transaction(priv, "v", 1, 258);
	if (!ret) {
		size_t n = priv->reply_length - 3;
		if (!openbci_banner_valid(priv->reply, n)) ret = -EPROTO;
		else { memcpy(priv->firmware, priv->reply, n); priv->firmware[n] = 0; }
	}
	if (!ret) {
		struct xtos_cyton_config config;
		ret = openbci_read_config(priv, &config);
		if (!ret) ret = openbci_commit_config(priv, &config);
	}
	if (ret) goto out;
	WRITE_ONCE(dev->acquisition_error, 0);
	ret = xtos_bci_device_frames_enable(dev, 500);
	if (ret || dev->ringbuf) goto out;
	/* Allocate two seconds of signed nanovolt samples (s32 per channel). */
	ringbuf = xtos_bci_ringbuf_alloc(&priv->serdev->dev,
		dev->num_channels * dev->sampling_rate * sizeof(s32) * 2);
	if (!ringbuf) {
		ret = -ENOMEM;
		goto out;
	}
	mutex_lock(&priv->state_lock);
	WRITE_ONCE(dev->ringbuf, ringbuf);
	priv->packets_received = 0;
	priv->packets_dropped = 0;
	mutex_unlock(&priv->state_lock);
out:
	if (ret) openbci_invalidate(dev, ret);
	mutex_unlock(&priv->control_lock);
	return ret;
}

static void openbci_cleanup(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;

	mutex_lock(&priv->control_lock);
	openbci_stop_locked(dev);

	/* Retain storage for read/poll and open FDs. Final put frees the ring. */
	mutex_lock(&priv->state_lock);
	mutex_unlock(&priv->state_lock);
	mutex_unlock(&priv->control_lock);
}

static int openbci_start_stream(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;
	int ret = 0;

	mutex_lock(&priv->control_lock);
	mutex_lock(&priv->state_lock);
	if (priv->receiving)
		goto unlock_state;
	if (!dev->ringbuf) {
		ret = -ENODEV;
		goto unlock_state;
	}
	if (priv->stream_generation == ~(u64)0) {
		mutex_unlock(&priv->state_lock);
		openbci_invalidate(dev, -EOVERFLOW);
		mutex_unlock(&priv->control_lock);
		return -EOVERFLOW;
	}
	mutex_unlock(&priv->state_lock);
	{
		struct xtos_cyton_config config;
		ret = openbci_read_config(priv, &config);
		if (!ret) ret = openbci_commit_config(priv, &config);
	}
	if (ret) {
		openbci_invalidate(dev, ret);
		openbci_send_command(priv, 's');
		mutex_unlock(&priv->control_lock);
		return ret;
	}
	mutex_lock(&priv->state_lock);
	if (READ_ONCE(priv->cancelled) || READ_ONCE(dev->disconnected)) {
		ret = -ENODEV;
		goto unlock_state;
	}
	WRITE_ONCE(dev->acquisition_error, 0);
	/* The board may deliver the first frame before synchronous TX returns. */
	xtos_cyton_parser_init(&priv->parser);
	priv->pending_quality = 0;
	priv->stream_generation++;
	priv->receiving = true;
	WRITE_ONCE(dev->streaming, true);
	mutex_unlock(&priv->state_lock);
	ret = openbci_send_command(priv, 'b');
	mutex_lock(&priv->state_lock);
	/* RX can fail synchronously inside TX; never report that START as live. */
	if (READ_ONCE(priv->cancelled) || READ_ONCE(dev->disconnected))
		ret = -ENODEV;
	else if (READ_ONCE(dev->acquisition_error))
		ret = READ_ONCE(dev->acquisition_error);
	mutex_unlock(&priv->state_lock);
	if (ret) {
		mutex_lock(&priv->state_lock);
		priv->receiving = false;
		WRITE_ONCE(dev->streaming, false);
		xtos_cyton_parser_init(&priv->parser);
		mutex_unlock(&priv->state_lock);
		/* A short/error write does not prove the board ignored START. */
		openbci_invalidate(dev, ret);
		openbci_send_command(priv, 's');
	}
	mutex_unlock(&priv->control_lock);
	return ret;

unlock_state:
	mutex_unlock(&priv->state_lock);
	mutex_unlock(&priv->control_lock);
	return ret;
}

/* control_lock serializes controls; state_lock must never span serial TX. */
static int openbci_stop_locked(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;
	bool active;

	mutex_lock(&priv->state_lock);
	active = priv->receiving;
	priv->receiving = false;
	WRITE_ONCE(dev->streaming, false);
	xtos_cyton_parser_init(&priv->parser);
	mutex_unlock(&priv->state_lock);
	if (dev->ringbuf) wake_up_interruptible(&dev->ringbuf->wait);
	xtos_bci_device_frames_wake(dev);
	if (active) {
		int ret = openbci_send_command(priv, 's');
		if (ret) openbci_invalidate(dev, ret);
		return ret;
	}
	return 0;
}

static int openbci_stop_stream(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;
	int ret;

	mutex_lock(&priv->control_lock);
	ret = openbci_stop_locked(dev);
	mutex_unlock(&priv->control_lock);
	return ret;
}

static int openbci_config_locked(struct xtos_bci_dev *dev,
			       struct xtos_bci_config_v1 *out)
{
	struct openbci_priv *priv = dev->hw_private;
	int ret = 0;
	if (READ_ONCE(priv->cancelled) || READ_ONCE(dev->disconnected)) ret = -ENODEV;
	else if (!priv->config_valid) ret = -ENODATA;
	else {
		memset(out, 0, sizeof(*out));
		out->version = 1;
		out->channels = 8;
		out->sample_rate_hz = 250;
		out->enabled_mask = priv->config.enabled_mask;
		out->capabilities = XTOS_BCI_CONFIG_READBACK | XTOS_BCI_FIRMWARE_READBACK |
			XTOS_BCI_GAIN_CONTROL;
		out->generation = priv->generation;
		memcpy(out->gain, priv->config.gain, sizeof(out->gain));
		memcpy(out->registers, priv->config.registers, sizeof(out->registers));
		memcpy(out->firmware, priv->firmware, sizeof(out->firmware));
	}
	return ret;
}

static int openbci_get_config(struct xtos_bci_dev *dev,
			      struct xtos_bci_config_v1 *out)
{
	struct openbci_priv *priv = dev->hw_private;
	int ret;
	mutex_lock(&priv->state_lock);
	ret = openbci_config_locked(dev, out);
	mutex_unlock(&priv->state_lock);
	return ret;
}

/* Caller holds control_lock, excluding START until the control completes.
 * RX may stop receiving under state_lock; sample that transition under the
 * same lock, then release it before any synchronous serial TX. */
static bool openbci_control_receiving(struct openbci_priv *priv)
{
	bool receiving;

	mutex_lock(&priv->state_lock);
	receiving = priv->receiving;
	mutex_unlock(&priv->state_lock);
	return receiving;
}

/* Comparator status is read-only; every other ADS byte must remain stable. */
static int openbci_verify_registers(const struct xtos_cyton_config *observed,
				   const unsigned char desired[24])
{
	unsigned int i;
	for (i = 1; i < 24; i++) {
		if (i == 18 || i == 19) continue;
		if (observed->registers[i] != desired[i]) return -EPROTO;
	}
	return 0;
}

static int openbci_set_channel_config(struct xtos_bci_dev *dev, int channel,
				       bool enabled, int gain)
{
	static const int gains[] = {1,2,4,6,8,12,24};
	struct openbci_priv *priv = dev->hw_private;
	struct xtos_cyton_config before, after;
	unsigned char desired[24];
	char command[10], ack[40];
	int code = 0, ret;
	if (channel < 0 || channel >= 8) return -EINVAL;
	if (gain) {
		for (code = 0; code < 7 && gains[code] != gain; code++);
		if (code == 7) return -EINVAL;
	}
	mutex_lock(&priv->control_lock);
	if (openbci_control_receiving(priv)) { ret = -EBUSY; goto out; }
	ret = openbci_read_config(priv, &before);
	if (ret) goto failed;
	/* Firmware writes both bias masks and the board-wide SRB1 byte. */
	if (before.registers[13] != before.registers[14] ||
	    (before.registers[21] & ~0x20)) { ret = -EOPNOTSUPP; goto out; }
	if (!gain) code = (before.registers[5 + channel] >> 4) & 7;
	memcpy(desired, before.registers, sizeof(desired));
	desired[5 + channel] = (desired[5 + channel] & 15) |
		(code << 4) | (enabled ? 0 : 0x80);
	snprintf(command, sizeof(command), "x%c%c%c%c%c%c%cX", '1' + channel,
		enabled ? '0' : '1', '0' + code, '0' + (desired[5 + channel] & 7),
		'0' + ((desired[13] >> channel) & 1),
		'0' + ((desired[5 + channel] >> 3) & 1),
		'0' + ((desired[21] >> 5) & 1));
	ret = openbci_transaction(priv, command, 9, 255);
	if (ret) goto failed;
	snprintf(ack, sizeof(ack), "Success: Channel set for %c$$$", '1' + channel);
	if (strcmp(priv->reply, ack)) { ret = -EPROTO; goto failed; }
	ret = openbci_read_config(priv, &after);
	if (!ret) ret = openbci_verify_registers(&after, desired);
	if (!ret) ret = openbci_commit_config(priv, &after);
	if (ret) goto failed;
	goto out;
failed:
	openbci_invalidate(dev, ret);
out:
	mutex_unlock(&priv->control_lock);
	return ret;
}

static int openbci_set_sampling_rate(struct xtos_bci_dev *dev, int rate)
{
	struct openbci_priv *priv = dev->hw_private;
	struct xtos_cyton_config config;
	int ret;

	switch (rate) {
	case 250:
		break;
	case 500:
	case 1000:
	case 2000:
		/* 33-byte frames, 8N1: 500 Hz already exceeds 115200 baud. */
		return -EOPNOTSUPP;
	default:
		pr_err("openbci: Unsupported sampling rate: %d Hz\n", rate);
		return -EINVAL;
	}

	mutex_lock(&priv->control_lock);
	if (openbci_control_receiving(priv)) {
		mutex_unlock(&priv->control_lock);
		return -EBUSY;
	}
	/* The sole supported rate is a readback assertion. Firmware ~6 resets
	 * all ADS state; never use it to acknowledge an already-250Hz profile. */
	ret = openbci_read_config(priv, &config);
	if (!ret) ret = openbci_commit_config(priv, &config);
	if (ret) openbci_invalidate(dev, ret);
	mutex_unlock(&priv->control_lock);
	return ret;
}

static int openbci_set_channel_enable(struct xtos_bci_dev *dev,
                                     int channel,
                                     bool enable)
{
	struct openbci_priv *priv = dev->hw_private;
	char command;
	int ret;

	if (channel < 0 || channel >= XTOS_CYTON_CHANNELS)
		return -EINVAL;

	/* Firmware commands are a literal keyboard-symbol table. */
	command = enable ? "!@#$%^&*"[channel] : "12345678"[channel];
	mutex_lock(&priv->control_lock);
	if (openbci_control_receiving(priv)) ret = -EBUSY;
	else {
		struct xtos_cyton_config before, after;
		ret = openbci_read_config(priv, &before);
		if (!ret) ret = openbci_send_command(priv, command);
		if (!ret) ret = openbci_read_config(priv, &after);
		if (!ret) {
			before.registers[5 + channel] = (before.registers[5 + channel] & 0x7f) |
				(enable ? 0 : 0x80);
			ret = openbci_verify_registers(&after, before.registers);
		}
		if (!ret) ret = openbci_commit_config(priv, &after);
		if (ret) openbci_invalidate(dev, ret);
	}
	mutex_unlock(&priv->control_lock);
	return ret;
}

static int openbci_calibrate(struct xtos_bci_dev *dev)
{
	return -EOPNOTSUPP;
}

static int openbci_impedance_check(struct xtos_bci_dev *dev, __u32 *impedances_kohm)
{
	pr_info("openbci: Impedance check not implemented\n");
	return -EOPNOTSUPP;
}

static void openbci_cancel(struct xtos_bci_dev *dev);

/* Device operations table */
static const struct xtos_bci_device_ops openbci_ops = {
	.cancel = openbci_cancel,
	.get_config = openbci_get_config,
	.set_channel_config = openbci_set_channel_config,
	.init = openbci_init,
	.cleanup = openbci_cleanup,
	.start_stream = openbci_start_stream,
	.stop_stream = openbci_stop_stream,
	.set_sampling_rate = openbci_set_sampling_rate,
	.set_channel_enable = openbci_set_channel_enable,
	.calibrate = openbci_calibrate,
	.impedance_check = openbci_impedance_check,
};

static void openbci_cancel(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;
	WRITE_ONCE(priv->cancelled, true);
	/* Wake a transaction before waiting for any state/control owner. */
	wake_up_interruptible(&priv->reply_wait);
	mutex_lock(&priv->state_lock);
	priv->receiving = false;
	priv->config_valid = false;
	WRITE_ONCE(dev->streaming, false);
	mutex_unlock(&priv->state_lock);
	if (dev->ringbuf) wake_up_interruptible(&dev->ringbuf->wait);
	xtos_bci_device_frames_wake(dev);
}

static void openbci_release(struct xtos_bci_dev *dev)
{
	struct openbci_priv *priv = dev->hw_private;
	kfree(priv->reply);
	kfree(priv);
	kfree(dev);
}

/**
 * Serial device probe/remove
 */
static int openbci_probe(struct serdev_device *serdev)
{
	struct openbci_priv *priv;
	struct xtos_bci_dev *dev;
	int ret;

	pr_info("openbci: Probing device\n");

	/* Allocate private data */
	dev = kzalloc(sizeof(*dev), GFP_KERNEL);
	if (!dev)
		return -ENOMEM;
	priv = kzalloc(sizeof(*priv), GFP_KERNEL);
	if (!priv) {
		kfree(dev);
		return -ENOMEM;
	}
	xtos_bci_device_init(dev);
	dev->owner = THIS_MODULE;
	dev->release = openbci_release;

	priv->reply = kzalloc(OPENBCI_REPLY_MAX + 1, GFP_KERNEL);
	if (!priv->reply) { kfree(priv); kfree(dev); return -ENOMEM; }
	init_waitqueue_head(&priv->reply_wait);
	priv->serdev = serdev;
	mutex_init(&priv->control_lock);
	mutex_init(&priv->state_lock);
	xtos_cyton_parser_init(&priv->parser);

	/* Initialize device descriptor */
	strncpy(dev->name, "OpenBCI Cyton", sizeof(dev->name) - 1);
	dev->num_channels = XTOS_CYTON_CHANNELS;
	dev->sampling_rate = OPENBCI_DEFAULT_RATE;
	dev->resolution_bits = OPENBCI_ADC_BITS;
	dev->input_range_mv = OPENBCI_VREF_MV;
	dev->interface = XTOS_BCI_SERIAL;
	dev->hw_private = priv;
	dev->ops = &openbci_ops;

	/* Configure serial device */
	serdev_device_set_drvdata(serdev, dev);
	serdev_device_set_client_ops(serdev, &openbci_serdev_ops);

	ret = serdev_device_open(serdev);
	if (ret) {
		pr_err("openbci: Failed to open serial device: %d\n", ret);
		goto err_put;
	}

	/* Configure serial parameters: 115200 8N1 */
	serdev_device_set_baudrate(serdev, 115200);
	serdev_device_set_flow_control(serdev, false);

	/* Register with BCI core */
	ret = xtos_bci_register_device(dev);
	if (ret) {
		pr_err("openbci: Failed to register device: %d\n", ret);
		serdev_device_close(serdev);
		goto err_put;
	}

	pr_info("openbci: Device probed successfully\n");

	return 0;
err_put:
	serdev_device_set_drvdata(serdev, NULL);
	xtos_bci_put_device(dev);
	return ret;
}

static void openbci_remove(struct serdev_device *serdev)
{
	struct xtos_bci_dev *dev = serdev_device_get_drvdata(serdev);
	pr_info("openbci: Removing device\n");

	openbci_cancel(dev);
	xtos_bci_unregister_device(dev);
	serdev_device_close(serdev);
	serdev_device_set_drvdata(serdev, NULL);
	xtos_bci_put_device(dev);

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
