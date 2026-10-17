# xpkg Format Specification (v1)

Status: stable / 2026-10-16

xpkg is the XTOS application package format for third-party apps. System base
packages are NOT xpkg — they stay in the locked deb repository (see
`docs/package_sources.md`). Applications install into an isolated tree and
never modify the system closure.

## Archive

`<name>-<version>-<arch>.xpkg` is a POSIX tar.gz with exactly these roots:

```
payload/...          application payload (regular files and directories only)
xpkg/manifest.json   canonical metadata (see below)
xpkg/SHA256SUMS      "<64 lowercase hex>  payload/<path>" lines, sha256sum format
```

Rules (enforced by `pkg app install` and by the `scripts/verify_xpkg.py`
publishing gate):

- Member names start with `payload/` or `xpkg/` only. No absolute paths, no
  `.`/`..` components, no control characters, length < 1024, ≤ 65536 members.
- Regular files and directories only. Symlinks, hardlinks, FIFOs, sockets and
  devices are rejected. Extracted payload files must have `st_nlink == 1` and
  no setuid/setgid/sticky bits.
- No member may double as a directory prefix of another member (blocks
  extraction escapes through symlink/hardlink shadows).
- Duplicate member names are rejected (tar would silently overwrite).
- `xpkg/SHA256SUMS` and the manifest `files` array must describe exactly the
  extracted payload set: same paths, same sizes, same sha256.
- Directory members must be listed (tar-compatible listing validation).

### Native reader (no external tar binary)

`pkg` unpacks xpkg archives with its own reader (`tools/xtos-native/common/`
`xtos_gzip.h` + `xtos_ustar.h`); no `tar` process is ever executed. The
reader accepts: POSIX ustar headers (name + prefix), GNU long-name/long-link
meta members (`L`/`K`), PAX per-file extended headers (`x`: `path=`, `size=`)
and PAX global headers (`g`, parsed and deliberately ignored — the listing
validator stays the audit authority). It verifies the gzip magic/CM/flags,
all DEFLATE block types (stored/fixed/dynamic), per-member CRC32 and ISIZE,
and accepts concatenated members plus zero padding. It rejects links and
devices, base-256 size encodings (>8 GiB members), bad header checksums,
oversized or malformed meta members, and any absolute/traversal/dirty name
before a single byte is written. The full listing is audited (the same
`tar -t`-style list validation as before) before extraction into the
staging directory.

## manifest.json

Canonical form: `json.dumps(manifest, indent=2, sort_keys=True) + "\n"`.
The gate rejects any other byte form (the on-device parser is line-based).

```json
{
  "architecture": "x86_64 | all",
  "files": [{"path": "payload/...", "sha256": "<hex>", "size": <bytes>}],
  "license": "<SPDX or name>",
  "name": "<[a-z0-9][a-z0-9-]{0,63}, not \"apps\">",
  "schema_version": 1,
  "summary": "<one-line description>",
  "vendor": "<who publishes it>",
  "version": "<[A-Za-z0-9][A-Za-z0-9.+~-]{0,63}>",
  "xtos_min_version": "<minimum XTOS release>"
}
```

## Install layout

```
/opt/xtos-apps/registry.json          {"schema_version":1,"apps":{<name>:{version,files,bytes,xpkg_sha256,installed_utc}}}
/opt/xtos-apps/<name>/<version>/payload/...
/opt/xtos-apps/<name>/<version>/xpkg/manifest.json,SHA256SUMS
```

`registry.json` updates hold `flock` on `/opt/xtos-apps/registry.lock` and are
written tmp+rename (atomic). Install extracts into a private staging directory,
verifies everything, then `rename()`s it into place; failures remove the
staging directory and leave no trace. `XTOS_APP_ROOT` overrides the root for
testing. Checksums are integrity, not authentication: source authentication is
provided by the signed repository index that lists admitted packages.

## CLI (pkg app ...)

```
pkg app install <file.xpkg>   verify + install (refuses existing app/version)
pkg app upgrade <file.xpkg>   verify + upgrade an installed application
pkg app remove <name>         remove installed app + registry entry
pkg app list                  registry + on-disk reconciliation
pkg app verify <name>         re-hash installed payload against SHA256SUMS
pkg app info <name>           manifest details + install record
```

`--json` output is supported for every command. Piped output stays plain
ASCII (shared `xtos_ui.h` styling rules).

`upgrade` replaces an installed application with a new version atomically:
the new payload is staged and fully verified first, then activated by
`rename()`, and the registry write is the commit point — a failure there
rolls the payload back and leaves the old version authoritative. Old payload
directories are dropped only after the commit. A stale target directory left
by an interrupted upgrade is cleaned up by the next attempt. Upgrading to
the already-installed version is refused.

## Gate-only policy

`scripts/verify_xpkg.py --xpkg <file> [--arch x86_64] [--report <json>]`
additionally enforces: canonical manifest bytes, ELF `e_machine` match for the
target architecture, kernel module/object rejection (`.ko`, `.o`), and payload
caps (50,000 files / 2 GiB). A package that fails the gate must never be
listed in a public XTOS source. v1 reserves manifest keys for a future store
index (icon, description, categories) without changing the archive format.
