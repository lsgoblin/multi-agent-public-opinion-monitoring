"""Compatibility imports for the former day3 module; use riskshield.simulation."""
from . import simulation as _implementation
for _name in dir(_implementation):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_implementation, _name)
del _name, _implementation
