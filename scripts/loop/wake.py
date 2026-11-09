"""Advance the simulation and read the city, with an optional local-model cross-check.

The controller resolves every summary field through an explicit path. The cross-check is off by
default (the model never decides anything here, and it holds video memory the game needs); asked
for (python -m loop.cycle ... --model, or play.py --model), the local model reads the same trimmed
results and each answer is compared; disagreements are reported (the deterministic value is always
used), so the loop measures whether the small model can take over the reading.
"""

from __future__ import annotations

import time
from typing import Any

from loop.city import City, mt

OBSERVE = [
    ("gameTime", "snapshot game time", "snapshot.gameTime", "string"),
    ("population", "city population", "cityHealth.population", "integer"),
    ("cash", "cash amountDisplay", "cityHealth.cash.amountDisplay", "number"),
    ("net", "periodNetDisplay", "cityHealth.cash.periodNetDisplay", "number"),
    ("unemployment", "unemployment percent", "cityHealth.employment.unemployment", "integer"),
    ("happiness", "wellbeing happiness", "cityHealth.wellbeing.happiness", "integer"),
    ("elecUtil", "electricity utilization", "cityHealth.utilities.electricity.utilization", "number"),
    ("waterUtil", "water utilization", "cityHealth.utilities.water.utilization", "number"),
    ("sewageUtil", "sewage utilization", "cityHealth.utilities.sewage.utilization", "number"),
    ("problems", "every key of problems.countsByProblem, all of them in order", "keys(problems.countsByProblem)",
     "strings"),
    ("milestone", "currentMilestone", "progression.currentMilestone", "string"),
]


def _field(path: str) -> str:
    """The field a reading comes from: keys(problems.countsByProblem) reads problems.countsByProblem."""
    return path[path.index("(") + 1:-1] if path.endswith(")") and "(" in path else path


def _model():
    try:
        import qwen_crosscheck
        return qwen_crosscheck, qwen_crosscheck.LocalModel()
    except Exception:  # no local model configured: the deterministic reading stands alone
        return None, None


SPEED_TRIES = 3
SPEED_RETRY_SECONDS = 5.0


def set_speed(arguments: dict[str, Any]) -> dict[str, Any]:
    """A speed change, sent again when the game thread was too busy to run it: such a timeout ran
    nothing, so sending it again is safe (live: a wake of a 27,000-people city ended at its first
    unpause, the command not run, and the whole wake was retried)."""
    for attempt in range(SPEED_TRIES):
        try:
            return mt.call_tool("set_simulation_speed", arguments)
        except (mt.BridgeRequestError, RuntimeError) as error:
            if "command was not run" not in str(error) or attempt == SPEED_TRIES - 1:
                raise
            time.sleep(SPEED_RETRY_SECONDS)
    raise RuntimeError("unreachable")


def wake(city: City, seconds: float, use_model: bool = False) -> dict[str, Any]:
    if seconds > 0:
        set_speed({"paused": False, "speed": 3})
        time.sleep(seconds)
    set_speed({"paused": True})
    snapshot = mt.call_tool("observe_city", {"detailLimit": 5})
    crosscheck, model = _model() if use_model else (None, None)
    values: dict[str, Any] = {}
    misses: list[dict[str, Any]] = []
    if crosscheck and model:
        task = {"tool": "observe_city", "result": snapshot,
                "focus": ["snapshot", "cityHealth", "problems.countsByProblem", "progression.currentMilestone"],
                "questions": [{"key": k, "ask": a, "path": p, "type": t} for k, a, p, t in OBSERVE]}
        try:
            outcome = crosscheck.run_extract(task, model)
            city.log("model-journal", {"id": "wake-observe", **outcome})
            values = {check["key"]: check["expected"] for check in outcome["checks"]}
            misses = [{"key": c["key"], "expected": c["expected"], "model": c["model"]}
                      for c in outcome["checks"] if not c["match"]]
            if misses:
                # the 4B model slips about one wake in thirty (an empty problem list). It answers a
                # prompt the same way every time (temperature 0), so the missed readings are asked
                # again alone, over their own fields only: one it misses twice is a disagreement
                missed = {miss["key"] for miss in misses}
                questions = [q for q in task["questions"] if q["key"] in missed]
                again = crosscheck.run_extract({**task, "questions": questions,
                                                "focus": sorted({_field(q["path"]) for q in questions})}, model)
                city.log("model-journal", {"id": "wake-observe-again", **again})
                right = {c["key"] for c in again["checks"] if c["match"]}
                misses = [{**miss, "again": next(c["model"] for c in again["checks"] if c["key"] == miss["key"])}
                          for miss in misses if miss["key"] not in right]
        except Exception as error:  # the model being unavailable skips the cross-check, never stops the loop
            city.log("model-journal", {"id": "wake-observe", "modelError": str(error)[:300]})
            values, model = {}, None
    if not values:
        import qwen_crosscheck as reader
        values = {key: reader.resolve(snapshot, path) for key, _, path, _ in OBSERVE}
    capabilities = (snapshot.get("progression") or {}).get("capabilities") or {}
    # counts by problem (NoResources...): the cargo step reads the trend (loop.cargo)
    counts = (snapshot.get("problems") or {}).get("countsByProblem") or {}
    return {**values, "unlocked": sorted(k for k, v in capabilities.items() if v), "problemCounts": counts,
            "model": "on" if use_model else "off",
            "modelChecked": bool(crosscheck and model), "modelMismatches": misses}
