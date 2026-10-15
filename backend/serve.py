"""Local development server based on gevent.

Gunicorn does not run on Windows, so local development uses
``gevent.pywsgi`` to keep the same concurrency model as production.

Usage: ``uv run python serve.py {admin|proxy} [port]``
"""

import sys

from gevent import monkey

monkey.patch_all()

# pylint: disable=wrong-import-position
from gevent.pywsgi import WSGIServer  # noqa: E402

DEFAULT_PORTS = {"admin": 5000, "proxy": 8000}


def main():
    """Start the selected application."""
    if len(sys.argv) < 2 or sys.argv[1] not in DEFAULT_PORTS:
        sys.exit(__doc__)
    name = sys.argv[1]
    port = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_PORTS[name]
    if name == "admin":
        from lgw.admin import create_app
    else:
        from lgw.proxy import create_app
    print(f"Serving {name} on http://127.0.0.1:{port}")
    WSGIServer(("0.0.0.0", port), create_app()).serve_forever()


if __name__ == "__main__":
    main()
