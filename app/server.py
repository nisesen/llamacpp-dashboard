"""Entry point for uvicorn: `uvicorn server:app`. The code is in inferenceinquire/."""

from inferenceinquire.api import app

__all__ = ["app"]
