from __future__ import annotations

import argparse

import uvicorn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="cimicode-bridge")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the bridge service")
    run_parser.add_argument("--config", default="config/bridge.example.yaml", help="Path to YAML config")
    run_parser.add_argument("--host", default="0.0.0.0", help="Host interface for uvicorn")
    run_parser.add_argument("--port", type=int, default=8081, help="Port for uvicorn")
    run_parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    run_parser.add_argument("--reload", action="store_true", help="Enable uvicorn autoreload")

    # Backward-compatible: allow flags directly without the run subcommand.
    parser.add_argument("--config", default="config/bridge.example.yaml", help=argparse.SUPPRESS)
    parser.add_argument("--host", default="0.0.0.0", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, default=8081, help=argparse.SUPPRESS)
    parser.add_argument("--debug", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--reload", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    config = getattr(args, "config", "config/bridge.example.yaml")
    host = getattr(args, "host", "0.0.0.0")
    port = getattr(args, "port", 8081)
    reload = getattr(args, "reload", False)

    uvicorn.run(
        "cimicode_bridge.app:create_app",
        host=host,
        port=port,
        reload=reload,
        factory=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
