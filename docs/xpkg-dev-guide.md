# Packaging third-party applications for XTOS (xpkg developer guide)

xpkg is the only way user applications enter an XTOS system. Applications
install into an isolated tree (`/opt/xtos-apps/<name>/<version>/`) and never
modify the immutable system closure. Format rules: `docs/xpkg-spec.md`.

## Trust model — two levels, both explicit

1. **Repository apps** (listed in a public XTOS package source). The signed
   release index authenticates them; on-device `pkg` verifies every file hash.
   No per-app signature is required — the source's pinned root key is the
   trust anchor.
2. **Third-party apps** (distributed out of band by a vendor). The vendor
   signs the package with their own ed25519 key; the user passes the vendor
   public key at install time:

   ```
   pkg app install myapp.xpkg --pubkey vendor-pub.pem
   ```

   The signature covers the exact `manifest.json` bytes, the manifest's
   `files` array pins every payload path+size+sha256, and `pkg` re-hashes
   every payload byte before activation — so vendor key -> manifest ->
   payload is a complete chain. Without `--pubkey` the signature is inert
   metadata and the install is integrity-only (level-1 semantics).

**What is deliberately not claimed**: there is no key-revocation
infrastructure and no global vendor registry in v1.1. Revocation means:
stop trusting the key (do not pass `--pubkey`), and remove the app
(`pkg app remove <name>`). For repository apps, the source stops listing
the package in the next signed index.

## Build a package

```
python3 scripts/make_xpkg.py \
  --root <your-payload-directory> \    # its files become payload/
  --name my-app --version 1.2.3 \
  --summary "One line" \
  --vendor "Your Company" --license Apache-2.0 \
  --sign vendor-ed25519.pem \           # optional, enables --pubkey installs
  --output my-app-1.2.3-x86_64.xpkg
```

Payload rules (enforced at build and again at install): regular files and
directories only — no symlinks, hardlinks, devices or setuid bits; no
kernel modules or objects; x86_64 ELF payloads must match the target
architecture (`all` is allowed for scripts/data). A working skeleton:
`sdk/xpkg-template/`.

## Check before distributing

```
python3 scripts/verify_xpkg.py --xpkg my-app-1.2.3-x86_64.xpkg \
       [--pubkey vendor-pub.pem]
```

The gate enforces everything the device installer enforces, plus canonical
manifest bytes, ELF architecture policy and payload caps (50,000 files /
2 GiB). A package that fails the gate must not be distributed.

## Ship it

Distribute three things: the `.xpkg`, `vendor-pub.pem`, and the install
line. Users install with `pkg app install <file> --pubkey vendor-pub.pem`,
upgrade with `pkg app upgrade <file> --pubkey vendor-pub.pem`, inspect
with `pkg app info <name>` and remove with `pkg app remove <name>`.

Keep the private key on the build host only. If it is lost or leaks, stop
shipping under that key and publish a new public key with the next
release — already-installed apps keep running unchanged.
