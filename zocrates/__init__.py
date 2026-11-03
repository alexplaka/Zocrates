from importlib.metadata import PackageNotFoundError, version
from typing import Any

try:
    __version__ = version("zocrates")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"


def __getattr__(name: str) -> Any:
    """Lazily resolve module-level attributes to avoid circular imports at startup.

    Args:
        name: The attribute name being accessed on this module.

    Returns:
        The requested attribute value (currently only ``"app"`` is supported).

    Raises:
        AttributeError: If ``name`` is not a recognised lazy attribute.
    """
    if name == "app":
        from zocrates.agents.app import app

        return app
    raise AttributeError(f"module {__name__} has no attribute {name}")
