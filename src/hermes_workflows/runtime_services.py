from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol, SupportsIndex


_SERVICE_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


class RuntimeServiceRegistry(Protocol):
    def resolve(self, service_id: str, contract_version: int) -> object | None: ...


@dataclass(frozen=True)
class RuntimeServicesV1:
    schema_version: int = 1
    services: Mapping[str, object] = field(default_factory=dict)

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
