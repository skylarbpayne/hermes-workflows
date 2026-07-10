from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, Sequence, runtime_checkable

if TYPE_CHECKING:
    from .types import JsonValue
else:
    JsonValue = Any


_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SECTION_FIELDS = frozenset({"schema_version", "section_id", "summary", "detail_ref"})
_MAX_SUMMARY_BYTES = 8192
_MAX_DETAIL_REF_BYTES = 512


@dataclass(frozen=True, init=False)
class ProjectionSectionV1:
    schema_version: int
    section_id: str
    summary: Mapping[str, JsonValue]
    detail_ref: str | None

    def __init__(
        self,
        section_id: str,
        summary: Mapping[str, JsonValue],
        detail_ref: str | None = None,
        schema_version: int = 1,
    ) -> None:
        if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
            raise ValueError("schema_version must equal 1")
        _validate_id(section_id, label="section_id")
        if not isinstance(summary, Mapping):
            raise TypeError("summary must be a JSON object")
        normalized_summary = _normalize_json_object(summary)
        summary_json = _canonical_json(normalized_summary)
        if len(summary_json.encode("utf-8")) > _MAX_SUMMARY_BYTES:
            raise ValueError(f"summary canonical JSON must be <= {_MAX_SUMMARY_BYTES} UTF-8 bytes")
        if detail_ref is not None:
            if not isinstance(detail_ref, str) or not detail_ref.strip():
                raise ValueError("detail_ref must be a nonblank string or None")
            if len(detail_ref.encode("utf-8")) > _MAX_DETAIL_REF_BYTES:
                raise ValueError(f"detail_ref must be <= {_MAX_DETAIL_REF_BYTES} UTF-8 bytes")

        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "section_id", section_id)
        object.__setattr__(self, "summary", MappingProxyType(normalized_summary))
        object.__setattr__(self, "detail_ref", detail_ref)

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": self.schema_version,
            "section_id": self.section_id,
            "summary": _normalize_json_object(self.summary),
            "detail_ref": self.detail_ref,
        }

    def to_json(self) -> str:
        return encode_projection_section(self)

    @classmethod
    def from_json(cls, value: str | bytes | bytearray) -> "ProjectionSectionV1":
        return decode_projection_section(value)


@runtime_checkable
class ProjectionContributorV1(Protocol):
    def project(self, workflow_id: str) -> tuple[ProjectionSectionV1, ...]:
        ...


def encode_projection_section(section: ProjectionSectionV1) -> str:
    if not isinstance(section, ProjectionSectionV1):
        raise TypeError("section must be ProjectionSectionV1")
    return _canonical_json(section.to_dict())


def decode_projection_section(value: str | bytes | bytearray) -> ProjectionSectionV1:
    try:
        payload = json.loads(value)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("projection section must be valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise TypeError("projection section JSON must be an object")
    unknown = set(payload) - _SECTION_FIELDS
    missing = _SECTION_FIELDS - set(payload)
    if unknown:
        raise ValueError(f"unknown projection section fields: {sorted(unknown)}")
    if missing:
        raise ValueError(f"missing projection section fields: {sorted(missing)}")
    return ProjectionSectionV1(
        schema_version=payload["schema_version"],
        section_id=payload["section_id"],
        summary=payload["summary"],
        detail_ref=payload["detail_ref"],
    )


def collect_projection_sections(
    workflow_id: str,
    contributors: Sequence[ProjectionContributorV1],
) -> tuple[ProjectionSectionV1, ...]:
    validate_workflow_id(workflow_id)
    sections: list[ProjectionSectionV1] = []
    section_ids: set[str] = set()
    for contributor in contributors:
        contributed = contributor.project(workflow_id)
        if not isinstance(contributed, tuple):
            raise TypeError("projection contributors must return a tuple")
        for section in contributed:
            if not isinstance(section, ProjectionSectionV1):
                raise TypeError("projection contributors must return ProjectionSectionV1 values")
            if section.section_id in section_ids:
                raise ValueError(f"duplicate projection section_id: {section.section_id}")
            section_ids.add(section.section_id)
            sections.append(section)
    return tuple(sections)


def validate_workflow_id(workflow_id: object) -> str:
    if not isinstance(workflow_id, str) or not workflow_id.strip():
        raise ValueError("workflow_id must be nonblank")
    return workflow_id


def _validate_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must match {_ID_PATTERN.pattern}")
    return value


def _normalize_json_object(value: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    normalized = _normalize_json_value(value)
    if not isinstance(normalized, dict):
        raise TypeError("summary must be a JSON object")
    return normalized


def _normalize_json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("JSON numbers must be finite")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, JsonValue] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON object keys must be strings")
            normalized[key] = _normalize_json_value(item)
        return normalized
    if isinstance(value, list):
        return [_normalize_json_value(item) for item in value]
    raise TypeError(f"value of type {type(value).__name__} is not a JSON value")


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
