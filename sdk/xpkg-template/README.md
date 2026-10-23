# xpkg app template

Minimal skeleton for an XTOS third-party application package.

```
payload/bin/hello-world      application payload (regular files only)
```

## Build (unsigned — integrity only)

```
python3 scripts/make_xpkg.py \
  --root sdk/xpkg-template/payload \
  --name hello-world --version 1.0.0 \
  --summary "Template hello world" \
  --vendor "Your Company" --license MIT \
  --output hello-world-1.0.0-x86_64.xpkg
```

## Build (signed — third-party authentication)

```
openssl genpkey -algorithm ed25519 -out vendor-ed25519.pem     # once, keep private
openssl pkey -in vendor-ed25519.pem -pubout -out vendor-pub.pem

python3 scripts/make_xpkg.py \
  --root sdk/xpkg-template/payload \
  --name hello-world --version 1.0.0 \
  --summary "Template hello world" \
  --vendor "Your Company" --license MIT \
  --sign vendor-ed25519.pem \
  --output hello-world-1.0.0-x86_64.xpkg
```

Distribute the `.xpkg` plus your `vendor-pub.pem` (never the private key).

## Publish gate (run before distributing)

```
python3 scripts/verify_xpkg.py --xpkg hello-world-1.0.0-x86_64.xpkg --pubkey vendor-pub.pem
```

## Install on an XTOS system

```
pkg app install hello-world-1.0.0-x86_64.xpkg --pubkey vendor-pub.pem
```

The signature binds the vendor key to the manifest, and the manifest pins
every payload file's sha256 — so `--pubkey` authenticates the whole package.
Without `--pubkey` the same package still installs with hash-verified
integrity (no vendor authentication). See docs/xpkg-dev-guide.md.
