# Independent action approval gate

This is an executable **module contract**, not a runner integration. It never
imports the inference service and never treats `action_allowed`, chat text,
`stdin=yes`, an actor label, or possession of a nonce as approval authority.
All actions submitted here require approval; risk classification is outside scope.

## Trusted management

From a root-controlled installation (never a user-writable Python import path):

```sh
python3 -m xtos_action_gate init
python3 -m xtos_action_gate approve --request reviewed.json --actor admin-console --ttl-seconds 60
```

The only production location is `/var/lib/xtos/action-gate`. No environment
variable, request field, or CLI option can select a different authority store.
Both management and eventual execution broker must run as root. Do **not**
install Python setuid or grant arbitrary callers access to the management CLI.
OS identity/access control is the authentication boundary; `actor` is audit text.
The CLI has no execution command. `FixtureStore(path)` is an explicit Python
injection for tests only, and must never be exposed to untrusted clients.

The store checks every ancestor with dirfd + `O_NOFOLLOW`, refuses writable
ancestors, pins its directory inode for the store object's lifetime, and requires
exact owner/group and 0700 directory / 0600 regular single-link file modes.
A private 256-bit key HMAC-binds the complete approval body (actor, issuer UID,
nonce, normalized request SHA-256, issue/expiry). A privileged attacker who can
read/replace that key or roll back the entire trusted filesystem is outside the
threat model. Filesystem snapshots must not restore spent approvals.

## Request and executor boundary

See `tests/test_action_gate.py:request()` for the fixed `xtos.action.v1` shape.
Every field is required; unknown fields are denied. The binding includes exact
argv order, absolute lexical root/cwd, nonzero uid/gid, CPU/address-space/nofile/
nproc limits, timeout, writable directories and versioned sandbox policy. The
sandbox version is a policy identity, **not proof of applied confinement**.
Directories in `writable_dirs` are absolute paths inside the sandbox root;
`root` and `cwd` are host-side paths, matching the current runner contract.
Canonicalization sorts JSON object keys, preserves array ordering and string
codepoints, and rejects duplicate keys, NaN/Infinity, floats in integer fields,
booleans as integers, unsafe path spelling, oversized input and unknown schemas.
There are no implicit executor options or defaults in the approved contract.

```python
from xtos_action_gate import ActionGate, TrustedStore
# executor is a trusted adapter, not a caller-selected callable or command shell.
result = ActionGate(TrustedStore()).execute(nonce, request_bytes, executor)
```

`execute` consumes first, validates the approved complete snapshot and invokes
the adapter directly with recursively immutable mappings/tuples. It never returns
an authorization boolean that can later be paired with mutable caller data. The
adapter must use **only** this snapshot, enforce every policy/limit, use fixed
argv (no shell), scrub inherited environment/descriptors, and revalidate/open
root/cwd/executable resources safely. This module binds request **values**, not
filesystem contents, executable hashes or root directory inode identity. Those
resource TOCTOU guarantees remain the executor integration's responsibility.
The tests use marker callbacks, not real commands or actual runner confinement.

## One-shot and failure semantics

A bounded flock serializes each authority store. Publishing is fsynced private
file -> atomic rename -> directory fsync. Execution is atomic pending -> spent
rename -> directory fsync **before parsing or callback**. A crash before durable
consume cannot have called the executor; a crash after consume may have executed
zero or one time and is never retried automatically. Malformed requests, changed
parameters, expiry, record corruption and executor exceptions consume the nonce
once the store is accessible; invalid nonce syntax, unavailable/unsafe storage
or lock timeout deny without guaranteed consumption. A fsync failure denies
without calling the executor. A valid spent marker is never deleted/recycled.
This is at-most-once invocation, not exactly-once external side effects.
Approval validity is rechecked immediately before the callback; trusted wall
clock rollback before issuance is denied. Clock/filesystem administration is
trusted, not an adversary-controlled expiration oracle.

Focused regression command (stdlib only):

```sh
python3 -m unittest discover -s tests -p test_action_gate.py -v
```
