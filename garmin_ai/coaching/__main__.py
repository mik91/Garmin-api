"""CLI so any agent with a shell can use the guardrails.

  python -m garmin_ai.coaching validate  input.json   # {"plan": {...}, "history": {...}}
  python -m garmin_ai.coaching readiness daily.json --as-of 2026-11-16 [--reported sick,pain]
Exit code 1 when validation finds errors.
"""
import argparse
import json
import sys

from . import assess_readiness, validate_week


def main() -> int:
    p = argparse.ArgumentParser(prog="garmin_ai.coaching")
    sub = p.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate")
    v.add_argument("file")
    r = sub.add_parser("readiness")
    r.add_argument("file")
    r.add_argument("--as-of", required=True)
    r.add_argument("--reported", default="")
    a = p.parse_args()
    with open(a.file, encoding="utf-8") as f:
        data = json.load(f)
    if a.cmd == "validate":
        out = validate_week(data["plan"], data["history"])
        print(json.dumps(out, indent=2))
        return 0 if out["ok"] else 1
    out = assess_readiness(data, a.as_of, [x for x in a.reported.split(",") if x])
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
