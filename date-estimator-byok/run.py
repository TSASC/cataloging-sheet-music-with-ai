#!/usr/bin/env python3
import os
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
VENV = ROOT / "venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main():
    if sys.version_info < (3, 11):
        raise SystemExit("Python 3.11 or newer is required.")
    if not PYTHON.exists():
        subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
    subprocess.run(
        [str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")],
        cwd=ROOT,
        check=True,
    )
    port = int(os.environ.get("PORT", "0"))
    if not port:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["FLASK_DEBUG"] = "0"
    url = f"http://127.0.0.1:{port}"
    print(f"Open {url} in your browser. Press Ctrl+C to stop.", flush=True)
    browser = threading.Timer(2, webbrowser.open, args=(url,))
    browser.daemon = True
    browser.start()
    server = subprocess.Popen([str(PYTHON), "app.py"], cwd=ROOT, env=env)
    try:
        return server.wait()
    except KeyboardInterrupt:
        if server.poll() is None:
            server.terminate()
        server.wait()
        return 0
    finally:
        browser.cancel()


if __name__ == "__main__":
    raise SystemExit(main())
