"""
sim_driver.py — Compatibility shim.

The simulator-backend classes now live under :mod:`symfuzz.drivers`. This
module re-exports the xsim driver as ``SimDriver`` so existing imports keep
working unchanged.

Use :func:`symfuzz.drivers.make_driver` for new code that needs to select a
backend at runtime.
"""
from .drivers.base import BaseStdioDriver
from .drivers.xsim import XsimDriver as SimDriver

__all__ = ["SimDriver", "BaseStdioDriver"]
