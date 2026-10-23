#!/usr/bin/env python3
"""CLI for offline, root-scoped userspace ELF closure verification."""
from userspace import verify_main

if __name__ == '__main__':
    raise SystemExit(verify_main())
