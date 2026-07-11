from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest


class _RepeatedItemsMapping(Mapping[str, Any]):
    """Adversarial Mapping whose item stream is not a JSON object."""

    def __getitem__(self, key: str) -> Any:
        if key == "value":
            return 2
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield "value"

    def __len__(self) -> int:
        return 1

    def items(self) -> Any:
        return (("value", 1), ("value", 2))


class _ConflictingReceiptAdapter:
    adapter_id = "test.file.v1"

    def __init__(self, source: str):
        self.source = source

    def lookup_receipt(self, operation_id: str):
        if self.source == "lookup":
            return {"operation_id": "op_" + "0" * 64, "adapter_receipt_id": "wrong"}
        return None

    def perform(self, operation_id: str, input_value: Any):
        return {"operation_id": "op_" + "0" * 64, "adapter_receipt_id": "wrong"}


def test_stable_operation_id_is_attempt_independent_and_input_sensitive():
    from hermes_workflows.effects import operation_identity

    first = operation_identity(
        workflow_id="wf-123",
        effect_key="publish-report",
        adapter_id="test.file.v1",
        input_value={"b": [2, 3], "a": 1},
        attempt=1,
    )
    replay = operation_identity(
        workflow_id="wf-123",
        effect_key="publish-report",
        adapter_id="test.file.v1",
        input_value={"a": 1, "b": [2, 3]},
        attempt=99,
    )
    changed = operation_identity(
        workflow_id="wf-123",
        effect_key="publish-report",
        adapter_id="test.file.v1",
        input_value={"a": 2, "b": [2, 3]},
        attempt=1,
    )

    assert first.operation_id == replay.operation_id
    assert first.input_hash == replay.input_hash
    assert first.operation_id != changed.operation_id
    assert first.input_hash != changed.input_hash
    assert first.operation_id.startswith("op_")
    assert len(first.input_hash) == 64


def test_noncanonical_or_secret_bearing_inputs_are_rejected():
    from hermes_workflows.effects import operation_identity

    with pytest.raises(TypeError, match="JSON"):
        operation_identity(
            workflow_id="wf-123",
            effect_key="publish-report",
            adapter_id="test.file.v1",
            input_value={"not-json": object()},
        )
    with pytest.raises(ValueError, match="finite"):
        operation_identity(
            workflow_id="wf-123",
            effect_key="publish-report",
            adapter_id="test.file.v1",
            input_value={"bad": float("nan")},
        )


def test_custom_mapping_with_repeated_items_cannot_collapse_into_json_object():
    from hermes_workflows.effects import operation_identity

    ordinary = operation_identity(
        workflow_id="wf-123",
        effect_key="publish-report",
        adapter_id="test.file.v1",
        input_value={"value": 2},
    )

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        operation_identity(
            workflow_id="wf-123",
            effect_key="publish-report",
            adapter_id="test.file.v1",
            input_value=_RepeatedItemsMapping(),
        )

    assert ordinary.operation_id.startswith("op_")


def test_sqlite_intent_claim_completion_and_stale_token_fencing(tmp_path):
    from hermes_workflows.effects import EffectPolicy, SQLiteEffectStore, operation_identity

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    identity = operation_identity(
        workflow_id="wf-cas",
        effect_key="send",
        adapter_id="test.file.v1",
        input_value={"message": "hello"},
    )
    intent = store.ensure_intent(identity, EffectPolicy.IDEMPOTENT, {"message": "hello"})
    assert intent.state == "pending"

    claim_a = store.claim(identity.operation_id, now=10.0, ttl_seconds=1.0, token="token-a")
    assert claim_a.attempt == 1
    with pytest.raises(RuntimeError, match="not claimable"):
        store.claim(identity.operation_id, now=10.5, ttl_seconds=1.0, token="token-b")

    claim_b = store.claim(identity.operation_id, now=11.1, ttl_seconds=5.0, token="token-b")
    assert claim_b.attempt == 2
    with pytest.raises(RuntimeError, match="stale claim token"):
        store.complete(identity.operation_id, "token-a", {"provider_id": "wrong"}, now=12.0)

    completed = store.complete(
        identity.operation_id,
        "token-b",
        {"provider_id": "receipt-1", "access_token": "must-not-project"},
        sensitive=True,
        now=12.0,
    )
    assert completed.state == "completed"
    assert completed.receipt is not None
    assert completed.receipt.payload["provider_id"] == "receipt-1"
    assert completed.receipt.descriptor()["sensitive"] is True
    assert "access_token" not in json.dumps(completed.receipt.descriptor())

    with sqlite3.connect(tmp_path / "effects.sqlite") as conn:
        row = conn.execute(
            "SELECT state, attempts, claim_token FROM effect_intents WHERE operation_id = ?",
            (identity.operation_id,),
        ).fetchone()
        receipt_row = conn.execute(
            "SELECT claim_token, receipt_hash FROM effect_receipts WHERE operation_id = ?",
            (identity.operation_id,),
        ).fetchone()
    assert row == ("completed", 2, "token-b")
    assert receipt_row is not None
    assert receipt_row[0] == "token-b"
    assert len(receipt_row[1]) == 64


def test_failure_is_fenced_and_terminal(tmp_path):
    from hermes_workflows.effects import EffectPolicy, SQLiteEffectStore, operation_identity

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    identity = operation_identity(
        workflow_id="wf-fail",
        effect_key="write",
        adapter_id="test.file.v1",
        input_value={"value": 1},
    )
    store.ensure_intent(identity, EffectPolicy.PURE, {"value": 1})
    claim = store.claim(identity.operation_id, token="winner")
    with pytest.raises(RuntimeError, match="stale claim token"):
        store.fail(identity.operation_id, "loser", {"kind": "wrong-owner"})
    failed = store.fail(identity.operation_id, claim.token, {"kind": "adapter-error"})
    assert failed.state == "failed"
    assert failed.error == {"kind": "adapter-error"}
    with pytest.raises(RuntimeError, match="not claimable"):
        store.claim(identity.operation_id)


def test_completion_rejects_repeated_mapping_items_without_terminal_state(tmp_path):
    from hermes_workflows.effects import EffectPolicy, SQLiteEffectStore, operation_identity

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    identity = operation_identity(
        workflow_id="wf-receipt-shape",
        effect_key="write",
        adapter_id="test.file.v1",
        input_value={"value": 1},
    )
    store.ensure_intent(identity, EffectPolicy.IDEMPOTENT, {"value": 1})
    claim = store.claim(identity.operation_id, token="receipt-owner")

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        store.complete(identity.operation_id, claim.token, _RepeatedItemsMapping())

    rejected = store.get(identity.operation_id)
    assert rejected.state == "claimed"
    assert rejected.receipt is None
    with sqlite3.connect(tmp_path / "effects.sqlite") as conn:
        receipt_count = conn.execute(
            "SELECT COUNT(*) FROM effect_receipts WHERE operation_id = ?",
            (identity.operation_id,),
        ).fetchone()[0]
    assert receipt_count == 0


def test_failure_rejects_repeated_mapping_items_without_terminal_state(tmp_path):
    from hermes_workflows.effects import EffectPolicy, SQLiteEffectStore, operation_identity

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    identity = operation_identity(
        workflow_id="wf-error-shape",
        effect_key="write",
        adapter_id="test.file.v1",
        input_value={"value": 1},
    )
    store.ensure_intent(identity, EffectPolicy.PURE, {"value": 1})
    claim = store.claim(identity.operation_id, token="error-owner")

    with pytest.raises(ValueError, match="duplicate JSON object key"):
        store.fail(identity.operation_id, claim.token, _RepeatedItemsMapping())

    rejected = store.get(identity.operation_id)
    assert rejected.state == "claimed"
    assert rejected.error is None
    with sqlite3.connect(tmp_path / "effects.sqlite") as conn:
        state, error_json = conn.execute(
            "SELECT state, error_json FROM effect_intents WHERE operation_id = ?",
            (identity.operation_id,),
        ).fetchone()
    assert (state, error_json) == ("claimed", None)


def test_expired_claim_cannot_complete_without_reclaim(tmp_path):
    from hermes_workflows.effects import EffectPolicy, SQLiteEffectStore, operation_identity

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    identity = operation_identity(
        workflow_id="wf-expired",
        effect_key="write",
        adapter_id="test.file.v1",
        input_value={"value": 1},
    )
    store.ensure_intent(identity, EffectPolicy.IDEMPOTENT, {"value": 1}, now=10.0)
    claim = store.claim(identity.operation_id, token="expired", now=10.0, ttl_seconds=1.0)

    with pytest.raises(RuntimeError, match="stale claim token"):
        store.complete(identity.operation_id, claim.token, {"receipt": 1}, now=11.0)

    replacement = store.claim(identity.operation_id, token="current", now=11.0)
    completed = store.complete(
        identity.operation_id, replacement.token, {"receipt": 1}, now=12.0
    )
    assert completed.state == "completed"


@pytest.mark.parametrize("source", ["lookup", "perform"])
def test_coordinator_rejects_conflicting_adapter_receipt_operation_id(tmp_path, source):
    from hermes_workflows.effects import EffectCoordinator, EffectPolicy, SQLiteEffectStore

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    coordinator = EffectCoordinator(store)
    input_value = {"value": 1}
    record = coordinator.prepare(
        workflow_id="wf-receipt-conflict",
        effect_key="write",
        adapter_id="test.file.v1",
        input_value=input_value,
        policy=EffectPolicy.IDEMPOTENT,
    )
    claim = store.claim(record.identity.operation_id, token=f"{source}-claim")

    with pytest.raises(ValueError, match="receipt operation_id mismatch"):
        coordinator.execute_claimed(
            record, claim, _ConflictingReceiptAdapter(source), input_value
        )

    rejected = store.get(record.identity.operation_id)
    assert rejected.state == "claimed"
    assert rejected.receipt is None


def test_coordinator_rejects_claim_for_another_operation_before_adapter_call(tmp_path):
    from hermes_workflows.effects import EffectCoordinator, EffectPolicy, SQLiteEffectStore

    class RecordingAdapter:
        adapter_id = "test.file.v1"

        def __init__(self):
            self.calls = []

        def lookup_receipt(self, operation_id: str):
            self.calls.append(("lookup", operation_id))
            return None

        def perform(self, operation_id: str, input_value: Any):
            self.calls.append(("perform", operation_id))
            return {"operation_id": operation_id, "adapter_receipt_id": "unexpected"}

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    coordinator = EffectCoordinator(store)
    input_value = {"value": 1}
    record = coordinator.prepare(
        workflow_id="wf-claim-conflict",
        effect_key="write",
        adapter_id="test.file.v1",
        input_value=input_value,
        policy=EffectPolicy.IDEMPOTENT,
    )
    valid_claim = store.claim(record.identity.operation_id, token="valid-token")
    conflicting_claim = replace(valid_claim, operation_id="op_" + "0" * 64)
    adapter = RecordingAdapter()

    with pytest.raises(ValueError, match="claim operation_id mismatch"):
        coordinator.execute_claimed(record, conflicting_claim, adapter, input_value)

    assert adapter.calls == []
    rejected = store.get(record.identity.operation_id)
    assert rejected.state == "claimed"
    assert rejected.receipt is None


def test_concurrent_claim_race_has_one_winner(tmp_path):
    from hermes_workflows.effects import EffectPolicy, SQLiteEffectStore, operation_identity

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    identity = operation_identity(
        workflow_id="wf-race",
        effect_key="write",
        adapter_id="test.file.v1",
        input_value={"value": 1},
    )
    store.ensure_intent(identity, EffectPolicy.IDEMPOTENT, {"value": 1}, now=10.0)
    barrier = threading.Barrier(2)

    def compete(token):
        barrier.wait()
        try:
            return store.claim(identity.operation_id, token=token, now=10.0)
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(compete, ("racer-a", "racer-b")))

    winners = [claim for claim in claims if claim is not None]
    assert len(winners) == 1
    record = store.get(identity.operation_id)
    assert record.state == "claimed"
    assert record.attempts == 1
    assert record.claim_token == winners[0].token


def test_intent_identity_conflicts_fail_closed(tmp_path):
    from hermes_workflows.effects import EffectPolicy, SQLiteEffectStore, operation_identity

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    identity = operation_identity(
        workflow_id="wf-conflict",
        effect_key="write",
        adapter_id="test.file.v1",
        input_value={"value": 1},
    )
    store.ensure_intent(identity, EffectPolicy.IDEMPOTENT, {"value": 1})
    with pytest.raises(ValueError, match="conflicts with durable intent"):
        store.ensure_intent(identity, EffectPolicy.PURE, {"value": 1})


def test_intent_rejects_forged_hash_shaped_operation_id_before_persistence(tmp_path):
    from hermes_workflows.effects import EffectPolicy, SQLiteEffectStore, operation_identity

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    identity = operation_identity(
        workflow_id="wf-forged",
        effect_key="write",
        adapter_id="test.file.v1",
        input_value={"value": 1},
    )
    forged = replace(identity, operation_id="op_" + "0" * 64)

    with pytest.raises(ValueError, match="operation identity mismatch"):
        store.ensure_intent(forged, EffectPolicy.IDEMPOTENT, {"value": 1})

    with sqlite3.connect(tmp_path / "effects.sqlite") as conn:
        count = conn.execute("SELECT COUNT(*) FROM effect_intents").fetchone()[0]
    assert count == 0


def test_unclassified_and_legacy_effects_refuse_execution(tmp_path):
    from hermes_workflows.effects import (
        EffectCoordinator,
        EffectPolicy,
        SQLiteEffectStore,
        operation_identity,
    )

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    coordinator = EffectCoordinator(store)
    with pytest.raises(ValueError, match="unclassified"):
        coordinator.prepare(
            workflow_id="wf-legacy",
            effect_key="legacy-call",
            adapter_id="legacy.adapter.v0",
            input_value={"value": 1},
            policy=EffectPolicy.UNCLASSIFIED,
        )
    with pytest.raises(ValueError, match="unsafe"):
        coordinator.prepare(
            workflow_id="wf-unsafe",
            effect_key="charge-card",
            adapter_id="payments.v1",
            input_value={"amount": 100},
            policy=EffectPolicy.UNSAFE,
        )

    legacy_identity = operation_identity(
        workflow_id="wf-legacy-row",
        effect_key="old-step",
        adapter_id="legacy.adapter.v0",
        input_value={"value": 1},
    )
    store.ensure_intent(legacy_identity, EffectPolicy.UNCLASSIFIED, {"value": 1})
    with pytest.raises(RuntimeError, match="not claimable"):
        store.claim(legacy_identity.operation_id)


def test_unsafe_effect_can_be_claimed_once_but_not_reclaimed_automatically(tmp_path):
    from hermes_workflows.effects import EffectCoordinator, EffectPolicy, SQLiteEffectStore

    store = SQLiteEffectStore(tmp_path / "effects.sqlite")
    record = EffectCoordinator(store).prepare(
        workflow_id="wf-unsafe-once",
        effect_key="charge-card",
        adapter_id="payments.v1",
        input_value={"amount": 100},
        policy=EffectPolicy.UNSAFE,
        allow_unsafe=True,
    )
    store.claim(record.identity.operation_id, token="only-attempt", now=10.0, ttl_seconds=1.0)

    with pytest.raises(RuntimeError, match="not claimable"):
        store.claim(record.identity.operation_id, token="forbidden-retry", now=11.0)
