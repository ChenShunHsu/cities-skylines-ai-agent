#!/usr/bin/env python3
"""Delegate simple read-only work to the local model and cross-check every answer.

The main agent plans; the local model (default ``qwen3.5-4b`` in LM Studio) does
the cheap, high-frequency work. Because a small model can read numbers or fill
arguments differently, every delegated answer is compared with a deterministic
reference computed by the controller from the same evidence:

- ``extract``: run one read-only typed tool (or reuse a supplied result), trim it
  to the focus paths, ask the model the listed questions, and compare each
  answer with the value the controller resolves through an explicit path.
- ``toolcall``: give the model an instruction plus the exact schemas of a few
  tools; compare its ``{toolName, arguments}`` with the expected call.
  Differences are split into ``semantic`` (a disagreement), ``benignDefaults``
  (an added key equal to the schema default), ``inventedControllerValues``
  (keys such as ``checkpointName`` that the controller always overwrites) and
  ``freeText``. Only a matching read-only call is executed, and only when asked.
- ``select``: the model picks one ``candidateId`` from a bounded list; compared
  with the expected choice. Nothing is executed.

Every outcome is appended to a JSONL journal so agreement can be tracked.
The model never receives mutation authority: execution is limited to tools the
repository contract classifies as ``read_only``.

Usage:
  python scripts/qwen_crosscheck.py tasks.json [--model qwen3.5-4b]
  python scripts/qwen_crosscheck.py --stats
"""

from __future__ import annotations

import argparse
import json
import os
import math
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))

import mayor_tools  # noqa: E402
from mayor import contracts  # noqa: E402


# Any OpenAI-compatible local server works; LM Studio's default port is 1234.
LMSTUDIO_URL = os.environ.get("LOCAL_MODEL_URL", "http://127.0.0.1:1234/v1/chat/completions")
DEFAULT_MODEL = os.environ.get("LOCAL_MODEL", "qwen3.5-4b")
DEFAULT_JOURNAL = SCRIPTS.parent / ".runtime" / "qwen-crosscheck" / "journal.jsonl"
# Prompts stay under an 8192-token context (qwen3.5-4b as loaded in LM Studio by default).
MAX_PROMPT_CHARS = int(os.environ.get("LOCAL_MODEL_MAX_PROMPT_CHARS", "14000"))
MISSING = "<absent>"
# reasoning_effort per task kind when LocalModel uses "auto"; None = thinking mode.
KIND_REASONING = {"extract": "none", "toolcall": "none", "select": None}
NO_DEFAULT = "<none>"

JSON_TYPES = {
    "number": {"type": "number"},
    "integer": {"type": "integer"},
    "string": {"type": "string"},
    "boolean": {"type": "boolean"},
    "numbers": {"type": "array", "items": {"type": "number"}},
    "strings": {"type": "array", "items": {"type": "string"}},
}


def is_read_only(tool: str) -> bool:
    return contracts.repository_tool_action_kinds().get(tool) == "read_only"


_SEGMENT = re.compile(r"([^.\[\]]+)|\[([^\]]*)\]")


def resolve(data: Any, path: str) -> Any:
    """Resolve a small deterministic path language.

    ``a.b`` nested keys, ``a[0]`` index, ``a[:n]`` first n items, ``a[*].b`` map over a list,
    ``a[k=v].b`` first item whose ``k`` equals ``v``, and the wrappers
    ``len(...)``, ``sum(...)`` and ``keys(...)``.
    """
    for name in ("len", "sum", "keys"):
        if path.startswith(name + "(") and path.endswith(")"):
            inner = resolve(data, path[len(name) + 1:-1])
            if name == "len":
                return len(inner)
            if name == "sum":
                return round(sum(inner), 6)
            return sorted(inner.keys())
    current = [data]
    mapped = False
    for key, bracket in _SEGMENT.findall(path):
        following = []
        for item in current:
            if key:
                following.append(item.get(key) if isinstance(item, dict) else None)
            elif bracket == "*":
                following.extend(item or [])
                mapped = True
            elif bracket.startswith(":"):
                following.append((item or [])[:int(bracket[1:])])
            elif "=" in bracket:
                field, value = bracket.split("=", 1)
                following.append(next((x for x in item or [] if str(x.get(field)) == value), None))
            else:
                following.append(item[int(bracket)] if item is not None else None)
        current = following
    return current if mapped else current[0]


def trim(data: Any, focus: list[str] | None, list_limit: int = 12) -> Any:
    """Keep only the focus subtrees and cap lists so the prompt fits the model context."""
    if focus:
        data = {path: resolve(data, path) for path in focus}

    def cap(value: Any) -> Any:
        if isinstance(value, dict):
            return {k: cap(v) for k, v in value.items()}
        if isinstance(value, list):
            return [cap(v) for v in value[:list_limit]]
        return value

    return cap(data)


# a key read with its count, as the model sometimes lists a dict's keys: '"Garbage": 42'
KEY_WITH_COUNT = re.compile(r'^"?([A-Za-z_]\w*)"?\s*:\s*-?[\d.,]+$')


def normalize(value: Any) -> Any:
    if isinstance(value, str):
        text = value.strip()
        try:
            return float(text.replace(",", "").replace("₡", ""))
        except ValueError:
            keyed = KEY_WITH_COUNT.match(text)
            return (keyed.group(1) if keyed else text).lower()
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, list):
        return sorted((normalize(v) for v in value), key=lambda v: json.dumps(v, sort_keys=True))
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    return value


def equal(left: Any, right: Any, tolerance: float = 1e-6) -> bool:
    left, right = normalize(left), normalize(right)
    if isinstance(left, float) and isinstance(right, float):
        return math.isclose(left, right, rel_tol=tolerance, abs_tol=tolerance)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(equal(a, b, tolerance) for a, b in zip(left, right))
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(equal(left[k], right[k], tolerance) for k in left)
    return left == right


def load_tool_schemas(names: list[str]) -> list[dict[str, Any]]:
    path = SCRIPTS.parent / "references" / "mayor-tools.json"
    definitions = json.loads(path.read_text(encoding="utf-8"))
    tools = definitions if isinstance(definitions, list) else definitions.get("tools", definitions)
    selected = []
    for tool in tools:
        function = tool.get("function", tool)
        if function["name"] in names:
            selected.append({
                "name": function["name"],
                "description": function.get("description", "")[:300],
                "parameters": function.get("parameters", {}),
            })
    return selected


def compare_arguments(
    expected: dict[str, Any],
    arguments: dict[str, Any],
    properties: dict[str, Any],
    controller_owned: set[str] = frozenset(),
    free_text: set[str] = frozenset(),
    tolerance: float = 1e-6,
) -> dict[str, list]:
    """Split argument differences; an empty ``semantic`` list means agreement."""
    result: dict[str, list] = {"semantic": [], "benignDefaults": [], "inventedControllerValues": [], "freeText": []}
    for key in sorted(set(expected) | set(arguments)):
        mine, theirs = expected.get(key, MISSING), arguments.get(key, MISSING)
        default = properties.get(key, {}).get("default", NO_DEFAULT)
        if key in controller_owned:
            if theirs != MISSING and theirs != mine:
                result["inventedControllerValues"].append({key: theirs})
        elif key in free_text:
            result["freeText"].append({key: theirs})
        elif mine == MISSING and default != NO_DEFAULT and equal(theirs, default, tolerance):
            result["benignDefaults"].append(key)
        elif theirs == MISSING and default != NO_DEFAULT and equal(mine, default, tolerance):
            result["benignDefaults"].append(key)
        elif mine == MISSING or theirs == MISSING or not equal(mine, theirs, tolerance):
            result["semantic"].append({"key": key, "expected": mine, "model": theirs})
    return {k: v for k, v in result.items() if v}


class LocalModel:
    """LM Studio client. ``reasoning_effort`` None keeps the model's thinking mode.

    Measured on qwen3.5-4b: "none" copied values as well as thinking mode
    (extract 14/14) about 20x faster, but in a live school-site choice it
    called 352 the maximum of a list containing 361; thinking mode chose
    correctly. Copy-style work therefore skips thinking and comparisons keep it.
    """

    def __init__(self, model: str = DEFAULT_MODEL, url: str = LMSTUDIO_URL,
                 reasoning_effort: str | None = "auto") -> None:
        self.model, self.url, self.reasoning_effort = model, url, reasoning_effort

    def ask(self, system: str, user: str, schema: dict[str, Any],
            kind: str = "extract") -> tuple[dict[str, Any], dict[str, Any]]:
        body = {
            "model": self.model,
            "temperature": 0,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": "answer", "strict": True, "schema": schema}},
        }
        effort = KIND_REASONING.get(kind) if self.reasoning_effort == "auto" else self.reasoning_effort
        if effort:
            body["reasoning_effort"] = effort
        started = time.monotonic()
        request = urllib.request.Request(self.url, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
        reply = json.load(urllib.request.urlopen(request, timeout=600))
        usage = reply.get("usage", {})
        meta = {"seconds": round(time.monotonic() - started, 1),
                "promptTokens": usage.get("prompt_tokens"),
                "completionTokens": usage.get("completion_tokens")}
        return json.loads(reply["choices"][0]["message"]["content"]), meta


def run_extract(task: dict[str, Any], model: LocalModel, call_tool=mayor_tools.call_tool) -> dict[str, Any]:
    tool = task["tool"]
    if not is_read_only(tool):
        raise ValueError(f"extract refuses non-read-only tool {tool}")
    result = task.get("result") or call_tool(tool, dict(task.get("arguments", {})))
    view = trim(result, task.get("focus"), task.get("listLimit", 12))
    text = json.dumps(view, ensure_ascii=False, separators=(",", ":"))
    if len(text) > MAX_PROMPT_CHARS:
        raise ValueError(f"trimmed result is {len(text)} chars; add focus paths")
    questions = task["questions"]
    schema = {"type": "object", "additionalProperties": False,
              "required": [q["key"] for q in questions],
              "properties": {q["key"]: JSON_TYPES[q.get("type", "number")] for q in questions}}
    system = ("You extract values from a Cities: Skylines tool result. Answer each key using only the "
              "JSON given. Copy numbers exactly; do not round or convert units. Return only JSON.")
    user = json.dumps({"tool": tool, "result": view,
                       "questions": {q["key"]: q["ask"] for q in questions}}, ensure_ascii=False)
    answers, meta = model.ask(system, user, schema)
    checks = []
    for question in questions:
        expected = resolve(result, question["path"])
        answer = answers.get(question["key"])
        checks.append({"key": question["key"], "expected": expected, "model": answer,
                       "match": equal(expected, answer, question.get("tolerance", 1e-6))})
    return {"kind": "extract", "tool": tool, "checks": checks,
            "matched": sum(c["match"] for c in checks), "total": len(checks), **meta}


def run_toolcall(task: dict[str, Any], model: LocalModel, call_tool=mayor_tools.call_tool) -> dict[str, Any]:
    names = task["allowedTools"]
    schemas = load_tool_schemas(names)
    schema = {"type": "object", "additionalProperties": False, "required": ["toolName", "argumentsJson"],
              "properties": {"toolName": {"type": "string", "enum": names},
                             "argumentsJson": {"type": "string"}}}
    system = ("You turn one instruction into exactly one typed tool call for a Cities: Skylines mayor. "
              "Use only listed tools and only parameters from their schema. Put the arguments object, "
              "encoded as a JSON string, in argumentsJson. Return only JSON.")
    user = json.dumps({"instruction": task["instruction"], "context": task.get("context", {}),
                       "tools": schemas}, ensure_ascii=False)
    answer, meta = model.ask(system, user, schema, kind="toolcall")
    try:
        arguments = json.loads(answer["argumentsJson"] or "{}")
    except json.JSONDecodeError:
        arguments = {"_unparsable": answer["argumentsJson"]}
    expected = task["expected"]
    properties = next((s["parameters"].get("properties", {}) for s in schemas
                       if s["name"] == answer["toolName"]), {})
    owned = set(task.get("controllerOwned", []))
    differences = compare_arguments(expected.get("arguments", {}), arguments, properties, owned,
                                    set(task.get("freeText", [])), task.get("tolerance", 1e-6))
    match = answer["toolName"] == expected["toolName"] and not differences.get("semantic")
    executed = None
    if match and task.get("execute") and is_read_only(answer["toolName"]):
        final = {**arguments, **{k: v for k, v in expected.get("arguments", {}).items() if k in owned}}
        executed = call_tool(answer["toolName"], final)
    return {"kind": "toolcall", "expected": expected,
            "model": {"toolName": answer["toolName"], "arguments": arguments},
            "match": match, "differences": differences, "matched": int(match), "total": 1,
            "executed": executed is not None,
            "executedOk": executed.get("ok") if isinstance(executed, dict) else None, **meta}


def run_select(task: dict[str, Any], model: LocalModel, call_tool=None) -> dict[str, Any]:
    ids = [c["candidateId"] for c in task["candidates"]]
    schema = {"type": "object", "additionalProperties": False, "required": ["candidateId", "reason"],
              "properties": {"candidateId": {"type": "string", "enum": ids}, "reason": {"type": "string"}}}
    system = ("You are a bounded Cities: Skylines decision selector. Choose exactly one candidateId "
              "from the list using the instruction, rules and state evidence. Never invent candidates. "
              "Return only JSON with candidateId and a one-sentence reason.")
    user = json.dumps({"instruction": task["instruction"], "rules": task.get("rules", []),
                       "state": task.get("state", {}), "candidates": task["candidates"]}, ensure_ascii=False)
    answer, meta = model.ask(system, user, schema, kind="select")
    match = answer["candidateId"] == task["expectedCandidateId"]
    return {"kind": "select", "expected": task["expectedCandidateId"], "model": answer["candidateId"],
            "reason": answer["reason"], "match": match, "matched": int(match), "total": 1, **meta}


RUNNERS = {"extract": run_extract, "toolcall": run_toolcall, "select": run_select}


def append_journal(journal: Path, model_name: str, outcome: dict[str, Any]) -> None:
    journal.parent.mkdir(parents=True, exist_ok=True)
    record = {"at": datetime.now(timezone.utc).isoformat(), "model": model_name, **outcome}
    with journal.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def journal_stats(journal: Path) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    if not journal.exists():
        return summary
    for line in journal.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        bucket = summary.setdefault(row["kind"], {"tasks": 0, "matched": 0, "total": 0})
        bucket["tasks"] += 1
        bucket["matched"] += row["matched"]
        bucket["total"] += row["total"]
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("tasks", nargs="?", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--reasoning-effort", default="auto",
                        help="'auto' (per task kind), an LM Studio reasoning_effort such as 'none', "
                             "or '' to keep thinking mode for every task")
    parser.add_argument("--journal", type=Path, default=DEFAULT_JOURNAL)
    parser.add_argument("--stats", action="store_true")
    parser.add_argument("--full", action="store_true", help="also print matched extract checks")
    options = parser.parse_args()
    if options.stats:
        print(json.dumps(journal_stats(options.journal), ensure_ascii=False))
        return
    if options.tasks is None:
        parser.error("tasks file required unless --stats")
    model = LocalModel(options.model, reasoning_effort=options.reasoning_effort or None)
    for task in json.loads(options.tasks.read_text(encoding="utf-8"))["tasks"]:
        outcome = {"id": task["id"], **RUNNERS[task["kind"]](task, model)}
        append_journal(options.journal, options.model, outcome)
        if outcome["kind"] == "extract" and not options.full:
            outcome["checks"] = [c for c in outcome["checks"] if not c["match"]] or "all matched"
        print(json.dumps(outcome, ensure_ascii=False, default=str), flush=True)


if __name__ == "__main__":
    main()
