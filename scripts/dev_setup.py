"""Create or update the local .env from .env.example with generated secrets.

Usage:  python scripts/dev_setup.py [--force]

* No .env yet: writes one, replacing every "change_me" value with a random hex secret.
* .env exists: appends only keys that are new in .env.example (existing values, such as
  database passwords already baked into data volumes, are never changed).
* --force: regenerate everything (existing volumes will no longer accept the old passwords).
"""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / ".env.example"
TARGET = ROOT / ".env"


def _generated(value: str) -> str:
    # 32 random bytes as hex: URL-safe in connection strings and valid as a 256-bit key.
    return secrets.token_hex(32) if "change_me" in value else value


def _entries(text: str) -> list[tuple[str, str] | str]:
    entries: list[tuple[str, str] | str] = []
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and not line.lstrip().startswith("#"):
            entries.append((key.strip(), value))
        else:
            entries.append(line)
    return entries


def render_new(template: str) -> str:
    out = [f"{e[0]}={_generated(e[1])}" if isinstance(e, tuple) else e for e in _entries(template)]
    return "\n".join(out) + "\n"


def missing_lines(template: str, existing: str) -> list[str]:
    present = {e[0] for e in _entries(existing) if isinstance(e, tuple)}
    lines: list[str] = []
    for entry in _entries(template):
        if isinstance(entry, tuple) and entry[0] not in present:
            lines.append(f"{entry[0]}={_generated(entry[1])}")
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--force", action="store_true", help="regenerate all secrets")
    args = parser.parse_args()
    template = TEMPLATE.read_text(encoding="utf-8")

    if not TARGET.exists() or args.force:
        TARGET.write_text(render_new(template), encoding="utf-8")
        print(f"Wrote {TARGET} with generated development secrets.")
        return 0

    additions = missing_lines(template, TARGET.read_text(encoding="utf-8"))
    if not additions:
        print(f"{TARGET} is up to date.")
        return 0
    with TARGET.open("a", encoding="utf-8") as fh:
        fh.write("\n# Added by scripts/dev_setup.py\n" + "\n".join(additions) + "\n")
    names = ", ".join(line.split("=", 1)[0] for line in additions)
    print(f"Added {len(additions)} new key(s) to {TARGET}: {names}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
