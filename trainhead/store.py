"""Reading the generated catalog. Kept free of heavy imports so the weekly
jobs and the Claude runs can load it with the standard library only."""
from __future__ import annotations

import gzip
import json
from pathlib import Path


def load_catalog(path: str | Path) -> list[dict]:
    path = Path(path)
    if path.is_dir():
        path = path / "routes.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def find_route(routes: list[dict], route_id: str) -> dict | None:
    for r in routes:
        if r["id"] == route_id:
            return r
    return None
