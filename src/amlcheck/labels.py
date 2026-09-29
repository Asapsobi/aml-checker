"""The user's own address labels (PRD §6: labels.csv with columns address,chain,tag,note,source).

The file is the source of truth: an import replaces every label, and nothing is imported when any
row is wrong. Tags are free text. Those in `[heuristics] risky_tags` raise R-HEU-05, and the
`allowlist` tag leaves a counterparty out of the behaviour rules.
"""

import csv
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from amlcheck.core.address import AddressError, parse
from amlcheck.core.models import Chain

REQUIRED = ("address", "chain", "tag")


@dataclass(frozen=True)
class Label:
    address_norm: str
    chain: str
    tag: str
    note: str | None
    source: str | None


def read_csv(path: Path) -> tuple[list[Label], list[str]]:
    """The labels in the file, and a description of every row that is wrong."""
    found: dict[tuple[str, str, str], Label] = {}
    problems: list[str] = []
    with path.open(newline="", encoding="utf-8-sig") as file:
        reader = csv.DictReader(file)
        missing = [name for name in REQUIRED if name not in (reader.fieldnames or [])]
        if missing:
            return [], [f"the header lacks {', '.join(missing)}: use address,chain,tag,note,source"]
        for line, row in enumerate(reader, start=2):
            chain_text = (row.get("chain") or "").strip().lower()
            tag = (row.get("tag") or "").strip().lower()
            try:
                chain = Chain(chain_text)
            except ValueError:
                problems.append(f"line {line}: the chain must be tron or bsc, not {chain_text!r}")
                continue
            try:
                address = parse(row.get("address") or "", chain)
            except AddressError as e:
                problems.append(f"line {line}: {e}")
                continue
            if not tag:
                problems.append(f"line {line}: the tag is empty")
                continue
            note = (row.get("note") or "").strip() or None
            source = (row.get("source") or "").strip() or None
            key = (address.normalized, chain.value, tag)
            found[key] = Label(address.normalized, chain.value, tag, note, source)
    return list(found.values()), problems


def replace(conn: sqlite3.Connection, labels: list[Label]) -> None:
    with conn:
        conn.execute("DELETE FROM labels")
        conn.executemany(
            "INSERT INTO labels (address_norm, chain, tag, note, source) VALUES (?, ?, ?, ?, ?)",
            [
                (label.address_norm, label.chain, label.tag, label.note, label.source)
                for label in labels
            ],
        )
