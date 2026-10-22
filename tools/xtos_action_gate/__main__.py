"""Root management CLI only. Deliberately no execution or fixture-store option."""
import argparse
import json
import os
from pathlib import Path
import sys

from . import ActionGate, GateError, TrustedStore
from .schema import MAX_JSON, snapshot


def main(argv=None):
    parser = argparse.ArgumentParser(description="Independent root approval management")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="create fixed private authority store, once")
    approve = commands.add_parser("approve", help="approve a reviewed exact request")
    approve.add_argument("--request", type=Path, required=True)
    approve.add_argument("--actor", required=True, help="audit label, NOT authentication")
    approve.add_argument("--ttl-seconds", type=int, required=True)
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise GateError("root management authority required")
        if args.command == "init":
            TrustedStore(initialize=True)
            result = {"status": "initialized"}
        else:
            # Read bytes once, bound them, then approve that same snapshot.
            with args.request.open("rb") as stream:
                raw = stream.read(MAX_JSON + 1)
            encoded, _ = snapshot(raw)
            token = ActionGate(TrustedStore()).approve(
                encoded, actor=args.actor, ttl_seconds=args.ttl_seconds)
            result = {"status": "approved", "nonce": token}
        print(json.dumps(result, sort_keys=True))
        return 0
    except (GateError, OSError) as exc:
        print(json.dumps({"status": "denied", "error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
