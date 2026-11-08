"""Expose the ASGI app lazily so offline schema tools have no startup effects."""

__all__ = ["app"]


def __getattr__(name):
    if name == "app":
        from .main import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
