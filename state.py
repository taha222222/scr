"""Resume support: one JSON file per researched company, written atomically."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path


class StateStore:
    def __init__(self, state_dir: Path):
        self.dir = state_dir / "companies"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = "".join(c if c.isalnum() or c in ".-" else "_" for c in key)[:80]
        return self.dir / f"{safe}-{hashlib.sha1(key.encode()).hexdigest()[:8]}.json"

    def load(self, key: str) -> dict | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def save(self, key: str, result: dict) -> None:
        p = self._path(key)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, p)

    def is_fresh(self, key: str, max_age_days: int) -> bool:
        r = self.load(key)
        if not r or not r.get("checked_at"):
            return False
        try:
            when = dt.datetime.strptime(r["checked_at"], "%Y-%m-%d %H:%M")
        except ValueError:
            return False
        return (dt.datetime.now() - when).days < max_age_days

    def clear(self) -> None:
        for p in self.dir.glob("*.json"):
            p.unlink()
