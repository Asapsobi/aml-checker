"""The 2-hop network as a picture (PRD Phase 4, U6): the screened address in the middle, its
counterparties in scope around it, and the sanctioned or frozen wallets that paid them outside.

It is SVG drawn by amlcheck itself, so the web page loads no outside code and keeps its strict
Content-Security-Policy. Colours are set as SVG attributes, never inline styles, and the page's own
stylesheet restyles them for dark mode by class. A state is never told by colour alone: each node
also carries a mark (! flagged, ? not read, H hub), and the legend names them all. The table in
`amlcheck investigate` and on the web page lists the same network as text.

Colours are the reference status palette: critical red for flagged wallets, warning amber for a
counterparty that could not be read, blue for the screened address and grey for one read clean.
Checked with the dataviz validator: every pair stays apart under colour blindness (ΔE ≥ 9.9).
"""

import math
from collections import defaultdict
from decimal import Decimal
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from amlcheck.core.models import CheckResult
from amlcheck.explorer import explorer

TWO_HOP = "exposure_2hop"  # the source that walks the network (adapters/two_hop.py)
WIDTH, HEIGHT = 760, 600
CENTRE = (WIDTH / 2, 270)
RING = {1: 150.0, 2: 238.0}
RADIUS = {"target": 15, "node": 10}
SURFACE = "#fcfcfb"  # light chart surface; the file version is always light
INK = "#0b0b0b"
MUTED = "#52514e"
LINE = "#b5b4ad"
LOOK = {  # state: (class, fill, mark, mark colour, dashed outline)
    "target": ("target", "#2a78d6", "", SURFACE, False),
    "read": ("read", "#8e8d86", "", SURFACE, False),
    "hub": ("hub", SURFACE, "H", MUTED, True),
    "flagged": ("flagged", "#d03b3b", "!", SURFACE, False),
    "not_reached": ("unread", "#fab219", "?", INK, True),
    "failed": ("unread", "#fab219", "?", INK, True),
}
LEGEND = [
    ("target", "this address"),
    ("read", "read, nothing found"),
    ("flagged", "sanctioned or frozen"),
    ("hub", "hub, not read"),
    ("not_reached", "could not be read"),
]


def of(result: CheckResult) -> dict[str, Any] | None:
    """The 2-hop network a check walked, or None when it walked none."""
    walk = next((s for s in result.sources if s.source == TWO_HOP), None)
    network = walk.evidence_meta.get("graph") if walk else None
    return network if isinstance(network, dict) else None


WALK_STATE = {
    "read": "read",
    "flagged": "flagged itself (R-EXP-01)",
    "hub": "a hub, not read",
    "not_reached": "not reached in time",
    "failed": "could not be read",
}


def walk_rows(network: dict[str, Any]) -> list[dict[str, Any]]:
    """The counterparties in scope, as text: what each exchanged with the address, how the walk
    went, and the sanctioned or frozen wallets that paid it. The table beside the picture."""
    target = network["nodes"][0]["id"]
    rows = []
    for node in (n for n in network["nodes"] if n["ring"] == 1):
        edge = next(e for e in network["edges"] if e["from"] == node["id"] and e["to"] == target)
        rows.append(
            {
                "address": node["id"],
                "received": f"{Decimal(edge['received_usdt']):,.2f}",
                "sent": f"{Decimal(edge['sent_usdt']):,.2f}",
                "state": WALK_STATE.get(node["state"], node["state"]),
                "paid_by": [
                    (e["from"], f"{Decimal(e['received_usdt']):,.2f}")
                    for e in network["edges"]
                    if e["to"] == node["id"]
                ],
            }
        )
    return rows


def short(address: str) -> str:
    """A label long enough to tell look-alike addresses apart: address poisoning copies the first
    and last few characters, so four of each are not enough (seen live on 2026-09-30)."""
    return f"{address[:8]}…{address[-6:]}" if len(address) > 16 else address


def _usdt(text: str) -> str:
    return f"{Decimal(text):,.2f} USDT"


def _place(graph: dict[str, Any]) -> dict[str, tuple[float, float, float]]:
    """(x, y, angle) for every node: counterparties evenly round the centre, and each flagged
    sender beside the counterparties it paid."""
    cx, cy = CENTRE
    places: dict[str, tuple[float, float, float]] = {}
    first = [n for n in graph["nodes"] if n["ring"] == 1]
    for i, node in enumerate(first):
        angle = -math.pi / 2 + 2 * math.pi * i / max(len(first), 1)
        places[node["id"]] = (
            cx + RING[1] * math.cos(angle),
            cy + RING[1] * math.sin(angle),
            angle,
        )
    places[graph["nodes"][0]["id"]] = (cx, cy, 0.0)
    paid: dict[str, list[float]] = defaultdict(list)
    for edge in graph["edges"]:
        if edge["to"] in places and edge["to"] != graph["nodes"][0]["id"]:
            paid[edge["from"]].append(places[edge["to"]][2])
    taken: dict[int, int] = defaultdict(int)
    for node in (n for n in graph["nodes"] if n["ring"] == 2):
        angles = paid.get(node["id"]) or [0.0]
        angle = math.atan2(
            sum(math.sin(a) for a in angles), sum(math.cos(a) for a in angles)
        )  # the mean direction of the counterparties it paid
        slot = round(math.degrees(angle) / 10)
        angle += math.radians(11) * taken[slot] * (-1) ** taken[slot]  # fan out a crowd
        taken[slot] += 1
        places[node["id"]] = (
            cx + RING[2] * math.cos(angle),
            cy + RING[2] * math.sin(angle),
            angle,
        )
    return places


def _title(node: dict[str, Any], graph: dict[str, Any], target: str) -> str:
    lines = [node["id"]]
    if node["ring"] == 1:
        edge = next(e for e in graph["edges"] if e["from"] == node["id"] and e["to"] == target)
        lines.append(
            f"sent this address {_usdt(edge['received_usdt'])}, received {_usdt(edge['sent_usdt'])}"
        )
    for edge in (e for e in graph["edges"] if e["from"] == node["id"] and e["to"] != target):
        lines.append(f"paid {edge['to']} {_usdt(edge['received_usdt'])}")
    lines += node.get("flags", [])
    return "\n".join(lines)


def to_svg(graph: dict[str, Any], chain: str, *, standalone: bool = False) -> str:
    """The network in `evidence_meta["graph"]` of a 2-hop result. `standalone` adds a light
    background, for a file; the web page draws it on its own surface, light or dark."""
    target = graph["nodes"][0]["id"]
    places = _place(graph)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}"'
        f' width="{WIDTH}" height="{HEIGHT}" class="walk-graph" role="img"'
        ' font-family="system-ui, -apple-system, sans-serif" font-size="11">',
        "<title>2-hop network</title>",
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerWidth="7"'
        ' markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z"'
        ' fill="#d03b3b" class="arrow"/></marker></defs>',
    ]
    if standalone:
        parts.append(f'<rect width="{WIDTH}" height="{HEIGHT}" fill="{SURFACE}"/>')
    for edge in graph["edges"]:
        (x1, y1, _), (x2, y2, _) = places[edge["from"]], places[edge["to"]]
        if edge["to"] == target:
            parts.append(
                f'<line class="edge" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"'
                f' stroke="{LINE}" stroke-width="1.5"/>'
            )
            continue
        # Stop the arrow at the edge of the counterparty it points to.
        length = math.hypot(x2 - x1, y2 - y1) or 1
        end = (length - RADIUS["node"] - 3) / length
        parts.append(
            f'<line class="edge flagged" x1="{x1:.1f}" y1="{y1:.1f}"'
            f' x2="{x1 + (x2 - x1) * end:.1f}" y2="{y1 + (y2 - y1) * end:.1f}"'
            ' stroke="#d03b3b" stroke-width="2" marker-end="url(#arrow)"/>'
        )
    for node in graph["nodes"]:
        x, y, angle = places[node["id"]]
        state = "target" if node["ring"] == 0 else node["state"]
        css, fill, mark, mark_colour, dashed = LOOK.get(state, LOOK["read"])
        radius = RADIUS["target" if node["ring"] == 0 else "node"]
        link = explorer(chain, node["id"])
        shape = (
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{fill}"'
            f' stroke="{MUTED if dashed else SURFACE}" stroke-width="2"'
            + (' stroke-dasharray="3 2"' if dashed else "")
            + "/>"
        )
        if mark:
            shape += (
                f'<text x="{x:.1f}" y="{y + 4:.1f}" text-anchor="middle" font-weight="700"'
                f' fill="{mark_colour}" class="mark">{mark}</text>'
            )
        if node["ring"] == 0:
            lx, ly, anchor = x, y + radius + 16, "middle"
        else:
            lx = x + (radius + 6) * math.cos(angle)
            ly = y + (radius + 6) * math.sin(angle) + 4
            anchor = (
                "start" if math.cos(angle) > 0.2 else "end" if math.cos(angle) < -0.2 else "middle"
            )
            if anchor == "middle":
                ly += 8 if math.sin(angle) > 0 else -4
        label = (  # a halo in the surface colour keeps a label readable where a line crosses it
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" fill="{MUTED}"'
            f' stroke="{SURFACE}" stroke-width="3" stroke-linejoin="round" paint-order="stroke"'
            f' class="label">{escape(short(node["id"]))}</text>'
        )
        body = f"<title>{escape(_title(node, graph, target))}</title>{shape}{label}"
        if link:
            body = f'<a href={quoteattr(link)} target="_blank" rel="noopener noreferrer">{body}</a>'
        parts.append(f'<g class="node {css}">{body}</g>')
    x = 24.0
    for state, text in LEGEND:  # names every mark, so colour never carries a meaning alone
        css, fill, mark, mark_colour, dashed = LOOK[state]
        parts.append(
            f'<g class="node {css}"><circle cx="{x + 7:.1f}" cy="{HEIGHT - 26}" r="7"'
            f' fill="{fill}" stroke="{MUTED if dashed else SURFACE}" stroke-width="1.5"'
            + (' stroke-dasharray="3 2"' if dashed else "")
            + "/>"
            + (
                f'<text x="{x + 7:.1f}" y="{HEIGHT - 22.5}" text-anchor="middle"'
                f' font-size="9" font-weight="700" fill="{mark_colour}" class="mark">{mark}</text>'
                if mark
                else ""
            )
            + f'<text x="{x + 20:.1f}" y="{HEIGHT - 22}" fill="{INK}" class="legend">'
            f"{escape(text)}</text></g>"
        )
        x += 20 + len(text) * 6.2 + 18
    parts.append("</svg>")
    return "".join(parts)
