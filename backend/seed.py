"""CLI helpers for the demo namespace.

Examples::

    python -m backend.seed seed --db .data/haizhizi.db
    DEMO_MODE=true python -m backend.seed reset --db .data/haizhizi.db
"""

from __future__ import annotations

import argparse

from .db import Database


def main() -> int:
    parser = argparse.ArgumentParser(description="筑福链 demo data")
    parser.add_argument("action", choices=("seed", "reset"))
    parser.add_argument("--db", default="backend/data/demo.sqlite3")
    args = parser.parse_args()
    db = Database(args.db)
    if args.action == "seed":
        print(db.seed_demo())
    else:
        db.reset_demo()
        print({"reset": True, "seed": db.seed_demo()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
