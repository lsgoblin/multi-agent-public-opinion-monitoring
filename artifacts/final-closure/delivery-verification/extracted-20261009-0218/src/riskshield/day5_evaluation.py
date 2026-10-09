"""Compatibility entry for the former day5 evaluation module."""
from . import evaluation as _implementation
for _name in dir(_implementation):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_implementation, _name)
del _name, _implementation
if __name__ == "__main__":
    from .evaluation import main
    raise SystemExit(main())
