#ifndef XTOS_CYTON_SERIAL_H
#define XTOS_CYTON_SERIAL_H
#include <signal.h>
#include <stddef.h>
#include <stdint.h>
struct cyton_board_config {
    /* Complete bounded v reply before $$$. Firmware identity, NOT a unique ID. */
    char identity[256];
    uint32_t gain[8];
    uint32_t enabled_mask;
};
struct cyton_serial_options {
    const char *device;
    unsigned duration_ms, command_timeout_ms, reconnect_delay_ms, reconnect_attempts;
};
enum cyton_serial_event { CYTON_CONNECTED, CYTON_BYTES, CYTON_DISCONNECTED };
typedef int (*cyton_serial_callback)(enum cyton_serial_event,
    const struct cyton_board_config *, const unsigned char *, size_t,
    uint64_t generation, uint64_t receive_ns, void *);
struct cyton_serial_stats { uint64_t connections, reconnects, bytes, disconnects, stop_errors; };
/* Synchronous, bounded-callback userland session. Caller owns signal handlers;
 * stop must remain alive and may be set by a handler. Cancellation is successful
 * unless cleanup fails. Return -1 with errno; original errors survive cleanup.
 * Poll checks stop at most every 10ms; callbacks must themselves return boundedly.
 * Each BYTES buffer is valid only for its callback. No parser bytes cross a
 * generation; consumer must reset its packet parser on CONNECTED/DISCONNECTED.
 * Duration includes discovery/reconnect. Final s gets one nonblocking attempt
 * even after cancellation/deadline; no unbounded tcdrain or blocking I/O.
 */
int cyton_serial_run(const struct cyton_serial_options *,
    volatile sig_atomic_t *, cyton_serial_callback, void *, struct cyton_serial_stats *);
#endif
