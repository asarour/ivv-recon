"""YAML config -> MappingSpec.

Credentials are deliberately absent from this file format. Connections are built
by the caller and injected. A mapping spec is a document you commit and review;
a credential is not.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schema import Column, ColumnMapping, MappingSpec, TableRef


def _table_ref(node: dict[str, Any], what: str) -> TableRef:
    missing = [k for k in ("dialect", "table") if not node.get(k)]
    if missing:
        raise ValueError(f"{what}: missing required field(s) {missing}")
    return TableRef(
        dialect=node["dialect"],
        database=node.get("database"),
        schema=node.get("schema"),
        table=node["table"],
    )


def _column_mapping(node: Any, side_default: str | None = None) -> ColumnMapping:
    """Accept either shorthand or the long form.

    Shorthand (same name and type both sides):
        - {name: CUSTOMER_ID, type: INT}

    Long form (renamed or retyped):
        - source: {name: cust_id, type: INT}
          target: {name: CUSTOMER_ID, type: NUMBER(38,0)}
          note: renamed in flight
    """
    if not isinstance(node, dict):
        raise ValueError(f"column mapping must be a mapping, got {type(node).__name__}")

    if "name" in node:
        col_s = Column.from_native(node["name"], node.get("type", "varchar"),
                                   nullable=node.get("nullable", True))
        col_t = Column.from_native(node.get("target_name", node["name"]),
                                   node.get("target_type", node.get("type", "varchar")),
                                   nullable=node.get("target_nullable", node.get("nullable", True)))
        return ColumnMapping(source=col_s, target=col_t, transform_note=node.get("note"))

    if "source" not in node or "target" not in node:
        raise ValueError(
            "column mapping needs either 'name' (shorthand) or both 'source' and 'target'"
        )
    s, t = node["source"], node["target"]
    return ColumnMapping(
        source=Column.from_native(s["name"], s.get("type", "varchar"), s.get("nullable", True)),
        target=Column.from_native(t["name"], t.get("type", "varchar"), t.get("nullable", True)),
        transform_note=node.get("note"),
    )


def load_spec(path: str | Path) -> MappingSpec:
    """Parse a mapping YAML into a validated MappingSpec."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"mapping file not found: {p}")

    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    for required in ("source", "target", "keys", "compare"):
        if required not in raw:
            raise ValueError(f"mapping file missing required section: '{required}'")

    spec = MappingSpec(
        source_table=_table_ref(raw["source"], "source"),
        target_table=_table_ref(raw["target"], "target"),
        key_columns=[_column_mapping(n) for n in raw["keys"]],
        compare_columns=[_column_mapping(n) for n in raw["compare"]],
        excluded=list(raw.get("exclude", [])),
    )
    spec.validate()
    return spec


def load_options(path: str | Path) -> dict[str, Any]:
    """Non-mapping run options from the same file (filters, thresholds)."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    opts = raw.get("options", {}) or {}
    return {
        "source_where": opts.get("source_where"),
        "target_where": opts.get("target_where"),
        "fail_threshold": opts.get("fail_threshold", "high"),
        "aggregate_tolerance": float(opts.get("aggregate_tolerance", 0.0)),
    }
