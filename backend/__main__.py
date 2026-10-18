"""Start the backend: `python -m backend [--board simulated] [--port 8000]`.

The app is expected to launch this process itself, pass a random token via
BCI_BACKEND_TOKEN, and talk to it on 127.0.0.1. If no token is given one
is generated and printed on stdout as `BCI_BACKEND_TOKEN=<token>`.
"""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import secrets
import sys
from pathlib import Path

import uvicorn

from .api import create_app
from .settings import BOARD_KINDS, BOARD_MUSE, Settings


def main(argv: list[str] | None = None) -> int:
    defaults = Settings()
    p = argparse.ArgumentParser(prog="python -m backend", description="BCI-VR local backend")
    p.add_argument("--host", default="127.0.0.1", help="API bind address (keep on loopback)")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--board", choices=BOARD_KINDS, default=BOARD_MUSE,
                   help="muse (real headset), synthetic (BrainFlow), simulated (reacts to cues)")
    p.add_argument("--serial", default=None, help="Muse name, e.g. Muse-15C3")
    p.add_argument("--data-root", type=Path, default=defaults.data_root)
    p.add_argument("--unity-ws-host", default=defaults.unity_ws_host,
                   help="Quest prediction socket bind address (0.0.0.0 for Wi-Fi)")
    p.add_argument("--unity-ws-port", type=int, default=defaults.unity_ws_port)
    p.add_argument("--no-auth", action="store_true", help="Disable the API token (development only)")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger("backend")

    try:
        loopback = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        loopback = args.host == "localhost"
    if not loopback:
        log.warning("API bound to %s — anyone on the network can reach it", args.host)

    token = None
    if not args.no_auth:
        token = os.environ.get("BCI_BACKEND_TOKEN")
        if not token:
            token = secrets.token_urlsafe(24)
            print(f"BCI_BACKEND_TOKEN={token}", flush=True)

    settings = Settings(
        data_root=args.data_root,
        board=args.board,
        serial=args.serial,
        token=token,
        unity_ws_host=args.unity_ws_host,
        unity_ws_port=args.unity_ws_port,
    )
    uvicorn.run(create_app(settings), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
