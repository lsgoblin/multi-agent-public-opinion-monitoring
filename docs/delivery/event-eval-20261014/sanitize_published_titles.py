"""Replace verbatim third-party headline text with a hash reference in the published copies.

Why
---
Five files in this directory carried the exact wording of headlines the project fetched
from a public index. The rest of the project publishes derived values only: the frozen
benchmark JSONs state their scope as "event identifiers, counts, and standardized
exclusion reasons only. Does not copy titles, label values, raw inputs, prompts, or
predictions."

The annotation is still checkable without the wording: every row keeps the text hash, so
a reader can confirm the text was frozen and unchanged, and the full text remains in the
ignored local copy under artifacts/final-94-event-result/ where the run happened.

This script runs after any regeneration, because the generators write the text back.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent

#
# NOTE: this file must never rewrite itself. The list below is the only place in the
# published copy where the wording is needed, and replacing it would disarm the script.
SELF = Path(__file__).resolve()
TITLES = [
    "FTC sues insulin middlemen for pocketing billions while patients face high costs",
    "UnitedHealth says a likely 'nation-state actor' behind Change Healthcare attack",
    "Americans React to UnitedHealthcare CEO's Murder: 'My Empathy Is Out of Network'",
    "Change Healthcare Experiencing Security Incident",
    "US health tech giant Change Healthcare hit by cyberattack",
    "Pharmacies across US disrupted following hack at Change Healthcare network",
    "Change Healthcare is experiencing a cyber security issue",
    "Cyberattack on Change Healthcare was an exploit of the ConnectWise flaw",
    "Network disruptions persist after Change Healthcare cyberattack",
    "US pharmacy outage triggered by Russian Blackcat ransomware at UnitedHealth unit",
    "Ransomware attack blamed for Change Healthcare outage stalling US prescriptions",
    "Ransomware causing nationwide pharmacy and hospital disruption",
    "UnitedHealthcare CEO Brian Thompson fatally shot in Manhattan",
    "UnitedHealthcare executive killed in Manhattan in targeted attack",
    "UnitedHealth's Effort to Deny Coverage for a Patient's Care (2023)",
    "Manhunt for UnitedHealthcare CEO Killer Meets Obstacle: Sympathy for Gunman",
    "The UnitedHealthcare Gunman Understands the Surveillance State",
    "Person of interest in shooting of UNH CEO ID'd as an ex-Ivy League, CS grad",
    "Suspect detained in manhunt for UnitedHealthcare CEO Brian Thompson's killer",
]

PLACEHOLDER = "<verbatim title held in the ignored local copy; verify by text_sha256>"

# The model echoed a quoted phrase back in its own words, so a free-text reason field can
# carry the wording after the titles are gone. The label, model, tokens and hash binding
# are what make a row checkable, so the reason is replaced instead.
REASON_PLACEHOLDER = "<model reason withheld from the published copy; it quoted the source title>"


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


changed: list[str] = []
for path in sorted(HERE.rglob("*")):
    if not path.is_file() or path.suffix not in {".json", ".py", ".md", ".csv"}:
        continue
    if path.resolve() == SELF:
        continue  # never rewrite the list this script depends on
    text = path.read_text(encoding="utf-8")
    original = text
    for title in TITLES:
        if title in text:
            text = text.replace(title, PLACEHOLDER)
    # a script that embeds the titles as source data also needs its example prompts trimmed
    text = re.sub(r'标题：\{title\}', '标题：<见冻结输入>', text)

    # reason fields in the prediction document
    if path.suffix == ".json" and '"reason"' in text:
        try:
            doc = json.loads(text)
        except json.JSONDecodeError:
            doc = None
        if isinstance(doc, dict) and isinstance(doc.get("predictions"), dict):
            touched = False
            for row in doc["predictions"].values():
                if isinstance(row, dict) and row.get("reason"):
                    row["reason"] = REASON_PLACEHOLDER
                    touched = True
            if touched:
                text = json.dumps(doc, ensure_ascii=False, indent=2) + "\n"

    if text != original:
        path.write_text(text, encoding="utf-8", newline="\n")
        changed.append(path.relative_to(HERE).as_posix())

# Report which published files still carry a frozen text hash, so a reader can see the
# linkage survives the substitution.
linkage = {}
for path in sorted(HERE.rglob("*.json")):
    doc = json.loads(path.read_text(encoding="utf-8"))
    found = []

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key in {"text_sha256", "text_sha256_prefix", "frozen_text_sha256",
                           "input_package_sha256"} and isinstance(value, str):
                    found.append((key, value[:16]))
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(doc)
    if found:
        linkage[path.relative_to(HERE).as_posix()] = found

print("published files sanitised: %d" % len(changed))
for name in changed:
    print("   %s" % name)
print()
print("text hashes still present, so the substitution stays checkable:")
for name, entries in linkage.items():
    keys = sorted({k for k, _ in entries})
    print("   %-52s %s" % (name, ", ".join(keys)))
