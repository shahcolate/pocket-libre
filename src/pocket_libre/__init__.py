"""Pocket Libre: Liberate your Pocket AI recorder from the cloud."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth is the version in pyproject.toml, so the
    # package and `pocket-libre --version` can never disagree.
    __version__ = version("pocket-libre")
except PackageNotFoundError:  # source tree without an install
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
