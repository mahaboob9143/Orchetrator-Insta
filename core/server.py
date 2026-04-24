"""
core/server.py — Temporary local HTTP image server for InstaAgent.

Meta Graph API requires images to be served from a publicly reachable URL.
This module starts a lightweight stdlib http.server in a background daemon
thread to serve the media/queue directory, then shuts it down once the
image container has been created by Meta.

REQUIREMENT: Your machine must have a publicly reachable IP address on the
configured port (see image_server.public_base_url in config.yaml).
If behind NAT, forward port 8765 → this machine's local IP on your router.

Usage (context manager):
    with ImageServer(serve_dir, port, public_base_url) as srv:
        url = srv.get_public_url("abc123.jpg")
        # Meta downloads from url ...
"""

import functools
import http.server
import threading
from core.logger import get_logger

logger = get_logger("ImageServer")


class _SilentHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that suppresses access log output."""

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        pass  # intentionally silent — don't mix HTTP logs with agent logs


class ImageServer:
    """
    Context-manager-based temporary HTTP file server.

    Serves a directory over HTTP so Meta Graph API can download images.
    The server runs in a background daemon thread and is shut down
    automatically when the context manager exits.

    Args:
        serve_dir:       Absolute path to the directory to serve.
        port:            TCP port to listen on (default: 8765).
        public_base_url: Public-facing base URL (e.g. "http://203.0.113.42").
                         Meta uses this to download uploaded images.
    """

    def __init__(self, serve_dir: str, port: int, public_base_url: str):
        self.serve_dir = serve_dir
        self.port = port
        self.public_base_url = public_base_url.rstrip("/")
        self._httpd: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Start the HTTP server in a background daemon thread."""
        handler = functools.partial(_SilentHandler, directory=self.serve_dir)
        self._httpd = http.server.HTTPServer(("", self.port), handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="ImageServer",
            daemon=True,    # dies automatically when main process exits
        )
        self._thread.start()
        logger.info(
            f"Image server started — serving '{self.serve_dir}' on port {self.port}. "
            f"Public base: {self.public_base_url}"
        )

    def stop(self) -> None:
        """Shut down the HTTP server gracefully."""
        if self._httpd:
            self._httpd.shutdown()   # signals serve_forever() to stop
            if self._thread:
                self._thread.join(timeout=5)
            logger.info("Image server stopped")
        self._httpd = None
        self._thread = None

    # ── Context manager support ───────────────────────────────────────────────

    def __enter__(self) -> "ImageServer":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    # ── URL builder ───────────────────────────────────────────────────────────

    def get_public_url(self, filename: str) -> str:
        """
        Build the publicly reachable URL for an image file.

        Args:
            filename: Bare filename (e.g. "a1b2c3d4.jpg"), not a full path.

        Returns:
            Full public URL (e.g. "http://203.0.113.42:8765/a1b2c3d4.jpg").
        """
        return f"{self.public_base_url}:{self.port}/{filename}"
