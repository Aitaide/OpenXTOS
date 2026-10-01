# XTOS BCI Driver Framework - Quick Start Guide

## Overview

The XTOS BCI (Brain-Computer Interface) driver framework provides a unified interface for EEG and brain sensing devices. This guide covers installation, configuration, and basic usage.

---

## Prerequisites

- Linux kernel 6.6.0+ with PREEMPT_RT patch
- GCC toolchain
- Kernel headers installed
- Root access for module loading

---

## Building

### 1. Build User-Space Library

```bash
cd /root/xtos-project/sdk/libxtos-bci
make
sudo make install
```

This creates:
- `/usr/lib/libxtos-bci.so.1.0` (shared library)
- `/usr/lib/libxtos-bci.a` (static library)
- `/usr/include/xtos/bci.h` (header file)

### 2. Build Kernel Modules

First, integrate XTOS drivers into kernel build:

```bash
cd /root/xtos-project/kernel/linux

# Add to drivers/Kconfig (after line "source drivers/..."): 
echo 'source "drivers/xtos/Kconfig"' >> drivers/Kconfig

# Add to drivers/Makefile:
echo 'obj-y += xtos/' >> drivers/Makefile

# Configure kernel
make menuconfig
```

In menuconfig, enable:
- `Device Drivers > XTOS Real-Time Operating System`
  - `[M] XTOS BCI Framework`
  - `[M] OpenBCI Cyton Driver`

Build modules:

```bash
make M=drivers/xtos -j$(nproc)
```

Or use the automated script:

```bash
cd /root/xtos-project
./scripts/build_bci.sh install
```

---

## Loading Modules

```bash
# Load core framework
sudo insmod /root/xtos-project/kernel/linux/drivers/xtos/bci/xtos_bci_core.ko

# Load buffer module (if separate)
sudo insmod /root/xtos-project/kernel/linux/drivers/xtos/bci/xtos_bci_buffer.ko

# Load OpenBCI driver
sudo insmod /root/xtos-project/kernel/linux/drivers/xtos/bci/devices/openbci_cyton.ko

# Verify
lsmod | grep xtos
ls -l /dev/xtos*
```

---

## Hardware Setup

### OpenBCI Cyton

1. **Connect device** via USB-to-Serial adapter or Bluetooth dongle
2. **Identify serial port**: `ls /dev/ttyUSB*` or `/dev/rfcomm0`
3. **Bind to serdev** (requires device tree or manual binding)

Manual binding example:

```bash
# Find the serial port
dmesg | grep tty

# Bind to serdev (kernel 4.11+)
echo "openbci,cyton" > /sys/bus/serial/devices/ttyUSB0/serial_port_type
```

---

## Testing

### Basic Test Program

```bash
cd /root/xtos-project/examples
gcc -o bci_test bci_test.c -lxtos-bci
sudo ./bci_test /dev/xtos0
```

Expected output:

```
XTOS BCI Test Program
=====================

Opening device: /dev/xtos0

Device Information:
  Name: OpenBCI Cyton
  Device ID: 0
  Channels: 8
  Sampling Rate: 250 Hz
  Resolution: 24 bits
  Input Range: 4500.0 mV

Starting data stream...
Reading data (press Ctrl+C to stop)...

Sample #10: 12.34 -5.67 8.90 ... µV
Sample #20: 14.23 -6.78 9.12 ... µV
...
```

### Python Example

```python
import ctypes
import struct

# Load library
lib = ctypes.CDLL('/usr/lib/libxtos-bci.so')

# Create session
session = lib.xtos_bci_session_create(b'/dev/xtos0')

# Start streaming
lib.xtos_bci_start(session)

# Read samples
buffer = (ctypes.c_float * 8)()
while True:
    ret = lib.xtos_bci_read_samples(session, buffer, 8)
    if ret > 0:
        print([buffer[i] for i in range(8)])

# Cleanup
lib.xtos_bci_stop(session)
lib.xtos_bci_session_destroy(session)
```

---

## API Reference

### Session Management

```c
// Create session
xtos_bci_session_t *session = xtos_bci_session_create("/dev/xtos0");

// Get device info
struct xtos_bci_device_info info;
xtos_bci_get_info(session, &info);

// Cleanup
xtos_bci_session_destroy(session);
```

### Data Streaming

```c
// Start streaming
xtos_bci_start(session);

// Read samples (blocking)
float buffer[8];
ssize_t n = xtos_bci_read_samples(session, buffer, 8);

// Stop streaming
xtos_bci_stop(session);
```

### Configuration

```c
// Set sampling rate
xtos_bci_set_sampling_rate(session, 500);  // 500 Hz

// Enable/disable channel
xtos_bci_set_channel(session, 0, true, 24);  // Ch0, enabled, gain=24
```

### Zero-Copy Access

```c
// Map ring buffer
size_t size;
void *buf = xtos_bci_get_mmap_buffer(session, &size);

// Use with poll/select
int fd = xtos_bci_get_fd(session);
struct pollfd pfd = {.fd = fd, .events = POLLIN};
poll(&pfd, 1, -1);
```

---

## Troubleshooting

### Module not loading

```bash
# Check dependencies
lsmod | grep xtos
modinfo drivers/xtos/bci/xtos_bci_core.ko

# Check kernel log
dmesg | tail -50
```

### Device not appearing

```bash
# Verify module loaded
lsmod | grep xtos_bci

# Check device registration
ls -l /dev/xtos*
cat /sys/class/xtos_bci/*/name
```

### No data received

1. Verify device is streaming: `cat /sys/class/xtos_bci/xtos0/streaming`
2. Check serial connection: `dmesg | grep openbci`
3. Test serial port: `cat /dev/ttyUSB0` (should show binary data)
4. Verify baud rate: `stty -F /dev/ttyUSB0` (should be 115200)

### Permission denied

```bash
# Add user to dialout group
sudo usermod -a -G dialout $USER

# Or change device permissions
sudo chmod 666 /dev/xtos0
```

---

## Performance Tuning

### CPU Affinity

```bash
# Bind IRQ to specific CPU
echo 2 > /proc/irq/<IRQ>/smp_affinity_list
```

### Buffer Size

Increase ring buffer size in driver code:

```c
dev->ringbuf = xtos_bci_ringbuf_alloc(
    &dev->device,
    dev->num_channels * dev->sampling_rate * sizeof(float) * 4  // 4 sec
);
```

### Real-Time Priority

```bash
# Run test with RT priority
sudo chrt -f 50 ./bci_test /dev/xtos0
```

---

## Supported Devices

| Device | Status | Channels | Interface | Driver |
|--------|--------|----------|-----------|--------|
| OpenBCI Cyton | ✅ Ready | 8 | Serial/BT | openbci_cyton |
| OpenBCI Ganglion | ⏳ Planned | 4 | Bluetooth | - |
| NeuroSky MindWave | ⏳ Planned | 1 | Bluetooth | - |
| Emotiv EPOC+ | ⏳ Planned | 14 | USB | - |
| Muse S | ⏳ Planned | 4 | Bluetooth | - |

---

## Development

### Adding New Device Driver

1. Create driver file: `drivers/xtos/bci/devices/mydevice.c`
2. Implement operations:

```c
static const struct xtos_bci_device_ops mydevice_ops = {
    .init = mydevice_init,
    .cleanup = mydevice_cleanup,
    .start_stream = mydevice_start_stream,
    .stop_stream = mydevice_stop_stream,
    // ... other ops
};
```

3. Register device:

```c
static struct xtos_bci_dev mydevice_dev = {
    .name = "My Device",
    .num_channels = 4,
    .sampling_rate = 250,
    .ops = &mydevice_ops,
};

xtos_bci_register_device(&mydevice_dev);
```

4. Add to Kconfig and Makefile

---

## Resources

- Design Document: `/root/xtos-project/docs/driver_design.md`
- Implementation Report: `/root/xtos-project/docs/driver_implementation.md`
- Example Code: `/root/xtos-project/examples/`
- Kernel API: `/root/xtos-project/kernel/linux/include/linux/xtos_bci.h`
- User API: `/root/xtos-project/sdk/libxtos-bci/include/xtos/bci.h`

---

## Support

For issues and questions:
- Check kernel logs: `dmesg | grep xtos`
- Review driver design document
- Examine example code

---

**Last Updated**: 2026-09-30  
**Version**: 1.0
