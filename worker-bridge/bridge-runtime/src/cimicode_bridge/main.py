from __future__ import annotations

import argparse

import uvicorn


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="cimicode-bridge")
    # 兼容历史入口词：entrypoint/CMD 可能传入 0 个、1 个甚至重复的 "run"，
    # 全部作为多余位置参数吞掉，避免 "unrecognized arguments: run"。
    parser.add_argument("legacy", nargs="*", help=argparse.SUPPRESS)
    parser.add_argument("--config", default="config/bridge.example.yaml", help="Path to YAML config")
    parser.add_argument("--host", default="0.0.0.0", help="Host interface for uvicorn")
    parser.add_argument("--port", type=int, default=8081, help="Port for uvicorn")
    parser.add_argument("--debug", action="store_true", help="Enable debug logging")
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn autoreload")
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    uvicorn.run(
        "cimicode_bridge.app:create_app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        factory=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
