"""Dynamic loader for workflow JSON schemas and the disposition catalogue."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class FieldSpec:
    """Definition of a single lead field within a workflow schema."""

    name: str
    field_type: str
    required: bool
    pii: bool
    reference: bool
    aliases: tuple[str, ...]
    values: tuple[str, ...]
    default: str | None


@dataclass(frozen=True)
class WorkflowSchema:
    """Loaded workflow schema definition."""

    workflow_id: str
    description: str
    fields: tuple[FieldSpec, ...]
    reference_field: str
    raw_json: str


@dataclass(frozen=True)
class DispositionSpec:
    """Single disposition definition from dispositions.csv."""

    workflow_id: str
    code: str
    label: str
    is_terminal: bool
    is_success: bool


@dataclass(frozen=True)
class SchemaRegistry:
    """In-memory registry of all loaded workflow schemas and dispositions."""

    workflows: dict[str, WorkflowSchema]
    dispositions: dict[str, dict[str, DispositionSpec]]


def _parse_bool(value: str) -> bool:
    return value.strip().lower() == "true"


def load_workflow_schema(path: Path) -> WorkflowSchema:
    """Parse one workflow JSON schema file."""
    raw_text = path.read_text(encoding="utf-8")
    data: dict[str, Any] = json.loads(raw_text)
    workflow_id = str(data["workflow_id"])
    description = str(data.get("description", ""))

    fields: list[FieldSpec] = []
    reference_field: str | None = None
    for item in data["fields"]:
        is_ref = bool(item.get("reference", False))
        name = str(item["name"])
        if is_ref:
            if reference_field is not None:
                raise ValueError(f"Workflow {workflow_id} defines multiple reference fields.")
            reference_field = name
        enum_values = tuple(str(v).lower() for v in item.get("values", ()))
        default_val = item.get("default")
        fields.append(
            FieldSpec(
                name=name,
                field_type=str(item["type"]),
                required=bool(item.get("required", False)),
                pii=bool(item.get("pii", False)),
                reference=is_ref,
                aliases=tuple(str(a) for a in item.get("aliases", ())),
                values=enum_values,
                default=str(default_val) if default_val is not None else None,
            )
        )

    if reference_field is None:
        raise ValueError(f"Workflow {workflow_id} does not define a reference=true field.")

    return WorkflowSchema(
        workflow_id=workflow_id,
        description=description,
        fields=tuple(fields),
        reference_field=reference_field,
        raw_json=raw_text,
    )


def load_dispositions(path: Path) -> dict[str, dict[str, DispositionSpec]]:
    """Parse dispositions.csv keyed by workflow_id -> disposition code."""
    result: dict[str, dict[str, DispositionSpec]] = {}
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            wf = row["workflow_id"].strip()
            code = row["code"].strip()
            spec = DispositionSpec(
                workflow_id=wf,
                code=code,
                label=row["label"].strip(),
                is_terminal=_parse_bool(row["is_terminal"]),
                is_success=_parse_bool(row["is_success"]),
            )
            result.setdefault(wf, {})[code] = spec
    return result


def load_registry(schemas_dir: Path) -> SchemaRegistry:
    """Load all *.json workflow schemas and dispositions.csv from schemas_dir."""
    workflows: dict[str, WorkflowSchema] = {}
    for json_path in sorted(schemas_dir.glob("*.json")):
        schema = load_workflow_schema(json_path)
        workflows[schema.workflow_id] = schema

    if not workflows:
        raise ValueError(f"No workflow JSON schemas found in {schemas_dir}")

    disp_path = schemas_dir / "dispositions.csv"
    if not disp_path.is_file():
        raise ValueError(f"Disposition catalogue not found at {disp_path}")

    dispositions = load_dispositions(disp_path)
    return SchemaRegistry(workflows=workflows, dispositions=dispositions)

