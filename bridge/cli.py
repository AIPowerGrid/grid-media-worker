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

    import argparse

    parser = argparse.ArgumentParser(
        prog="comfy-bridge",
        description="AI Power Grid media worker bridge for ComfyUI.",
    )
    parser.add_argument(
        "--install-service", action="store_true",
        help="start the bridge automatically (Windows login / systemd / launchd)",
    )
    parser.add_argument(
        "--uninstall-service", action="store_true",
        help="remove the auto-start installation",
    )
    parser.add_argument(
        "--service-status", action="store_true",
        help="show whether auto-start is installed",
    )
    args = parser.parse_args()

    if args.service_status:
        from . import service
        service.status()
        sys.exit(0)
    if args.uninstall_service:
        from . import service
        sys.exit(0 if service.uninstall() else 1)
    if args.install_service:
        from . import service
        ok = service.install(start=True)
        if ok and sys.platform == "win32":
            # The Run key alone only fires at the NEXT login — also start the
            # bridge now, after a short delay in case a bridge is exiting.
            service.schedule_start()
        sys.exit(0 if ok else 1)

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
