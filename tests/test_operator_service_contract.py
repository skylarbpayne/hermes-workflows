from __future__ import annotations

import json
import pickle
import threading
from collections.abc import Iterator, Mapping
from urllib.request import urlopen

import pytest

from hermes_workflows.dashboard_server import DashboardHandler, DashboardServer
from hermes_workflows.engine import WorkflowEngine
from hermes_workflows.operator_services import OperatorServiceRegistry, OperatorServicesV1
from hermes_workflows.projection_sections import (
    ProjectionContributorV1,
    ProjectionSectionV1,
    decode_projection_section,
    encode_projection_section,
)
from hermes_workflows.status_projection import JsonCodec, StatusProjection
from hermes_workflows.types import to_json_value


class DuplicateItemsMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        if key == "review.service":
            return object()
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield "review.service"

    def __len__(self) -> int:
        return 1

    def items(self):  # type: ignore[override]
        marker = object()
        return (("review.service", marker), ("review.service", marker))


class RecordingContributor:
    def __init__(self, *sections: ProjectionSectionV1) -> None:
        self.sections = sections
        self.workflow_ids: list[str] = []

    def project(self, workflow_id: str) -> tuple[ProjectionSectionV1, ...]:
        self.workflow_ids.append(workflow_id)
        return self.sections


def test_operator_service_registry_contract_and_identity_lookup():
    service = object()
    registry = OperatorServicesV1(services={"review.service": service})

    assert isinstance(registry, OperatorServiceRegistry)
    assert registry.resolve("review.service", 1) is service
    assert registry.resolve("missing.service", 1) is None

    for service_id in ("", "Review", "a/b", "a" * 65):
        with pytest.raises(ValueError):
            registry.resolve(service_id, 1)
    for version in (True, 0, -1, 2, 999, 1.5, "1"):
        with pytest.raises(ValueError):
            registry.resolve("review.service", version)  # type: ignore[arg-type]


def test_operator_service_registry_rejects_invalid_schema_ids_duplicates_and_serialization():
    with pytest.raises(ValueError):
        OperatorServicesV1(schema_version=2, services={})
    with pytest.raises(ValueError):
        OperatorServicesV1(services={"Not.Valid": object()})
    with pytest.raises(ValueError):
        OperatorServicesV1(services=DuplicateItemsMapping())

    registry = OperatorServicesV1(services={})
    with pytest.raises(TypeError):
        json.dumps(registry)
    with pytest.raises(TypeError, match="process-local"):
        to_json_value(registry)
    with pytest.raises(TypeError, match="process-local"):
        JsonCodec.dumps(registry)
    with pytest.raises(TypeError):
        pickle.dumps(registry)


def test_projection_section_exact_canonical_json_round_trip():
    section = ProjectionSectionV1(
        section_id="revision.summary",
        summary={"z": [1, True, None], "a": "✓"},
        detail_ref="artifact:revision/123",
    )

    encoded = encode_projection_section(section)

    assert encoded == (
        '{"detail_ref":"artifact:revision/123","schema_version":1,'
        '"section_id":"revision.summary","summary":{"a":"✓","z":[1,true,null]}}'
    )
    assert decode_projection_section(encoded) == section
    assert section.to_dict() == {
        "schema_version": 1,
        "section_id": "revision.summary",
        "summary": {"a": "✓", "z": [1, True, None]},
        "detail_ref": "artifact:revision/123",
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 2, "section_id": "valid", "summary": {}, "detail_ref": None},
        {"schema_version": 1, "section_id": "valid", "summary": {}, "detail_ref": None, "extra": True},
        {"schema_version": 1, "section_id": "valid", "summary": []},
    ],
)
def test_projection_section_decode_rejects_unknown_version_fields_and_non_objects(payload):
    with pytest.raises((TypeError, ValueError)):
        decode_projection_section(json.dumps(payload))


def test_projection_section_validation_bounds_and_json_rules():
    with pytest.raises(ValueError):
        ProjectionSectionV1(section_id="Invalid", summary={})
    with pytest.raises(TypeError):
        ProjectionSectionV1(section_id="valid", summary=[])  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        ProjectionSectionV1(section_id="valid", summary={"bad": object()})
    with pytest.raises(ValueError):
        ProjectionSectionV1(section_id="valid", summary={"bad": float("nan")})
    with pytest.raises(ValueError):
        ProjectionSectionV1(section_id="valid", summary={"payload": "x" * 8193})
    with pytest.raises(ValueError):
        ProjectionSectionV1(section_id="valid", summary={}, detail_ref=" ")
    for detail_ref in ("not a uri", "abc", "://"):
        with pytest.raises(ValueError, match="URI-like"):
            ProjectionSectionV1(section_id="valid", summary={}, detail_ref=detail_ref)
    with pytest.raises(ValueError):
        ProjectionSectionV1(section_id="valid", summary={}, detail_ref="artifact:" + "é" * 252)

    exact_limit = ProjectionSectionV1(section_id="valid", summary={"payload": "x" * 8178})
    assert len(json.dumps(dict(exact_limit.summary), sort_keys=True, separators=(",", ":")).encode("utf-8")) == 8192


def test_status_projection_is_unchanged_without_contributors_and_appends_validated_sections(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    projection = StatusProjection(engine)

    assert projection._contributors == ()

    engine.start(lambda inputs: inputs, {}, workflow_id="wf_projection_sections")
    unchanged = projection.workflow_status("wf_projection_sections")
    assert "projection_sections" not in unchanged

    contributor = RecordingContributor(
        ProjectionSectionV1(section_id="revision.summary", summary={"attempt": 2}),
    )
    contributed = StatusProjection(engine, contributors=(contributor,)).workflow_status("wf_projection_sections")

    assert contributed["projection_sections"] == [
        {
            "schema_version": 1,
            "section_id": "revision.summary",
            "summary": {"attempt": 2},
            "detail_ref": None,
        }
    ]
    assert contributor.workflow_ids == ["wf_projection_sections"]


def test_status_projection_rejects_blank_workflow_and_duplicate_contributed_sections(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    section = ProjectionSectionV1(section_id="duplicate", summary={})
    projection = StatusProjection(
        engine,
        contributors=(RecordingContributor(section), RecordingContributor(section)),
    )

    with pytest.raises(ValueError):
        projection.workflow_status(" ")

    engine.start(lambda inputs: inputs, {}, workflow_id="wf_duplicate_sections")
    with pytest.raises(ValueError, match="duplicate"):
        projection.workflow_status("wf_duplicate_sections")


def test_projection_protocols_are_runtime_checkable():
    assert isinstance(RecordingContributor(), ProjectionContributorV1)


@pytest.mark.filterwarnings("ignore::DeprecationWarning")
def test_dashboard_server_stores_registry_and_serves_real_temporary_sqlite_status(tmp_path):
    db = tmp_path / "workflow.sqlite"
    engine = WorkflowEngine(db)
    engine.start(lambda inputs: inputs, {}, workflow_id="wf_dashboard_service_registry")
    registry = OperatorServicesV1(services={"status.contributor": RecordingContributor()})
    server = DashboardServer(
        ("127.0.0.1", 0),
        DashboardHandler,
        db_path=db,
        workflow=None,
        workflow_ref="tests:workflow",
        operator_services=registry,
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        body = urlopen(server.url, timeout=5).read().decode("utf-8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert server.operator_services is registry
    assert "Hermes Workflows Dashboard" in body
    assert "wf_dashboard_service_registry" in body
    assert "Approval actions disabled" in body
