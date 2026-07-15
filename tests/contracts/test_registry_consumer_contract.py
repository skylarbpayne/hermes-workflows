from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hermes_workflows.operator_services import OperatorServicesV1
from hermes_workflows.registry_v2 import (
    REGISTRY_IDENTITY_CONTRACT_VERSION,
    REGISTRY_IDENTITY_SERVICE_ID,
    RegistryContractError,
    RegistryIdentityServiceV1,
    require_consumer_parity,
)


FIXTURES = Path(__file__).parents[1] / "fixtures"


def _service(root: Path, fixture: str = "registry_v2_valid.json") -> RegistryIdentityServiceV1:
    registry = root / ".hermes" / "workflows.registry.json"
    registry.parent.mkdir(parents=True)
    shutil.copy2(FIXTURES / fixture, registry)
    return RegistryIdentityServiceV1.from_file(registry)


def _consume(service: RegistryIdentityServiceV1, db_alias: str) -> dict[str, object]:
    return service.identity(db_alias).to_dict()


def test_cli_plugin_and_supervisor_contract_consumers_share_one_service_identity(tmp_path: Path) -> None:
    service = _service(tmp_path / "workspace")
    services = OperatorServicesV1(services={REGISTRY_IDENTITY_SERVICE_ID: service})

    resolved = services.resolve(REGISTRY_IDENTITY_SERVICE_ID, REGISTRY_IDENTITY_CONTRACT_VERSION)

    assert resolved is service
    cli = _consume(service, "primary")
    plugin = _consume(service, "primary")
    supervisor = _consume(service, "primary")
    identity = require_consumer_parity(
        {
            "cli": service.identity("primary"),
            "plugin": service.identity("primary"),
            "supervisor": service.identity("primary"),
        }
    )
    assert cli == plugin == supervisor == identity.to_dict()
    assert set(cli) == {
        "schema_version",
        "registry_fingerprint",
        "registry_identity",
        "db_alias",
        "resolved_db_identity",
    }
    assert not any("path" in key for key in cli)


def test_every_public_consumer_requires_an_alias_not_a_raw_path(tmp_path: Path) -> None:
    service = _service(tmp_path / "workspace")

    for consumer in (_consume, _consume, _consume):
        with pytest.raises(RegistryContractError) as raised:
            consumer(service, "state/primary/workflows.sqlite")
        assert raised.value.code == "registry_alias_required"
        assert raised.value.exit_code == 2


def test_consumer_drift_is_a_redacted_doctor_style_exit_2_error(tmp_path: Path) -> None:
    canonical = _service(tmp_path / "canonical")
    drifted = _service(tmp_path / "drifted", "registry_v2_drift.json")

    with pytest.raises(RegistryContractError) as raised:
        require_consumer_parity(
            {
                "cli": canonical.identity("primary"),
                "plugin": canonical.identity("primary"),
                "supervisor": drifted.identity("primary"),
            }
        )

    error = raised.value
    payload = error.to_dict()
    encoded = json.dumps(payload, sort_keys=True)
    assert error.code == "registry_drift"
    assert error.exit_code == 2
    assert payload["fields"] == {"consumers": ["cli", "plugin", "supervisor"]}
    assert "canonical" not in encoded
    assert "drifted" not in encoded
    assert "workflows.sqlite" not in encoded
    assert len(encoded.encode("utf-8")) <= 4096


def test_consumer_names_are_bounded_canonical_ids(tmp_path: Path) -> None:
    identity = _service(tmp_path / "workspace").identity("primary")

    for name in ("", "CLI", "has/slash", "a" * 65):
        with pytest.raises(RegistryContractError) as raised:
            require_consumer_parity({name: identity})
        assert raised.value.code == "registry_invalid_consumer"
        assert raised.value.exit_code == 2
