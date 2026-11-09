"""Tool-definition loading, selection, and argument validation."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any, Iterable


def load_definitions(path: Path) -> list[dict[str, Any]]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_cluster_catalog(
    path: Path, definitions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Load a fail-closed, machine-readable tool-cluster manifest."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
        raise ValueError("Unsupported mayor tool-cluster manifest")
    clusters = payload.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        raise ValueError("Mayor tool-cluster manifest must contain clusters")
    known_tools = {item["function"]["name"] for item in definitions}
    seen_clusters: set[str] = set()
    covered_tools: set[str] = set()
    for cluster in clusters:
        if not isinstance(cluster, dict):
            raise ValueError("Mayor tool cluster must be an object")
        name = cluster.get("name")
        description = cluster.get("description")
        tools = cluster.get("tools")
        if not isinstance(name, str) or not name or name in seen_clusters:
            raise ValueError("Mayor tool cluster names must be non-empty and unique")
        if not isinstance(description, str) or not description:
            raise ValueError(f"Mayor tool cluster {name} requires a description")
        if (not isinstance(tools, list) or not tools
                or any(not isinstance(tool, str) or not tool for tool in tools)
                or len(tools) != len(set(tools))):
            raise ValueError(f"Mayor tool cluster {name} has invalid tools")
        unknown = set(tools) - known_tools
        if unknown:
            raise ValueError(
                f"Mayor tool cluster {name} has unknown tools: "
                + ", ".join(sorted(unknown))
            )
        seen_clusters.add(name)
        covered_tools.update(tools)
    missing = known_tools - covered_tools
    if missing:
        raise ValueError(
            "Mayor tool-cluster manifest leaves tools unclustered: "
            + ", ".join(sorted(missing))
        )
    return payload


def select_definitions(
    definitions: list[dict[str, Any]],
    *,
    profile: str,
    names: Iterable[str] | None,
    mayor_profile: frozenset[str],
    cluster: str | None = None,
    tool_clusters: dict[str, frozenset[str]] | None = None,
) -> list[dict[str, Any]]:
    if names is not None and cluster is not None:
        raise ValueError("Choose exact tool names or one cluster, not both")
    if cluster is not None:
        if tool_clusters is None or cluster not in tool_clusters:
            raise ValueError(f"Unknown tool cluster: {cluster}")
        selected = set(tool_clusters[cluster])
    else:
        selected = set(names) if names is not None else mayor_profile if profile == "mayor" else None
    if selected is None:
        return definitions
    unknown = selected - {item["function"]["name"] for item in definitions}
    if unknown:
        raise ValueError("Unknown tools: " + ", ".join(sorted(unknown)))
    return [item for item in definitions if item["function"]["name"] in selected]


def schema_for(definitions: list[dict[str, Any]], name: str) -> dict[str, Any]:
    for item in definitions:
        if item["function"]["name"] == name:
            return item["function"]["parameters"]
    raise ValueError(f"Unknown tool: {name}")


def validate_type(value: Any, expected: str, path: str) -> None:
    checks = {
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
        "string": lambda item: isinstance(item, str),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
    }
    if expected in checks and not checks[expected](value):
        raise ValueError(f"{path} must be {expected}")


def validate_arguments(definitions: list[dict[str, Any]], name: str, arguments: dict[str, Any]) -> None:
    schema = schema_for(definitions, name)
    validate_type(arguments, "object", "arguments")
    validate_schema(arguments, schema, schema, "arguments")


def validate_schema(value: Any, schema: dict, root: dict, path: str) -> None:
    """Enforce the JSON Schema keyword subset used by the tool definitions."""

    reference = schema.get("$ref")
    if reference:
        if not reference.startswith("#/$defs/"):
            raise ValueError(f"Unsupported tool schema reference: {reference}")
        target = root["$defs"][reference.removeprefix("#/$defs/")]
        validate_schema(value, target, root, path)
    if "type" in schema:
        validate_type(value, schema["type"], path)
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} must be one of: {', '.join(map(str, schema['enum']))}")
    if "const" in schema and value != schema["const"]:
        raise ValueError(f"{path} must equal {schema['const']!r}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite:
            raise ValueError(f"{path} must be finite")
        for bound, invalid in (
            ("minimum", value < schema.get("minimum", value)),
            ("maximum", value > schema.get("maximum", value)),
        ):
            if invalid:
                raise ValueError(f"{path} violates {bound} {schema[bound]}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", len(value)):
            raise ValueError(f"{path} has invalid string length")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise ValueError(f"{path} does not match the required pattern")
    if isinstance(value, dict):
        if len(value) < schema.get("minProperties", 0):
            raise ValueError(f"{path} has too few properties")
        if len(value) > schema.get("maxProperties", len(value)):
            raise ValueError(f"{path} has too many properties")
        for required in schema.get("required", []):
            if required not in value:
                raise ValueError(f"Missing required argument: {path}.{required}")
        properties = schema.get("properties", {})
        extra = sorted(set(value) - set(properties))
        if schema.get("additionalProperties") is False and extra:
            raise ValueError(f"Unexpected arguments: {path}: {', '.join(extra)}")
        for key, item in value.items():
            validate_schema(item, properties.get(key, {}), root, f"{path}.{key}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", len(value)):
            raise ValueError(f"{path} has invalid array length")
        if schema.get("uniqueItems"):
            canonical = [json.dumps(item, ensure_ascii=False, sort_keys=True) for item in value]
            if len(canonical) != len(set(canonical)):
                raise ValueError(f"{path} must contain unique items")
        for index, item in enumerate(value):
            validate_schema(item, schema.get("items", {}), root, f"{path}[{index}]")
