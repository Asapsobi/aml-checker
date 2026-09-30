"""A corridor system's call to amlcheck before it settles: the PRD's Phase 5 exit test.

It uses the standard library only, as a corridor team might. It sends POST /v1/check with the token
and an Idempotency-Key, checks that the answer holds the stable JSON contract (PRD §10.3), and says
whether the transfer may go ahead. Only NO_HITS lets it go ahead, and even that is not a clearance.

A timeout or a 409 is retried with the same Idempotency-Key, so a retry never makes a second check:
it gets the first check's result.

    AMLCHECK_API_TOKEN=... python3 scripts/corridor_mock.py <address> [amount]

Exit status: 0 go ahead (NO_HITS), 2 hold (BLOCK, REVIEW or INCOMPLETE), 1 the call failed.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

BASE = os.environ.get("AMLCHECK_API_URL", "http://127.0.0.1:8766")
VERDICTS = {"BLOCK", "REVIEW", "INCOMPLETE", "NO_HITS"}
# The contract's fields and their JSON types (docs/api.md).
FIELDS: dict[str, type | tuple[type, ...]] = {
    "check_id": str,
    "created_at": str,
    "address": str,
    "chain": str,
    "verdict": str,
    "sources": list,
    "findings": list,
    "amount_hint": (str, type(None)),
    "operator_note": (str, type(None)),
    "client": (str, type(None)),
    "tool_version": str,
    "config_hash": str,
    "record_hash": str,
    "attribution": list,
}
SOURCE_FIELDS = ("source", "label", "required", "status", "as_of", "summary", "meta")
FINDING_FIELDS = ("rule_id", "severity", "priority", "source", "summary", "evidence", "observed_at")


def contract_problems(result: dict[str, Any]) -> list[str]:
    """What in an answer breaks the contract; empty when it holds."""
    found = []
    for name, kind in FIELDS.items():
        if name not in result:
            found.append(f"missing {name}")
        elif not isinstance(result[name], kind):
            found.append(f"{name} has the wrong type")
    if result.get("verdict") not in VERDICTS:
        found.append(f"unknown verdict {result.get('verdict')!r}")
    if result.get("chain") not in ("tron", "bsc"):
        found.append(f"unknown chain {result.get('chain')!r}")
    for source in result.get("sources") or []:
        found += [f"a source lacks {f}" for f in SOURCE_FIELDS if f not in source]
    for finding in result.get("findings") or []:
        found += [f"a finding lacks {f}" for f in FINDING_FIELDS if f not in finding]
    return found


def screen(
    base: str,
    token: str,
    address: str,
    amount: str | None = None,
    *,
    key: str | None = None,
    attempts: int = 5,
    timeout: float = 300,
) -> tuple[dict[str, Any], bool]:
    """The check of an address, and whether it was an earlier request's result (a replay)."""
    body = {"address": address, "client": "corridor-mock"}
    if amount is not None:
        body["amount"] = amount
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Idempotency-Key": f'"{key or uuid.uuid4()}"',
    }
    for attempt in range(1, attempts + 1):
        request = urllib.request.Request(
            f"{base}/v1/check", data=json.dumps(body).encode(), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                replayed = response.headers.get("Idempotent-Replayed") == "true"
                return json.load(response), replayed
        except urllib.error.HTTPError as e:
            if e.code != 409 or attempt == attempts:
                problem = json.load(e) if e.headers.get_content_type().endswith("json") else {}
                raise RuntimeError(f"HTTP {e.code}: {problem.get('detail') or e.reason}") from e
        except (TimeoutError, urllib.error.URLError):
            if attempt == attempts:
                raise
        time.sleep(2)  # the same key again: the first request's result, once it is ready
    raise AssertionError("unreachable")


def main(argv: list[str]) -> int:
    if len(argv) not in (2, 3):
        print(__doc__, file=sys.stderr)
        return 1
    token = os.environ.get("AMLCHECK_API_TOKEN")
    if not token:
        print("Set AMLCHECK_API_TOKEN to the token amlcheck api was given.", file=sys.stderr)
        return 1
    try:
        result, replayed = screen(BASE, token, argv[1], argv[2] if len(argv) == 3 else None)
    except (RuntimeError, OSError) as e:
        print(f"The check failed: {e}. Hold the transfer.", file=sys.stderr)
        return 1
    broken = contract_problems(result)
    if broken:
        print(f"The answer breaks the contract: {'; '.join(broken)}. Hold.", file=sys.stderr)
        return 1
    go = result["verdict"] == "NO_HITS"
    print(
        f"{result['verdict']} for {result['address']} ({result['chain']}), check"
        f" {result['check_id']}{' (replayed)' if replayed else ''}:"
        f" {'go ahead (not a clearance)' if go else 'hold the transfer'}"
    )
    return 0 if go else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
