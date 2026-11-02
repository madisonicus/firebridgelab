"""Where the library looks for its data files.

The data directory is resolved, in order, from:

1. the ``FIREBRIDGELAB_DATA`` environment variable;
2. ``data/cache/`` in a source checkout (this package sits next to a
   ``pyproject.toml``);
3. the user cache directory, ``$XDG_CACHE_HOME/firebridgelab`` or
   ``~/.cache/firebridgelab``.

``firebridgelab-fetch-data`` downloads into the same directory. The
directory is resolved when the data modules are first imported, so set the
environment variable before ``import firebridgelab``.
"""

from __future__ import annotations

import os
from pathlib import Path


ENV_VAR = "FIREBRIDGELAB_DATA"

_PACKAGE_DIR = Path(__file__).resolve().parent

_PACKAGE_DIR = Path(__file__).resolve().parents[1]  # the firebridgelab package
def checkout_root() -> Path | None:
    """The repository root when running from a source checkout, else None."""
    root = _PACKAGE_DIR.parent
    if (root / "pyproject.toml").is_file() and (root / "firebridgelab").is_dir():
        return root
    return None


def user_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "firebridgelab"


def data_dir() -> Path:
    """The directory that holds the fetched and built data files."""
    env = os.environ.get(ENV_VAR)
    if env:
        return Path(env).expanduser()
    root = checkout_root()
    if root is not None:
        return root / "data" / "cache"
    return user_cache_dir()


def missing_data_message(path: Path) -> str:
    return (
        f"data file not found: {path}. Run `firebridgelab-fetch-data` to "
        f"download it (it writes to {data_dir()}), or set {ENV_VAR} to a "
        "directory that already holds the data."
    )
