"""Deterministic smoke command for the Skill execution pipeline."""

from __future__ import annotations

import json
import sys


def main() -> int:
    print(
        json.dumps(
            {
                "ok": True,
                "kind": "skill-script",
                "arguments": sys.argv[1:],
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
