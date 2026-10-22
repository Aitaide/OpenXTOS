"""Run the local AF_UNIX ONNX inference service."""
from __future__ import annotations

import argparse
import signal
import sys
import threading
from pathlib import Path

from .server import InferenceService, UnixInferenceServer


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--socket", type=Path, required=True)
    args = parser.parse_args(argv)
    service = InferenceService(args.model_root)
    try:
        server = UnixInferenceServer(args.socket, service)
    except (OSError, ValueError) as exc:
        print(f"xtos-ai-runtime: cannot create socket: {exc}", file=sys.stderr)
        try:
            service.close()
        except Exception as cleanup_exc:
            print(f"xtos-ai-runtime: cleanup failed: {cleanup_exc}", file=sys.stderr)
        return 2
    stopped = threading.Event()
    def stop(_signum, _frame):
        if not stopped.is_set():
            stopped.set()
            threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve_forever()
    finally:
        try:
            service.close()
        except Exception as exc:
            print(f"xtos-ai-runtime: shutdown failed: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
