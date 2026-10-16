# XTOS Package Sources

Status: complete / 2026-10-16

Two layers, deliberately separate:

1. **System base (deb lock → signed local repository)** — the userspace rootfs stays an immutable, atomically-published image. Packages are never installed at runtime.
2. **Applications (xpkg)** — third-party apps install into an isolated `/opt/xtos-apps` tree without touching the system closure.

## 1. Self-hosted userspace source (`scripts/userspace_repo.py`)

Turns a userspace lock (`system/packages/locks/*.json`) into a signed local repository:

```
repo/pool/<deb>          exact locked archives (sha256 + size verified)
repo/manifest.json       deterministic index (lock identity + package list)
repo/SHA256SUMS          sha256 of manifest.json + every pool archive
repo/SHA256SUMS.sig      detached ed25519 signature (openssl pkeyutl -rawin)
repo/manifest.json.sig   detached ed25519 signature
repo/public.pem          repository public key (committed at system/packages/repo-public.pem)
```

- `create --lock <json> --repo <dir> [--cache-dir <dir>] [--publish-public-key <pem>]`
  downloads each lock `source_url` (deb.debian.org / security.debian.org only, https only),
  verifies sha256+size against the lock, then signs the index. Re-running resumes:
  pool archives with matching hashes are reused. The private signing key lives
  OUTSIDE the repository tree (`XTOS_USERSPACE_REPO_KEY` or
  `~/.xtos-keys/userspace-repo-ed25519.pem`, mode 600) and is never committed.
- `verify --repo <dir> [--lock <json>]` checks both signatures, every SHA256SUMS
  entry, pool coverage, stray files and lock agreement. Rejects `.part` leftovers.
- `populate --repo <dir> --packages-dir <dir> --lock <json>` reconstitutes the flat
  packages dir consumed by `scripts/build_userspace.sh --lock … --packages-dir …`
  after full repository verification — fully offline assembly, no apt contact.

Verified 2026-10-16 against `live-x86_64-v4.json`: 362/362 archives downloaded
(381,525,696 bytes), signed, verified, populated and accepted by
`userspace.preflight` (hashes + dpkg-deb control fields). Tests:
`tests/test_userspace_repo.py` (12 cases: tamper, stray, signature, mismatch).

## 2. xpkg application packages (`pkg app ...`)

Format (tar.gz, see `docs/xpkg-spec.md`):

```
payload/...              application files (regular files + dirs only)
xpkg/manifest.json       canonical JSON metadata (indent=2, sort_keys)
xpkg/SHA256SUMS          "<sha256>  payload/<path>" lines
```

Install path: `/opt/xtos-apps/<name>/<version>/` with `registry.json` under
`/opt/xtos-apps` (flock-guarded, atomic tmp+rename). `XTOS_APP_ROOT` overrides
the root for tests.

Security model (enforced by `pkg app install`, mirrored by the gate):

- listing-first validation: only `payload/` and `xpkg/` roots, no absolute or
  `..` components, no member may double as a directory prefix of another member
  (blocks symlink/hardlink extraction escapes);
- post-extract scan: regular files only, `st_nlink == 1`, no setuid/setgid/sticky;
- three-way set equality: disk payload == SHA256SUMS == manifest `files`, with
  per-file sha256 and size verification before activation;
- activation is `rename()` of the staging directory; registry update is atomic.

Commands: `pkg app install|remove|list|verify|info` (`--json` supported).
Tests: `tests/test_native_tools_xpkg.py` (18 cases incl. traversal, shadow,
symlink, hardlink, setuid, tamper, duplicate, junk).

## Publishing gate (`scripts/verify_xpkg.py`)

Every package must pass the gate before it may be listed in any XTOS source.
It repeats all device-side rules and adds gate-only policy: canonical manifest
bytes, ELF machine match (`--arch`), kernel-module/object rejection, file/size
caps (50,000 files / 2 GiB). Exit 0 + JSON verdict. Tests:
`tests/test_verify_xpkg.py` (9 cases). All three suites are wired into
`scripts/test.sh`.

An app store/market front-end is intentionally NOT part of this layer; a
website package index can be generated later from signed repository metadata
once real third-party applications exist.
