#!/usr/bin/env python3
"""PhotoFrame & Shadow Game — Launch script."""

import sys
import os
import threading

def check_dependencies():
    """Check and install missing dependencies."""
    missing = []
    try:
        import fastapi
    except ImportError:
        missing.append("fastapi")
    try:
        import socketio
    except ImportError:
        missing.append("python-socketio")
    try:
        import sqlalchemy
    except ImportError:
        missing.append("sqlalchemy")
    try:
        import PIL
    except ImportError:
        missing.append("Pillow")

    if missing:
        print(f"Missing dependencies: {', '.join(missing)}")
        print("Installing from requirements.txt...")
        os.system(f"{sys.executable} -m pip install -r requirements.txt")
        print("Dependencies installed. Restarting...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


def run_http_redirect(https_port):
    """Tiny HTTP server that redirects everything to HTTPS."""
    from http.server import HTTPServer, BaseHTTPRequestHandler

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            host = self.headers.get("Host", "").split(":")[0] or "photoframe.local"
            self.send_response(301)
            self.send_header("Location", f"https://{host}:{https_port}{self.path}")
            self.end_headers()
        do_POST = do_HEAD = do_GET

        def log_message(self, format, *args):
            pass  # silent

    http_port = 80 if https_port == 443 else https_port - 1  # 8079 for 8080
    try:
        server = HTTPServer(("0.0.0.0", http_port), RedirectHandler)
        server.serve_forever()
    except OSError:
        # Port 8079/80 unavailable — skip redirect, HTTPS still works
        pass


def main():
    check_dependencies()

    import uvicorn
    app_dir = os.path.dirname(os.path.abspath(__file__))
    os.chdir(app_dir)
    from server import socket_app, SERVER_PORT

    print("\n" + "=" * 50)
    print("  PhotoFrame & Shadow Game")
    print("=" * 50)

    # Plain HTTP — self-signed HTTPS breaks mobile phones (no trusted CA)
    uvicorn.run(
        socket_app,
        host="0.0.0.0",
        port=SERVER_PORT,
        log_level="info",
    )

if __name__ == "__main__":
    main()
