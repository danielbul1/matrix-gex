from __future__ import annotations

import argparse

from fastapi import FastAPI
import uvicorn

from tripity_experiment.company_api import create_company_connector_api

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765


def build_app() -> FastAPI:
    """Build the local Tripity company connector demo app."""
    return create_company_connector_api()


def demo_url(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> str:
    shown_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    return f"http://{shown_host}:{port}/company-demo"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Tripity company connector demo")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"Host to bind (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to bind (default: {DEFAULT_PORT})")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    app = build_app()
    print("Tripity company connector demo")
    print(f"Open: {demo_url(host=args.host, port=args.port)}")
    print(f"Operator token (paste into the demo to approve writes): {app.state.operator_token}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
