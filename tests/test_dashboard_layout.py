"""
Layout checks: titles and tables fit a 1440 px window with Grafana's side menu docked.

The geometry was measured in Grafana 12.4.14 (`grafana/README.md`, "Size"): the grid is about
1,108 px wide, a panel title gets `46.17 * w - 106` px (74 without a description icon), a table
gets `46.17 * w - 26` px, and a column with no width defaults to 150 px.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS = sorted((ROOT / "grafana/dashboards").glob("*.json"))
UNIT_PX = 1108 / 24
DEFAULT_COLUMN_PX = 150
# Panel-title glyph widths in px (Inter 14 px, weight 500), measured in Grafana 12.4.14.
GLYPH_PX = {
    " ": 3.88, "!": 4.41, '"': 7.07, "#": 9.09, "$": 9.19, "%": 14.05, "&": 9.3, "'": 4.53,
    "(": 5.31, ")": 5.31, "*": 7.44, "+": 9.49, ",": 4.4, "-": 6.62, ".": 4.4, "/": 5.33,
    "0": 9.19, "1": 5.96, "2": 8.78, "3": 8.93, "4": 9.34, "5": 8.8, "6": 8.97, "7": 8.41,
    "8": 8.96, "9": 8.97, ":": 4.4, ";": 4.57, "<": 9.49, "=": 9.49, ">": 9.49, "?": 7.53,
    "@": 13.9, "A": 10.27, "B": 9.34, "C": 10.42, "D": 10.25, "E": 8.59, "F": 8.4, "G": 10.62,
    "H": 10.57, "I": 3.96, "J": 8.2, "K": 9.78, "L": 8.07, "M": 12.93, "N": 10.74, "O": 10.88,
    "P": 9.13, "Q": 10.91, "R": 9.22, "S": 9.19, "T": 9.29, "U": 10.51, "V": 10.27, "W": 14.5,
    "X": 9.96, "Y": 9.9, "Z": 9.12, "[": 5.31, "\\": 4.68, "]": 5.31, "^": 6.82, "_": 6.63,
    "`": 4.87, "a": 8.1, "b": 8.8, "c": 8.23, "d": 8.8, "e": 8.37, "f": 4.93, "g": 8.82,
    "h": 8.57, "i": 3.68, "j": 3.68, "k": 7.98, "l": 3.68, "m": 12.58, "n": 8.57, "o": 8.61,
    "p": 8.8, "q": 8.8, "r": 5.73, "s": 7.69, "t": 4.54, "u": 8.57, "v": 8.2, "w": 11.76,
    "x": 7.95, "y": 8.2, "z": 7.98, "{": 6.32, "|": 4.99, "}": 6.32, "~": 9.49, "·": 4.4,
    "→": 13.51, "≥": 9.49, "—": 14.15, "–": 7.15, "÷": 9.49, "×": 9.49,  # noqa: RUF001 (glyphs Grafana titles use)
}  # fmt: skip


def panels(path: Path) -> list[dict[str, Any]]:
    """
    Every non-row panel of a dashboard, including those inside collapsed rows.

    Args:
        path (Path): Dashboard JSON file.

    Returns:
        list[dict[str, Any]]: The panels.
    """
    out: list[dict[str, Any]] = []
    for p in json.loads(path.read_text())["panels"]:
        out.extend([p] if p["type"] != "row" else p.get("panels", []))
    return out


def title_px(title: str) -> float:
    """
    Rendered width of a panel title.

    Args:
        title (str): The title.

    Returns:
        float: Width in px; unmeasured glyphs count as the widest letter.
    """
    return sum(GLYPH_PX.get(ch, 14.5) for ch in title)


def column_px(panel: dict[str, Any], name: str) -> float:
    """
    Width a table column takes: its width, its minimum, 0 when hidden, else Grafana's default.

    Args:
        panel (dict[str, Any]): Table panel.
        name (str): Column name as displayed before overrides.

    Returns:
        float: Width in px.
    """
    props: dict[str, Any] = dict(panel["fieldConfig"]["defaults"].get("custom", {}))
    props = {f"custom.{k}": v for k, v in props.items()}
    for o in panel["fieldConfig"]["overrides"]:
        m = o["matcher"]
        pattern = m["options"].strip("/") if m["id"] == "byRegexp" else None
        if (m["id"] == "byName" and m["options"] == name) or (
            pattern is not None and re.search(pattern, name)
        ):
            props.update({pr["id"]: pr["value"] for pr in o["properties"]})
    if props.get("custom.hidden"):
        return 0
    return props.get("custom.width") or props.get("custom.minWidth") or DEFAULT_COLUMN_PX


def columns(panel: dict[str, Any]) -> list[str]:
    """
    Column names of a table, from the quoted aliases of its SQL, variables at their defaults.

    Args:
        panel (dict[str, Any]): Table panel.

    Returns:
        list[str]: Distinct names in order.
    """
    sql = " ".join(t.get("rawSql", "") for t in panel.get("targets", []))
    names = re.findall(r'AS "([^"]+)"', sql.replace("$period", "28"))
    return list(dict.fromkeys(names))


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_titles_fit_the_docked_layout(path: Path) -> None:
    """A title never truncates at 1440 px with the side menu docked."""
    problems = []
    for p in panels(path):
        if "$" in p["title"]:
            continue
        room = UNIT_PX * p["gridPos"]["w"] - (106 if p.get("description") else 74)
        if title_px(p["title"]) > room - 1:
            problems.append(
                f"#{p['id']} {p['title']!r}: {title_px(p['title']):.0f} > {room:.0f} px"
            )
    assert not problems, problems


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_one_override_per_column(path: Path) -> None:
    """Grafana applies the last of duplicate overrides, so a second one silently wins."""
    for p in panels(path):
        matchers = [
            json.dumps(o["matcher"], sort_keys=True)
            for o in p.get("fieldConfig", {}).get("overrides", [])
        ]
        assert len(matchers) == len(set(matchers)), (p["id"], p["title"])


@pytest.mark.parametrize("path", DASHBOARDS, ids=lambda p: p.stem)
def test_tables_fit_without_scrolling_sideways(path: Path) -> None:
    """Column widths add up to no more than the panel; headers wrap instead of widening columns."""
    problems = []
    for p in panels(path):
        if p["type"] != "table" or not columns(p):
            continue
        assert p["fieldConfig"]["defaults"].get("custom", {}).get("wrapHeaderText"), p["id"]
        need = sum(column_px(p, c) for c in columns(p))
        room = UNIT_PX * p["gridPos"]["w"] - 26
        if need > room:
            problems.append(f"#{p['id']} {p['title']!r}: columns {need:.0f} > {room:.0f} px")
    assert not problems, problems
