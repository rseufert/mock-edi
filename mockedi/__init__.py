"""mock-edi - an open source mock EDI trading partner.

The mock speaks what trading partners speak - ASC X12 and UN/EDIFACT over AS2
- and answers the way one does: an order arrives, an acknowledgment goes back,
then a purchase order response, a despatch advice and an invoice.  It runs no
business logic of its own beyond deciding what to confirm, and it keeps its
state in SQLite.
"""
import os
import re

__all__ = ["Config", "make_server", "__version__"]


def _discover_version():
    """pyproject.toml is the single source of truth for the version.

    Running from a checkout, the pyproject.toml sitting next to the package is
    authoritative and the version is marked `+source`; stale build metadata in
    the working tree (a leftover *.egg-info directory, say) would otherwise
    shadow it and report a version that has already moved on. Installed - in
    site-packages, a wheel, a container - there is no pyproject.toml alongside,
    and the version recorded at build time is read instead.
    """
    pyproject = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "pyproject.toml")
    try:
        with open(pyproject, encoding="utf-8") as handle:
            match = re.search(r'^version\s*=\s*"([^"]+)"', handle.read(), re.M)
        if match:
            return match.group(1) + "+source"
    except OSError:
        pass
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8
        return "0+unknown"
    try:
        return version("mock-edi")
    except PackageNotFoundError:
        return "0+unknown"


__version__ = _discover_version()

from .server import Config, make_server  # noqa: E402,F401
