"""
core/tunnel.py — ngrok tunnel integration for local image serving.

This wrappers the local HTTP ImageServer and opens an ngrok tunnel
to expose the local port to the internet with a public HTTPS URL.
This allows Meta's servers to fetch the image even if the user is
behind a NAT/firewall without needing manual port forwarding.
"""

import os
from pyngrok import conf, ngrok

from core.logger import get_logger
from core.server import ImageServer

logger = get_logger("Tunnel")

class TunneledImageServer:
    """
    Context manager that starts the local ImageServer AND an ngrok tunnel.
    Yields the public base URL of the tunnel.
    """
    def __init__(self, serve_dir: str, port: int = 8765):
        self.serve_dir = serve_dir
        self.port = port
        self.server = ImageServer(
            serve_dir=serve_dir, 
            port=port, 
            public_base_url=f"http://localhost:{port}"
        )
        self.public_url = None
        self.tunnel = None

        # Apply ngrok auth token if provided
        auth_token = os.getenv("NGROK_AUTH_TOKEN")
        if auth_token:
            conf.get_default().auth_token = auth_token

    def __enter__(self) -> str:
        # Start local HTTP server
        self.server.__enter__()
        
        # Start ngrok tunnel pointing to local server
        logger.info(f"Starting ngrok tunnel on local port {self.port}...")
        
        # Try to connect, use bind_tls=True for HTTPS only
        self.tunnel = ngrok.connect(self.port, bind_tls=True)
        self.public_url = self.tunnel.public_url
        
        logger.info(f"✅ ngrok tunnel established: {self.public_url}")
        
        return self.public_url
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        # Disconnect ngrok
        if self.public_url:
            logger.info(f"Stopping ngrok tunnel: {self.public_url}")
            try:
                ngrok.disconnect(self.public_url)
            except Exception as e:
                logger.warning(f"Error disconnecting ngrok tunnel: {e}")
        
        # Stop local server
        self.server.__exit__(exc_type, exc_val, exc_tb)
