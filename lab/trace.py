"""Follows one request through the services by its request id.

    python -m lab.trace <request_id> --run results/final/ft2/X1/rep1
    python -m lab.trace --failed --run results/final/ft2/X1/rep1     # ids of failed requests

Reads the service logs stored with a run (``logs.jsonl.gz``). Every service writes the id of the
request it is serving into each JSON log line, and passes it on in calls to other services.
"""

import argparse
import gzip
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path


def load(run: Path) -> list[dict]:
    entries = []
    with gzip.open(run / "logs.jsonl.gz", "rt", encoding="utf-8") as file:
        for line in file:
            row = json.loads(line)
            if row["text"].startswith("{"):
                try:
                    row["json"] = json.loads(row["text"])
                except json.JSONDecodeError:
                    continue
                entries.append(row)
    return entries


def trace(entries: list[dict], request_id: str) -> list[dict]:
    return [e for e in entries if e["json"].get("request_id") == request_id]


def failed_ids(entries: list[dict]) -> Counter:
    return Counter(
        e["json"]["request_id"]
        for e in entries
        if e["json"].get("event") == "request_failed" and "request_id" in e["json"]
    )


def show(rows: list[dict]) -> None:
    for row in rows:
        data = row["json"]
        stamp = datetime.fromtimestamp(row["t"], UTC).strftime("%H:%M:%S.%f")[:-3]
        extra = {
            k: v
            for k, v in data.items()
            if k
            not in ("ts", "level", "service", "instance", "node", "logger", "event", "request_id")
        }
        print(
            f"{stamp} {data['level']:<7} {data['service']:<10} {data['instance']:<12} "
            f"{data['event']} {json.dumps(extra, default=str) if extra else ''}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("request_id", nargs="?")
    parser.add_argument("--run", required=True, type=Path)
    parser.add_argument("--failed", action="store_true", help="list ids of failed requests")
    args = parser.parse_args()
    entries = load(args.run)
    if args.failed:
        for request_id, count in failed_ids(entries).most_common(20):
            print(request_id, count)
        return
    rows = trace(entries, args.request_id)
    if not rows:
        raise SystemExit(f"no log lines with request id {args.request_id}")
    show(rows)


if __name__ == "__main__":
    main()
