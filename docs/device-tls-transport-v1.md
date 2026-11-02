# Device TLS transport v1

## Scope and trust boundary

`tools/xtos-devices/tls_transport.py` provides `TLSTransport`, a TCPTransport subclass for explicit bounded TLS bytes. It implements no vendor wire protocol, discovery, scanning, radio activation, action authorization, or physical device identity. Vendor and physical acceptance gates remain open.

The launcher must provide the numeric IP address/port, independently trusted `server_hostname`, and explicit `cafile`. Never derive trust names or CA paths from discovery/network records. The trust file and optional credentials are trusted local launch configuration and must be regular accessible files; loading local credentials is synchronous and is outside a socket-poll scheduling guarantee. No context/verification override, plaintext fallback, system-root fallback, or TLS downgrade is exposed.

## API

```python
from tls_transport import TLSTransport

t = TLSTransport(
    '127.0.0.1', 443,
    server_hostname='device.example.test',
    cafile='/trusted/launch/device-ca.pem',
    # Optional mTLS: supply both explicit paths.
    # certfile='/trusted/launch/client.pem',
    # keyfile='/trusted/launch/client.key',
)
try:
    t.open(deadline_ns=absolute_monotonic_ns)
    t.write_all(b'payload', deadline_ns=absolute_monotonic_ns)
    data = t.read(132, deadline_ns=absolute_monotonic_ns)
finally:
    t.close()
```

The socket is nonblocking. The TLS context requires `CERT_REQUIRED`, hostname checking against SAN (no common-name fallback), and TLS 1.3. `wrap_socket(..., do_handshake_on_connect=False)` is followed by an actual handshake with SSLWantRead/SSLWantWrite polling. `connected` remains false and `security_info` remains None until negotiated TLS and peer certificate verification complete. Immutable `SecurityInfo(protocol, cipher, server_hostname)` describes this transport session only; it never grants device/action authentication. It resets on close. Raw/private socket attributes are internal and never returned by the public API.

`read(max_bytes, *, deadline_ns)` accepts only integer 1..132 and returns nonempty bytes or raises. It can consume already-decrypted pending TLS data without waiting for new ciphertext. `write_all(data, *, deadline_ns)` accepts bytes of length 1..8192 and returns None after all bytes have been submitted. Both operations handle SSL wants in either direction and retain one absolute positive integer monotonic-nanosecond deadline; no retry restarts the budget. Partial writes before timeout/cancellation are possible: neither remote receipt nor command execution is guaranteed.

The existing NetworkError/NetworkTimeout/NetworkCancelled/NetworkDisconnected hierarchy is retained. SSL, peer verification and wrapper failures are transport errors. EOF is terminal for the operation; like TCPTransport, the allocation-oriented `connected` flag does not become a liveness oracle after remote EOF. The caller closes the session.

## Ownership, cancellation and cleanup

Open/read/write/close belong to one session-owner thread. Only `request_cancel()` is cross-thread. The inherited lifetime lock protects wake-descriptor notification against close/reuse; cancellation is latched, retried wake writes are bounded, and poll fallback uses at most 50 ms slices, subject to OS scheduling. Inherited TCP connection acquisition precedes the TLS phase; cancellation becomes available once its wake pair is published. A pre-open cancel is a no-op, matching TCPTransport. TLS handshake waits and data waits observe cancellation; post-syscall checks refuse publication after observed cancellation or deadline expiry.

Failed handshake/wrapping closes the TLS/raw socket and both wake FDs. Close is idempotent; reuse starts a fresh session without an old cancellation latch. Close drops the socket without a blocking TLS unwrap/shutdown exchange. mTLS requires both explicit cert/key paths. Encrypted private-key interactive prompts are refused; key bytes are never logged or placed in security metadata.

## Verification and limits

Run from the repository root:

```sh
python3 tests/test_xtos_tls_transport.py -v
python3 tests/test_xtos_network_transport.py -v
```

The TLS fixture generates an ephemeral OpenSSL CA and localhost SAN certificate in a private temporary scratch directory. It binds/connects only 127.0.0.1; keys are deleted after testing and excluded from repository snapshots/reports. Certificate subject/issuer/validity/SAN/fingerprint metadata may be recorded independently. OpenSSL CLI is a test dependency; production uses Python stdlib ssl and network_transport.

Real loopback cases cover TLS 1.3 handshake/transfer, mTLS, hostname/CA refusal, plaintext/TLS 1.2 refusal, handshake timeout/cancellation, EOF, idle read timeout, cancellation/reopen, input/owner checks and failure cleanup. Controlled SSL seam injections supplement real TLS to force rare cross-direction wants and publication boundaries; these are fault-injection software evidence, not a claim that every SSL state arose naturally. Tests use Events for scheduling barriers.

TDD evidence under `.build/remaining-closure-v2/tls-transport-implement/` retains increment source/test/dependency bytes and actual logs. Entry failure is missing-API evidence; handshake, optional-mTLS API, and explicit-path validation have separate RED/GREEN increments. Later negative/edge cases passed against that implementation and are supplemental verification, not independently observed RED cycles. An initial test syntax error and a later fixture-release failure are preserved and excluded from behavioral RED claims.

No full repository suite, rootfs/image inclusion, installer/SBOM integration, hardware, vendor compatibility, physical identity, real-time performance, revocation/OCSP, certificate pinning, ALPN, or device-action acceptance is established by this slice. Independent SPEC/QUALITY review and parent integration remain required.
