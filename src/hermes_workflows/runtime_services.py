from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field, is_dataclass, make_dataclass
from types import MappingProxyType
from typing import Any, Callable, Protocol, SupportsIndex, cast, runtime_checkable


_SERVICE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


@runtime_checkable
class RuntimeServiceRegistry(Protocol):
    def resolve(self, service_id: str, contract_version: int) -> object | None: ...


def _make_registry_process_local(registry: RuntimeServiceRegistry) -> RuntimeServiceRegistry:
    if isinstance(registry, (RuntimeServicesV1, EmptyRuntimeServicesV1)):
        return registry

    registry_type = cast(type[Any], type(registry))
    original_getattribute = cast(Callable[[object, str], object], registry_type.__getattribute__)

    def guarded_getattribute(self: object, name: str) -> object:
        if name == "_serialization_guard":
            raise TypeError("runtime service registries are process-local and cannot be serialized")
        return original_getattribute(self, name)

    def reject_pickle(self: object, protocol: SupportsIndex):
        raise TypeError("runtime service registries are process-local and cannot be pickled")

    namespace = {
        "__module__": registry_type.__module__,
        "__getattribute__": guarded_getattribute,
        "__reduce_ex__": reject_pickle,
    }
    if is_dataclass(registry) and not isinstance(registry, type):
        dataclass_params = registry_type.__dataclass_params__
        guarded_type = make_dataclass(
            f"_ProcessLocal{registry_type.__name__}",
            [("_serialization_guard", object, field(init=False, repr=False, compare=False))],
            bases=(registry_type,),
            namespace=namespace,
            frozen=dataclass_params.frozen,
        )
    else:
        guarded_type = type(f"_ProcessLocal{registry_type.__name__}", (registry_type,), namespace)

    try:
        object.__setattr__(registry, "__class__", guarded_type)
    except TypeError as exc:
        raise TypeError("runtime service registry cannot be made process-local") from exc
    return registry


@dataclass(frozen=True)
class RuntimeServicesV1:
    _serialization_guard: object = field(init=False, repr=False, compare=False)
    schema_version: int = 1
    services: Mapping[str, object] = field(default_factory=dict)

    def __getattribute__(self, name: str) -> object:
        if name == "_serialization_guard":
            raise TypeError("runtime service registries are process-local and cannot be serialized")
        return super().__getattribute__(name)

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("schema_version must equal 1")
        if not isinstance(self.services, Mapping):
            raise TypeError("services must be a mapping")

        validated: dict[str, object] = {}
        for service_id, service in self.services.items():
            _validate_service_id(service_id)
            if service_id in validated:
                raise ValueError(f"duplicate service_id: {service_id}")
            validated[service_id] = service
        object.__setattr__(self, "services", MappingProxyType(validated))

    def resolve(self, service_id: str, contract_version: int) -> object | None:
        _validate_resolution(service_id, contract_version)
        return self.services.get(service_id)

    def __reduce_ex__(self, protocol: SupportsIndex):
        raise TypeError("runtime service registries are process-local and cannot be pickled")


@dataclass(frozen=True)
class EmptyRuntimeServicesV1:
    _serialization_guard: object = field(init=False, repr=False, compare=False)

    def __getattribute__(self, name: str) -> object:
        if name == "_serialization_guard":
            raise TypeError("runtime service registries are process-local and cannot be serialized")
        return super().__getattribute__(name)

    def resolve(self, service_id: str, contract_version: int) -> object | None:
        _validate_resolution(service_id, contract_version)
        return None

    def __reduce_ex__(self, protocol: SupportsIndex):
        raise TypeError("runtime service registries are process-local and cannot be pickled")


def _validate_service_id(service_id: object) -> None:
    if not isinstance(service_id, str) or _SERVICE_ID_PATTERN.fullmatch(service_id) is None:
        raise ValueError("service_id must match ^[a-z][a-z0-9_.-]{0,63}$")


def _validate_resolution(service_id: object, contract_version: object) -> None:
    _validate_service_id(service_id)
    if type(contract_version) is not int or contract_version < 1:
        raise ValueError("contract_version must be an integer >= 1")
