import logging
import sys

logger = logging.getLogger(__name__)


def main():
    """Entry point for `comfy-bridge` console script and `python -m bridge.cli`."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    import uvicorn
    from .config import Settings
    from .web.app import app  # noqa: F401 — triggers route registration

    host = Settings.BRIDGE_HOST
    port = Settings.BRIDGE_PORT

    logger.info(f"Starting Comfy Bridge on http://{host}:{port}")
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, exiting gracefully.")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
