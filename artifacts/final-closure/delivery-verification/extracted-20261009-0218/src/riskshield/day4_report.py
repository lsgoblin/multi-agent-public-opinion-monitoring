"""Compatibility imports for the former day4 report module."""
from . import reporting as _implementation
for _name in dir(_implementation):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_implementation, _name)
del _name, _implementation
