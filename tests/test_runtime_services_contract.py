from __future__ import annotations

import json
import pickle
from collections.abc import Iterator, Mapping, Sequence, Set
from dataclasses import dataclass
from typing import overload

import pytest

from hermes_workflows import WorkflowEngine, workflow
from hermes_workflows.artifacts import JsonArtifact
from hermes_workflows.engine import JsonCodec
from hermes_workflows.runtime_services import (
    EmptyRuntimeServicesV1,
    RuntimeOnlyServiceRegistry,
    RuntimeServicesV1,
)
from hermes_workflows.status_projection import JsonCodec as StatusProjectionJsonCodec
from hermes_workflows.types import to_json_value


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


@dataclass(frozen=True)
class IntegrationOwnedRuntimeServices(RuntimeOnlyServiceRegistry):
    marker: str

    def resolve(self, service_id: str, contract_version: int) -> object | None:
        return None


class UnmarkedStructuralRuntimeServices:
    def resolve(self, service_id: str, contract_version: int) -> object | None:
        return None


class HashableCycleSequence(Sequence[object]):
    def __init__(self, registry: RuntimeOnlyServiceRegistry | None = None):
        self._items = (self, registry) if registry is not None else (self,)

    def __hash__(self) -> int:
        return id(self)

    def __getitem__(self, index):
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)


class HashableCycleMapping(Mapping[object, object]):
    def __init__(self, registry: RuntimeOnlyServiceRegistry):
        self._items = (("self", self), ("registry", registry))

    def __hash__(self) -> int:
        return id(self)

    def __getitem__(self, key: object) -> object:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[object]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def items(self):  # type: ignore[override]
        return self._items


class HashableCycleSet(Set[object]):
    def __init__(self, registry: RuntimeOnlyServiceRegistry):
        self._items = (self, registry)

    def __hash__(self) -> int:
        return id(self)

    def __contains__(self, item: object) -> bool:
        return any(candidate is item for candidate in self._items)

    def __iter__(self) -> Iterator[object]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


@dataclass(eq=False)
class HashableCycleDataclass:
    self_reference: object | None = None
    registry: RuntimeOnlyServiceRegistry | None = None

    def __hash__(self) -> int:
        return id(self)


@dataclass(eq=False)
class HashableDataclassMapping(Mapping[object, object]):
    label: str

    def __post_init__(self) -> None:
        self._items: tuple[tuple[object, object], ...] = ()

    def hide(self, *items: tuple[object, object]) -> None:
        self._items = items

    def __hash__(self) -> int:
        return id(self)

    def __str__(self) -> str:
        for _, value in self._items:
            if isinstance(value, IntegrationOwnedRuntimeServices):
                return value.marker
        return self.label

    def __getitem__(self, key: object) -> object:
        for candidate, value in self._items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[object]:
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def items(self):  # type: ignore[override]
        return self._items


@dataclass(eq=False)
class HashableDataclassSequence(Sequence[object]):
    label: str

    def __post_init__(self) -> None:
        self._items: tuple[object, ...] = ()

    def hide(self, *items: object) -> None:
        self._items = items

    def __hash__(self) -> int:
        return id(self)

    @overload
    def __getitem__(self, index: int) -> object: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[object]: ...

    def __getitem__(self, index: int | slice) -> object | Sequence[object]:
        return self._items[index]

    def __len__(self) -> int:
        return len(self._items)


@dataclass(eq=False)
class HashableDataclassSet(Set[object]):
    label: str

    def __post_init__(self) -> None:
        self._items: tuple[object, ...] = ()

    def hide(self, *items: object) -> None:
        self._items = items

    def __hash__(self) -> int:
        return id(self)

    def __contains__(self, item: object) -> bool:
        return any(candidate is item for candidate in self._items)

    def __iter__(self) -> Iterator[object]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)


class HashableMappingSequence(Mapping[object, object], Sequence[object]):
    def __init__(self, registry: IntegrationOwnedRuntimeServices):
        self._mapping_items = (("safe", "value"), ("also-safe", "value"))
        self._sequence_items = (self, registry)

    def __hash__(self) -> int:
        return id(self)

    def __str__(self) -> str:
        return self._sequence_items[1].marker

    def __getitem__(self, key):  # type: ignore[override]
        if isinstance(key, int):
            return self._sequence_items[key]
        for candidate, value in self._mapping_items:
            if candidate == key:
                return value
        raise KeyError(key)

    def __iter__(self) -> Iterator[object]:
        return (key for key, _ in self._mapping_items)

    def __len__(self) -> int:
        return len(self._mapping_items)

    def items(self):  # type: ignore[override]
        return self._mapping_items


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
    services = RuntimeServicesV1(services={"test.recording": "must not leak"})
    empty = EmptyRuntimeServicesV1()

    for registry in (services, empty):
        with pytest.raises(TypeError):
            json.dumps(registry)
        with pytest.raises(TypeError, match="process-local"):
            pickle.dumps(registry)
        with pytest.raises(TypeError, match="process-local"):
            JsonCodec.dumps(registry)
        with pytest.raises(TypeError, match="process-local"):
            JsonCodec.dumps({"nested": [registry]})


@pytest.mark.parametrize(
    "registry",
    [
        RuntimeServicesV1(services={"test.recording": "must not leak"}),
        EmptyRuntimeServicesV1(),
    ],
)
def test_runtime_services_reject_framework_persistence_helpers(registry):
    for serialize in (
        to_json_value,
        StatusProjectionJsonCodec.dumps,
        lambda value: JsonArtifact("runtime services", value),
    ):
        with pytest.raises(TypeError, match="process-local"):
            serialize(registry)


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


def test_engine_preserves_marked_registry_identity_without_mutation_or_wrapping(tmp_path):
    db_path = tmp_path / "workflow.sqlite"
    marker = "integration-registry-must-not-persist"
    registry = IntegrationOwnedRuntimeServices(marker=marker)
    original_type = type(registry)
    original_state = dict(registry.__dict__)
    engine = WorkflowEngine(db_path, runtime_services=registry)

    assert engine.runtime_services is registry
    assert type(registry) is original_type
    assert registry.__dict__ == original_state
    assert engine.resolve_runtime_service("integration.any", 1) is None


def test_engine_rejects_unmarked_structural_registry(tmp_path):
    registry = UnmarkedStructuralRuntimeServices()

    with pytest.raises(TypeError, match="runtime-only marker"):
        WorkflowEngine(tmp_path / "workflow.sqlite", runtime_services=registry)


def test_marked_registry_rejected_by_framework_serializers_and_persistence(tmp_path):
    db_path = tmp_path / "workflow.sqlite"
    marker = "integration-registry-must-not-persist"
    registry = IntegrationOwnedRuntimeServices(marker=marker)
    engine = WorkflowEngine(db_path, runtime_services=registry)
    nested = {"outer": [{"registry": registry}]}

    for serialize in (
        to_json_value,
        JsonCodec.dumps,
        StatusProjectionJsonCodec.dumps,
        lambda value: JsonArtifact("runtime services", value),
    ):
        with pytest.raises(TypeError, match="process-local"):
            serialize(nested)

    with pytest.raises(TypeError, match="process-local"):
        engine.start(
            runtime_service_contract_workflow,
            {"value": nested},
            workflow_id="wf_marked_runtime_service_registry",
        )

    assert marker not in db_path.read_bytes().decode("utf-8", errors="ignore")
    with pytest.raises(KeyError, match="unknown workflow_id"):
        engine.events("wf_marked_runtime_service_registry")


@pytest.mark.parametrize(
    ("key_factory", "workflow_id"),
    [
        pytest.param(lambda registry: registry, "wf_registry_direct_mapping_key", id="direct-key"),
        pytest.param(lambda registry: ("nested", registry), "wf_registry_nested_mapping_key", id="nested-key"),
        pytest.param(
            lambda registry: frozenset({registry}),
            "wf_registry_direct_frozenset_mapping_key",
            id="direct-frozenset-key",
        ),
        pytest.param(
            lambda registry: ("nested", frozenset({registry})),
            "wf_registry_nested_frozenset_mapping_key",
            id="nested-frozenset-key",
        ),
    ],
)
def test_marked_registry_rejected_as_mapping_key_by_serializers_and_persistence(
    tmp_path,
    key_factory,
    workflow_id,
):
    db_path = tmp_path / "workflow.sqlite"
    secret = "mapping-key-registry-secret-must-not-persist"
    registry = IntegrationOwnedRuntimeServices(marker=secret)
    payload = {key_factory(registry): "safe value"}

    for serialize in (
        to_json_value,
        JsonCodec.dumps,
        StatusProjectionJsonCodec.dumps,
        lambda value: JsonArtifact("runtime services", value),
    ):
        with pytest.raises(TypeError, match="process-local"):
            serialize(payload)

    engine = WorkflowEngine(db_path, runtime_services=registry)
    with pytest.raises(TypeError, match="process-local"):
        engine.start(
            runtime_service_contract_workflow,
            {"value": payload},
            workflow_id=workflow_id,
        )

    assert secret not in db_path.read_bytes().decode("utf-8", errors="ignore")
    with pytest.raises(KeyError, match="unknown workflow_id"):
        engine.events(workflow_id)


def test_safe_mapping_keys_preserve_string_conversion_compatibility():
    payload = {7: "integer", ("safe", 1): "tuple"}

    assert to_json_value(payload) == {"7": "integer", "('safe', 1)": "tuple"}


def _overlapping_shape_key(shape: str, registry: RuntimeOnlyServiceRegistry) -> object:
    if shape == "mapping":
        value = HashableDataclassMapping("safe dataclass field")
        value.hide(("self", value), ("registry", registry))
        return value
    if shape == "sequence":
        value = HashableDataclassSequence("safe dataclass field")
        value.hide(value, registry)
        return value
    if shape == "set":
        value = HashableDataclassSet("safe dataclass field")
        value.hide(value, registry)
        return value
    if shape == "mapping-sequence":
        assert isinstance(registry, IntegrationOwnedRuntimeServices)
        return HashableMappingSequence(registry)
    raise AssertionError(f"unknown shape: {shape}")


@pytest.mark.parametrize("shape", ["mapping", "sequence", "set", "mapping-sequence"])
def test_overlapping_dataclass_container_keys_traverse_every_applicable_shape(tmp_path, shape):
    db_path = tmp_path / "workflow.sqlite"
    secret = f"overlapping-{shape}-registry-secret-must-not-persist"
    registry = IntegrationOwnedRuntimeServices(marker=secret)
    payload = {_overlapping_shape_key(shape, registry): "safe value"}

    for serialize in (
        to_json_value,
        JsonCodec.dumps,
        StatusProjectionJsonCodec.dumps,
        lambda value: JsonArtifact("overlapping key", value),
    ):
        with pytest.raises(TypeError, match="process-local"):
            serialize(payload)

    workflow_id = f"wf_overlapping_{shape}_registry_mapping_key"
    engine = WorkflowEngine(db_path, runtime_services=registry)
    with pytest.raises(TypeError, match="process-local"):
        engine.start(
            runtime_service_contract_workflow,
            {"value": payload},
            workflow_id=workflow_id,
        )

    assert secret not in db_path.read_bytes().decode("utf-8", errors="ignore")
    with pytest.raises(KeyError, match="unknown workflow_id"):
        engine.events(workflow_id)


def _cyclic_key_with_registry(shape: str, registry: RuntimeOnlyServiceRegistry) -> object:
    if shape == "sequence":
        return HashableCycleSequence(registry)
    if shape == "mapping":
        return HashableCycleMapping(registry)
    if shape == "set":
        return HashableCycleSet(registry)
    if shape == "dataclass":
        value = HashableCycleDataclass(registry=registry)
        value.self_reference = value
        return value
    raise AssertionError(f"unknown shape: {shape}")


@pytest.mark.parametrize("shape", ["sequence", "mapping", "set", "dataclass"])
def test_cyclic_mapping_keys_do_not_mask_marked_registry_rejection(tmp_path, shape):
    db_path = tmp_path / "workflow.sqlite"
    secret = f"cyclic-{shape}-registry-secret-must-not-persist"
    registry = IntegrationOwnedRuntimeServices(marker=secret)
    payload = {_cyclic_key_with_registry(shape, registry): "safe value"}

    for serialize in (
        to_json_value,
        JsonCodec.dumps,
        StatusProjectionJsonCodec.dumps,
        lambda value: JsonArtifact("runtime services", value),
    ):
        with pytest.raises(TypeError, match="process-local"):
            serialize(payload)

    workflow_id = f"wf_cyclic_{shape}_registry_mapping_key"
    engine = WorkflowEngine(db_path, runtime_services=registry)
    with pytest.raises(TypeError, match="process-local"):
        engine.start(
            runtime_service_contract_workflow,
            {"value": payload},
            workflow_id=workflow_id,
        )

    assert secret not in db_path.read_bytes().decode("utf-8", errors="ignore")
    with pytest.raises(KeyError, match="unknown workflow_id"):
        engine.events(workflow_id)


def test_safe_cyclic_non_string_mapping_key_fails_closed_deterministically():
    payload = {HashableCycleSequence(): "safe value"}

    for serialize in (
        to_json_value,
        JsonCodec.dumps,
        StatusProjectionJsonCodec.dumps,
        lambda value: JsonArtifact("cyclic key", value),
    ):
        with pytest.raises(TypeError, match="cyclic mapping keys are not JSON-serializable"):
            serialize(payload)
