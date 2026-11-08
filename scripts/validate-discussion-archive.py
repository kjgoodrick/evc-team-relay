#!/usr/bin/env python3
"""Offline validation only; run with the control-plane virtual environment."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps" / "control-plane"))

from app.services.discussion_archive_validation import (  # noqa: E402
    ArchiveValidationError,
    validate_archive,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path, help="Completed scope or collection directory")
    args = parser.parse_args()
    try:
        result = validate_archive(args.archive)
    except ArchiveValidationError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
