from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_workflows.effects import (
    EFFECT_ADAPTER_CONTRACT_VERSION,
    EFFECT_ADAPTER_SERVICE_ID,
    EffectCoordinator,
    EffectPolicy,
    SQLiteEffectStore,
    operation_identity,
    resolve_effect_adapter,
)
from hermes_workflows.runtime_services import RuntimeServicesV1


class FileAdapter:
    adapter_id = "contract.file.v1"

    def __init__(self, path: Path):
        self.path = path
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS external_effects (
                    operation_id TEXT PRIMARY KEY,
                    input_hash TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS adapter_receipts (
                    operation_id TEXT PRIMARY KEY,
                    adapter_receipt_id TEXT NOT NULL
                );
                """
            )

    def lookup_receipt(self, operation_id: str):
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT adapter_receipt_id FROM adapter_receipts WHERE operation_id = ?",
                (operation_id,),
            ).fetchone()
        if row is None:
            return None
        return {"adapter_receipt_id": row[0], "operation_id": operation_id}

    def perform(self, operation_id: str, input_value):
        input_hash = operation_identity(
            workflow_id="wf-contract-001",
            effect_key="publish-report",
            adapter_id=self.adapter_id,
            input_value=input_value,
        ).input_hash
        receipt_id = f"file-{operation_id[-16:]}"
        with sqlite3.connect(self.path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "INSERT OR IGNORE INTO external_effects(operation_id, input_hash) VALUES (?, ?)",
                (operation_id, input_hash),
            )
            conn.execute(
                "INSERT OR IGNORE INTO adapter_receipts(operation_id, adapter_receipt_id) VALUES (?, ?)",
                (operation_id, receipt_id),
            )
            conn.commit()
        return {"adapter_receipt_id": receipt_id, "operation_id": operation_id}


class Resolver:
    def __init__(self, adapter):
        self.adapter = adapter

    def resolve_adapter(self, adapter_id: str):
        return self.adapter if adapter_id == self.adapter.adapter_id else None


def _run_adapter_worker(db_path: Path, operation_id: str, input_json: str) -> dict:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--adapter-worker",
        str(db_path),
        operation_id,
        input_json,
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def test_file_backed_adapter_subprocess_is_idempotent_and_lookupable(tmp_path):
    fixture = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "effect_contract_v1.json").read_text()
    )
    assert fixture["adapter_contract"]["unsafe_requires_durable_authorization"] is True
    vector = fixture["identity_vectors"][0]
    identity = operation_identity(
        workflow_id=vector["workflow_id"],
        effect_key=vector["effect_key"],
        adapter_id=vector["adapter_id"],
        input_value=vector["input"],
    )
    assert identity.operation_id == vector["operation_id"]
    assert identity.input_hash == vector["input_hash"]
    adapter_db = tmp_path / "adapter.sqlite"
    input_json = json.dumps(vector["input"], sort_keys=True, separators=(",", ":"))

    first = _run_adapter_worker(adapter_db, identity.operation_id, input_json)
    second = _run_adapter_worker(adapter_db, identity.operation_id, input_json)

    assert first["adapter_receipt_id"] == second["adapter_receipt_id"]
    assert first["external_effect_count"] == second["external_effect_count"] == 1
    assert FileAdapter(adapter_db).lookup_receipt(identity.operation_id) == {
        "adapter_receipt_id": first["adapter_receipt_id"],
        "operation_id": identity.operation_id,
    }


def test_adapter_resolves_only_through_generic_runtime_service_lookup(tmp_path):
    adapter = FileAdapter(tmp_path / "adapter.sqlite")
    resolver = Resolver(adapter)
    services = RuntimeServicesV1(services={EFFECT_ADAPTER_SERVICE_ID: resolver})

    assert EFFECT_ADAPTER_CONTRACT_VERSION == 1
    assert resolve_effect_adapter(services, adapter.adapter_id) is adapter


def test_coordinator_queries_receipt_before_perform(tmp_path):
    adapter = FileAdapter(tmp_path / "adapter.sqlite")
    coordinator = EffectCoordinator(SQLiteEffectStore(tmp_path / "effects.sqlite"))
    input_value = {"report_id": "r-7", "sections": ["summary", "risks"]}
    record = coordinator.prepare(
        workflow_id="wf-contract-001",
        effect_key="publish-report",
        adapter_id=adapter.adapter_id,
        input_value=input_value,
        policy=EffectPolicy.IDEMPOTENT,
    )
    adapter.perform(record.identity.operation_id, input_value)
    claim = coordinator.store.claim(record.identity.operation_id)
    completed = coordinator.execute_claimed(record, claim, adapter, input_value)

    assert completed.state == "completed"
    with sqlite3.connect(adapter.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM external_effects").fetchone()[0] == 1


@pytest.mark.parametrize("source", ["lookup", "perform"])
def test_adapter_receipt_operation_id_must_match_requested_operation(tmp_path, source):
    class ConflictingFileAdapter(FileAdapter):
        def lookup_receipt(self, operation_id: str):
            if source == "lookup":
                return {"adapter_receipt_id": "wrong", "operation_id": "op_" + "0" * 64}
            return None

        def perform(self, operation_id: str, input_value):
            return {"adapter_receipt_id": "wrong", "operation_id": "op_" + "0" * 64}

    adapter = ConflictingFileAdapter(tmp_path / "adapter.sqlite")
    coordinator = EffectCoordinator(SQLiteEffectStore(tmp_path / "effects.sqlite"))
    input_value = {"report_id": "r-conflict"}
    record = coordinator.prepare(
        workflow_id="wf-contract-001",
        effect_key="publish-report",
        adapter_id=adapter.adapter_id,
        input_value=input_value,
        policy=EffectPolicy.IDEMPOTENT,
    )
    claim = coordinator.store.claim(record.identity.operation_id)

    with pytest.raises(ValueError, match="receipt operation_id mismatch"):
        coordinator.execute_claimed(record, claim, adapter, input_value)

    rejected = coordinator.store.get(record.identity.operation_id)
    assert rejected.state == "claimed"
    assert rejected.receipt is None


def _adapter_worker(args: list[str]) -> int:
    db_path = Path(args[0])
    operation_id = args[1]
    input_value = json.loads(args[2])
    receipt = FileAdapter(db_path).perform(operation_id, input_value)
    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM external_effects").fetchone()[0]
    print(json.dumps({**receipt, "external_effect_count": count}, sort_keys=True))
    return 0


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--adapter-worker":
        raise SystemExit(_adapter_worker(sys.argv[2:]))
    raise SystemExit("this module is an adapter worker only when invoked with --adapter-worker")
