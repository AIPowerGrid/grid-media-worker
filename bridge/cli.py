import errno
import logging
import socket
import sys

logger = logging.getLogger(__name__)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# EADDRINUSE spellings: POSIX errno, winsock (10048), macOS (48).
PORT_IN_USE_ERRNOS = frozenset({errno.EADDRINUSE, 10048, 48})


def port_in_use(host: str, port: int) -> bool:
    """True if something is already listening on (host, port)."""
    try:
        with socket.create_connection((host, port), timeout=1):
            return True
    except OSError:
        return False


def _explain_port_conflict(port: int) -> None:
    logger.error(
        "Port %s is already in use — another Comfy Bridge (or another app) is "
        "listening there. Stop the other instance, or set BRIDGE_PORT to a "
        "free port and restart.",
        port,
    )


def validated_bridge_host(value: object) -> str:
    host = str(value or "").strip().lower()
    if host not in LOOPBACK_HOSTS:
        raise RuntimeError(
            "Comfy Bridge UI may bind only to loopback; use an SSH tunnel for remote access"
        )
    return host


def main():
    """Entry point for `comfy-bridge` console script and `python -m bridge.cli`."""
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    import uvicorn
    from .config import Settings
    from .web.app import app  # noqa: F401 — triggers route registration

    host = validated_bridge_host(Settings.BRIDGE_HOST)
    port = Settings.BRIDGE_PORT

    logger.info(f"Starting Comfy Bridge on http://{host}:{port}")
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except KeyboardInterrupt:
        logger.info("Received keyboard interrupt, exiting gracefully.")
        sys.exit(0)
    except SystemExit:
        # uvicorn exits itself on a failed bind after printing only the raw
        # socket error; add the plain-words cause when the port is taken.
        if port_in_use(host, port):
            _explain_port_conflict(port)
        raise
    except OSError as e:
        if getattr(e, "errno", None) in PORT_IN_USE_ERRNOS:
            _explain_port_conflict(port)
            sys.exit(1)
        logger.error(f"Fatal error: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
