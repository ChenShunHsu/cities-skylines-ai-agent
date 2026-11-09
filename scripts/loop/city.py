"""The city a loop is playing, and where its private state lives.

What the loop decides or learns about one city (planned land use, notes, which remedies worked,
each module's state) is kept under .runtime/cities/<instanceId>/ in the repository, which git
ignores: a city's layout belongs to its player and never reaches the published repository. The
game's city instance id keys the folder, so the same loop resumes the right state on any map.

profile.json holds what a planner may edit:
  identity  {instanceId, name, map}
  plan      [{minX, maxX, minZ, maxZ, kind, reason}]  planned land use; overrides neighbourhoods
            (an entry may also name a "layout" for new land inside it, and "crossTo": true asks
            for that land next, over the water if need be)
  notes     [str]                                      free-form planning notes
  settings  {reserve, ...}                             loop settings for this city
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

SCRIPTS = Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import mayor_tools as mt  # noqa: E402

ROOT = REPO / ".runtime" / "cities"
DEFAULT_SETTINGS = {"reserve": 4000.0}


class City:
    def __init__(self, identity: dict[str, Any], root: Path = ROOT):
        self.identity = identity
        self.dir = root / (identity.get("instanceId") or "unknown-city")
        self.dir.mkdir(parents=True, exist_ok=True)
        self.profile_path = self.dir / "profile.json"
        self.profile = self._load_profile()

    def _load_profile(self) -> dict[str, Any]:
        if self.profile_path.exists():
            profile = json.loads(self.profile_path.read_text(encoding="utf-8"))
        else:
            profile = {}
        profile["identity"] = self.identity
        profile.setdefault("plan", [])
        profile.setdefault("notes", [])
        profile["settings"] = {**DEFAULT_SETTINGS, **profile.get("settings", {})}
        return profile

    def save_profile(self) -> None:
        self.profile_path.write_text(json.dumps(self.profile, indent=1, ensure_ascii=False), encoding="utf-8")

    @property
    def plan(self) -> list[dict[str, Any]]:
        return self.profile["plan"]

    def state_path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def load_state(self, name: str, default: Any) -> Any:
        path = self.state_path(name)
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def save_state(self, name: str, value: Any) -> None:
        self.state_path(name).write_text(json.dumps(value, indent=1, ensure_ascii=False, default=str), encoding="utf-8")

    def log(self, name: str, record: dict[str, Any]) -> None:
        with (self.dir / f"{name}.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def current(root: Path = ROOT) -> City:
    """The city the game has loaded. A wake is one process and no city loads during it, so its
    identity is kept for the process (mayor_tools.remember_city): regression baselines and mayor
    memory then key their records with it instead of each reading the snapshot again."""
    snapshot = mt._request("GET", "/state/mayor-snapshot", query={"detailLimit": 0})
    mt.remember_city(snapshot)
    city = snapshot.get("city") or {}
    return City({"instanceId": city.get("instanceId"), "name": city.get("name"), "map": city.get("map")}, root)
