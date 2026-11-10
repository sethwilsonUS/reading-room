"""Worker-free backend for Vite's loopback-only development proxy."""
from __future__ import annotations

from .local_server import DEFAULT_BASE_URL
from .service import Library
from .web import create_app


def create_dev_app():
    library = Library()
    # The browser continues to use Vite on 18765. The backend's transport port
    # must not leak into durable reading/citation links.
    library._local_base_url = DEFAULT_BASE_URL
    return create_app(library, enable_intake_worker=False)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("book_library.dev_web:create_dev_app", factory=True,
                host="127.0.0.1", port=18766, reload=True,
                reload_dirs=[str(Path(__file__).parent)])
