"""Arranque del hub: `rsshub` o `python -m rsshub`."""

from __future__ import annotations

import argparse
import logging

import uvicorn
from rsscore.config import Config


def main() -> None:
    parser = argparse.ArgumentParser(description="Hub headless del lector RSS")
    parser.add_argument("--config", help="ruta al config.yaml")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--no-scheduler", action="store_true", help="no refrescar feeds")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    cfg = Config.load(args.config)
    host = args.host or cfg.hub.host
    port = args.port or cfg.hub.port

    from .app import create_app

    app = create_app(cfg, with_scheduler=not args.no_scheduler)
    logging.getLogger("rsshub").info("Escuchando en http://%s:%d (sin interfaz web)", host, port)
    uvicorn.run(app, host=host, port=port, log_level=args.log_level)


if __name__ == "__main__":
    main()
