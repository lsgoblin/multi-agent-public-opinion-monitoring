"""Strip byte-order marks that a shell writer added, then report what changed.

PowerShell's Set-Content -Encoding UTF8 writes a BOM; json.load chokes on it. This
normalises every text file in the directory to UTF-8 without BOM and prints the ones
that had one.
"""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
BOM = b"\xef\xbb\xbf"

fixed = []
broken = []
for path in sorted(HERE.rglob("*")):
    if not path.is_file() or path.suffix not in {".json", ".csv", ".md", ".py", ".txt"}:
        continue
    raw = path.read_bytes()
    if raw.startswith(BOM):
        path.write_bytes(raw[len(BOM):])
        fixed.append(path.relative_to(HERE).as_posix())

for path in sorted(HERE.rglob("*.json")):
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - reported, not raised
        broken.append("%s: %s" % (path.relative_to(HERE).as_posix(), exc))

print("files that had a BOM: %d" % len(fixed))
for name in fixed:
    print("   %s" % name)
print("json files that still fail to parse: %d" % len(broken))
for name in broken:
    print("   %s" % name)
