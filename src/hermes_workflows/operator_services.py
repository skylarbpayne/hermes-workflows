from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable


_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")


@runtime_checkable
class OperatorServiceRegistry(Protocol):
    def resolve(self, service_id: str, contract_version: int) -> object | None:
        ...


@dataclass(frozen=True, init=False)
class OperatorServicesV1:
    schema_version: int
    services: Mapping[str, object]

    def __init__(self, services: Mapping[str, object], schema_version: int = 1) -> None:
        if isinstance(schema_version, bool) or not isinstance(schema_version, int) or schema_version != 1:
            raise ValueError("schema_version must equal 1")
        if not isinstance(services, Mapping):
            raise TypeError("services must be a mapping")

        copied: dict[str, object] = {}
        for service_id, service in services.items():
            _validate_id(service_id, label="service_id")
            if service_id in copied:
                raise ValueError(f"duplicate service_id: {service_id}")
            copied[service_id] = service

        object.__setattr__(self, "schema_version", schema_version)
        object.__setattr__(self, "services", MappingProxyType(copied))

    def resolve(self, service_id: str, contract_version: int) -> object | None:
        _validate_id(service_id, label="service_id")
        if isinstance(contract_version, bool) or not isinstance(contract_version, int) or contract_version != 1:
            raise ValueError("contract_version must equal 1")
        return self.services.get(service_id)

    def __getattribute__(self, name: str) -> object:
        if name == "__dataclass_fields__":
            # Framework JSON normalization introspects dataclass fields. Refuse
            # that process-local serialization path without changing the shared
            # serializer or weakening this type's frozen-dataclass contract.
            raise TypeError("operator service registries are process-local and nonserializable")
        return object.__getattribute__(self, name)

    def __reduce_ex__(self, protocol: object):
        raise TypeError("operator service registries are process-local and nonserializable")


def _validate_id(value: object, *, label: str) -> str:
    if not isinstance(value, str) or _ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{label} must match {_ID_PATTERN.pattern}")
    return value
