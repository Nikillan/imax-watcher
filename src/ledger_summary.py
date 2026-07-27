"""One-line summary of the most recent run, for the poll.yml commit message."""
from __future__ import annotations

import json
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ledger_path = REPO_ROOT / "data" / "runs.jsonl"
    rows = [json.loads(line) for line in open(ledger_path) if line.strip()]
    if not rows:
        print("unknown")
        return 0
    last_run_id = rows[-1]["run_id"]
    recent = [r for r in rows if r["run_id"] == last_run_id]
    ok = sum(1 for r in recent if r["status"] == "ok")
    rows_written = sum(r["rows_written"] for r in recent)
    print(f"{last_run_id}: {ok}/{len(recent)} venues ok, {rows_written} rows")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
