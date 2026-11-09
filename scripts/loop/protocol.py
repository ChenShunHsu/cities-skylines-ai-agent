"""Every change under the mutation protocol: checkpoint, regression baseline, change, settle, verify.

The checkpoint (a native save) is taken once per batch, not once per change: the first change of a
batch saves the city and the changes after it reuse that save, each still with its own regression
baseline and verification. A batch is one phase of a wake (cycle.main starts a new one before
utilities, growth, clearing, services, budgets and traffic), and never older than
CHECKPOINT_MAX_AGE_SECONDS. So the save to go back to is the city before the batch's first change,
not before the change that went wrong: nothing reads a checkpoint back while the game runs (CS1
cannot load one in-process; recovery is a restart and a manual load), so a later change only means
redoing the batch's earlier changes too. A save per change wrote 8-15 saves of 6.6 MB a wake, one
every 15-20 s while zoning, each holding the game thread.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from loop.city import mt

CHECKPOINT_RETRY_SECONDS = 10.0
CHECKPOINT_MAX_AGE_SECONDS = 600.0  # a batch's save is taken again after this long

_batch: dict[str, Any] = {"name": None, "at": 0.0}
SAVE_POLL_SECONDS = 0.5


def _saving() -> bool | None:
    """Whether a save is being written; None when the Bridge could not say. /state/save-status is
    answered off the game thread; an older Bridge only has the full listing, which runs on it."""
    try:
        status = mt._save_status()
        if status is None:
            status = mt._request("GET", "/state/saves")
    except (mt.BridgeRequestError, RuntimeError):
        return None  # the listing can time out on the game thread while a save is being written
    saving = status.get("isSaving")
    return saving if isinstance(saving, bool) else None


def wait_save(timeout_seconds: float = 180.0) -> None:
    """A checkpoint save holds the game thread; reads made meanwhile time out."""
    deadline = time.monotonic() + timeout_seconds
    while True:
        if _saving() is False:
            return
        if time.monotonic() >= deadline:
            raise RuntimeError("save did not finish")
        time.sleep(SAVE_POLL_SECONDS)


KEEP_CHECKPOINTS = 20  # as scripts/prune-agent-saves.ps1 keeps at a restart
PRUNE_PER_WAKE = 5
PRUNE_GAP_SECONDS = 0.5
CHECKPOINT_PATTERN = "AgentCheckpoint-*.crp"


def _modified(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None  # gone (or unreadable) since the folder was listed


def _protected(folder: Path) -> set[str]:
    """The save the launcher's Continue loads (continue_game.json beside the save folder), as the
    restart prune protects it."""
    try:
        title = json.loads((folder.parent / "continue_game.json").read_text(encoding="utf-8")).get("title")
    except (OSError, ValueError, AttributeError):
        return set()
    return {str(title).lower()} if title else set()


def prune_checkpoints(keep: int = KEEP_CHECKPOINTS, limit: int = PRUNE_PER_WAKE) -> dict[str, Any]:
    """Delete the oldest agent checkpoints beyond the newest `keep`, at most `limit` a wake, one at
    a time. Only AgentCheckpoint-* saves are touched (never a save the player named, nor the one
    Continue loads), and nothing while a save is being written.

    Until now old checkpoints were removed only at a restart, with the game closed, because a run
    that moved hundreds out from under the running game left it throwing for each missing save on
    every later save until its thread stopped answering. The game's own Load panel deletes a save
    with a plain File.Delete while running; its package index drops a deleted save on the file
    watcher's event, and that watcher has an 8 KB buffer (ColossalFramework PackageManager), which
    hundreds of changes at once overflow, losing the events. A few deletions a wake, spaced apart,
    stay well inside it, and the folder no longer grows by a save every mutation between restarts
    (live: 962 checkpoints, 6.9 GB)."""
    try:
        status = mt._save_status()
    except (mt.BridgeRequestError, RuntimeError):
        status = None  # pruning never stops a wake
    if status is None:
        return {"pruned": 0, "skipped": "noSaveStatus"}
    if status.get("isSaving") is not False:
        return {"pruned": 0, "skipped": "saving"}
    folder = Path(str(status.get("directory") or ""))
    if not status.get("directory") or not folder.is_dir():
        return {"pruned": 0, "skipped": "noSaveFolder"}
    dated = [(stamp, path) for path in folder.glob(CHECKPOINT_PATTERN)
             if (stamp := _modified(path)) is not None]
    dated.sort(key=lambda item: item[0], reverse=True)
    protected = _protected(folder)
    old = [path for _, path in reversed(dated[keep:]) if path.stem.lower() not in protected][:limit]
    pruned, failed = [], []
    for index, path in enumerate(old):
        if index:
            time.sleep(PRUNE_GAP_SECONDS)
        try:
            path.unlink()
            pruned.append(path.stem)
        except FileNotFoundError:
            pass
        except OSError as error:  # in use by the game: left for a later wake or the restart prune
            failed.append({"name": path.stem, "error": str(error)[:120]})
    return {"pruned": len(pruned), "remaining": len(dated) - len(pruned), "failed": failed}


def settle(seconds: float) -> None:
    if seconds <= 0:
        return
    mt.call_tool("set_simulation_speed", {"paused": False, "speed": 1})
    time.sleep(seconds)
    mt.call_tool("set_simulation_speed", {"paused": True})


def new_batch() -> None:
    """The next change saves the city again (see the module docstring)."""
    _batch.update(name=None, at=0.0)


def checkpoint(label: str) -> str:
    """The batch's save (taken now if the batch has none, or it is too old), and a regression
    baseline for this change. A save the Bridge could not verify is not reused: the next change
    takes its own."""
    name = _batch["name"]
    if name is None or time.monotonic() - _batch["at"] > CHECKPOINT_MAX_AGE_SECONDS:
        created = mt.call_tool("create_checkpoint", {"name": f"pre-{label}"})
        name = created["checkpointName"]
        wait_save()
        _batch.update(name=name if created.get("ok") else None, at=time.monotonic())
    mt.call_tool("capture_regression_baseline", {"name": f"{label}-baseline"})
    return name


def verify(label: str) -> dict[str, Any]:
    return mt.call_tool("verify_regression", {"name": f"{label}-baseline"})


def run(label: str, tool: str, arguments: dict[str, Any], settle_seconds: float = 0.0) -> dict[str, Any]:
    """One typed mutation; the result keeps only scalar fields so logs stay small. A refused or
    failed call is returned as a failed action (ok False with the error), never raised: one
    refusal must not crash the wake and repeat everything already done in it. A checkpoint the
    game thread is too busy to take (a save still being written) is tried once more."""
    name = None
    for attempt in range(2):
        try:
            name = checkpoint(label)
            break
        except (mt.BridgeRequestError, RuntimeError) as error:
            if attempt:
                return {"label": label, "tool": tool, "ok": False, "error": f"checkpoint: {str(error)[:280]}",
                        "result": {"error": str(error)[:300]}, "regressionOk": None,
                        "regressionFindings": None, "delta": {}}
            time.sleep(CHECKPOINT_RETRY_SECONDS)
    try:
        result = mt.call_tool(tool, {**arguments, "checkpointName": name})
    except (mt.BridgeRequestError, RuntimeError, ValueError) as error:
        regression = verify(label)
        return {"label": label, "tool": tool, "ok": False, "error": str(error)[:300],
                "result": {"error": str(error)[:300]}, "regressionOk": regression.get("ok"),
                "regressionFindings": regression.get("findings") or regression.get("regressions"), "delta": {}}
    settle(settle_seconds)
    regression = verify(label)
    delta = regression.get("delta", {})
    scalar = ({k: v for k, v in result.items() if not isinstance(v, (dict, list))}
              if isinstance(result, dict) else result)
    return {"label": label, "tool": tool, "ok": result.get("ok") if isinstance(result, dict) else None,
            "result": scalar, "regressionOk": regression.get("ok"),
            "regressionFindings": regression.get("findings") or regression.get("regressions"),
            "delta": {k: delta.get(k) for k in ("cash", "population", "problemEntities", "roadSegments",
                                                "progressionChanged")},
            "zoningSupport": result.get("zoningSupport") if isinstance(result, dict) else None}
