from __future__ import annotations

import json
import pickle
from collections.abc import Iterator, Mapping

import pytest

from hermes_workflows import WorkflowEngine, workflow
from hermes_workflows.runtime_services import EmptyRuntimeServicesV1, RuntimeServicesV1


@workflow
async def runtime_service_contract_workflow(inputs):
    return {"value": inputs["value"]}


class DuplicateItemsMapping(Mapping[str, object]):
    """Deliberately broken Mapping used to prove duplicate input rejection."""

    def __getitem__(self, key: str) -> object:
        if key == "test.recording":
            return object()
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield "test.recording"

    def __len__(self) -> int:
        return 1

    def items(self):  # type: ignore[override]
        recording = object()
        return (("test.recording", recording), ("test.recording", recording))


def test_runtime_services_resolve_recording_object_by_identity():
    recording = object()
    services = RuntimeServicesV1(services={"test.recording": recording})

    assert services.resolve("test.recording", 1) is recording
    assert services.resolve("test.missing", 1) is None


def test_empty_runtime_services_preserve_default_engine_path(tmp_path):
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    assert isinstance(engine.runtime_services, EmptyRuntimeServicesV1)
    assert engine.resolve_runtime_service("test.missing", 1) is None
    result = engine.run_until_idle(
        runtime_service_contract_workflow,
        {"value": "unchanged"},
        workflow_id="wf_runtime_service_default",
    )
    assert result.status == "completed"
    assert result.result == {"value": "unchanged"}


@pytest.mark.parametrize("service_id", ["", "Uppercase", "1leading", "has space", "a" * 65, 1, None])
def test_runtime_services_reject_malformed_service_ids(service_id):
    with pytest.raises(ValueError, match="service_id"):
        RuntimeServicesV1(services={service_id: object()})

    with pytest.raises(ValueError, match="service_id"):
        EmptyRuntimeServicesV1().resolve(service_id, 1)


@pytest.mark.parametrize("contract_version", [0, -1, True, 1.0, "1", None])
def test_runtime_services_reject_malformed_contract_versions(contract_version):
    services = RuntimeServicesV1(services={})

    with pytest.raises(ValueError, match="contract_version"):
        services.resolve("test.recording", contract_version)


@pytest.mark.parametrize("schema_version", [0, 2, True, "1", None])
def test_runtime_services_require_schema_version_one(schema_version):
    with pytest.raises(ValueError, match="schema_version"):
        RuntimeServicesV1(schema_version=schema_version, services={})


def test_runtime_services_reject_duplicate_ids():
    with pytest.raises(ValueError, match="duplicate service_id"):
        RuntimeServicesV1(services=DuplicateItemsMapping())


def test_runtime_services_are_process_local_and_nonserializable():
    services = RuntimeServicesV1(services={"test.recording": object()})
    empty = EmptyRuntimeServicesV1()

    for registry in (services, empty):
        with pytest.raises(TypeError):
            json.dumps(registry)
        with pytest.raises(TypeError, match="process-local"):
            pickle.dumps(registry)


def test_engine_stores_one_registry_without_persisting_it(tmp_path):
    db_path = tmp_path / "workflow.sqlite"
    recording = object()
    services = RuntimeServicesV1(services={"test.recording": recording})
    engine = WorkflowEngine(db_path, runtime_services=services)

    assert engine.runtime_services is services
    assert engine.resolve_runtime_service("test.recording", 1) is recording
    result = engine.run_until_idle(
        runtime_service_contract_workflow,
        {"value": "recording stays process-local"},
        workflow_id="wf_runtime_service_injected",
    )
    assert result.status == "completed"

    assert "test.recording" not in db_path.read_bytes().decode("utf-8", errors="ignore")
    assert all("test.recording" not in json.dumps(event) for event in engine.events("wf_runtime_service_injected"))
