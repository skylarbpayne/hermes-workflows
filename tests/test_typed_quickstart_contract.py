from __future__ import annotations

import ast
import importlib.util
import inspect
import json
import py_compile
import subprocess
import sys
from dataclasses import fields
from pathlib import Path
from typing import get_type_hints

from hermes_workflows import WorkflowEngine
from hermes_workflows.runner_api import run_result_payload
from hermes_workflows.status_projection import JsonCodec


REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SNIPPET = REPO_ROOT / "src" / "hermes_workflows" / "examples" / "install_smoke.py"
DOC_SNIPPET = REPO_ROOT / "docs" / "snippets" / "typed_quickstart.py"
INPUT_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "typed_quickstart_input.json"
WORKFLOW_ID = "wf_typed_quickstart"
STARTED_JSON = '{"error":null,"result":null,"status":"running","waiting_on":null,"workflow_id":"wf_typed_quickstart"}'
WAITING_JSON = '{"error":null,"result":null,"status":"waiting","waiting_on":"signal:operator.response:review_release_note","workflow_id":"wf_typed_quickstart"}'
RESULT_JSON = (
    '{"error":null,"result":{"decision":{"action":"approve","feedback":"Ready to ship."},'
    '"draft":{"text":"Release note: Expose typed workflow contracts."},'
    '"side_effects":{"published":false}},"status":"completed","waiting_on":null,'
    '"workflow_id":"wf_typed_quickstart"}'
)


def _load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _input_payload() -> dict[str, str]:
    return json.loads(INPUT_FIXTURE.read_text())


def test_canonical_snippet_compiles_imports_without_side_effects_and_matches_packaged_example(tmp_path, monkeypatch):
    assert PACKAGE_SNIPPET.is_file()
    assert DOC_SNIPPET.is_file()
    assert PACKAGE_SNIPPET.read_text() == DOC_SNIPPET.read_text()

    monkeypatch.chdir(tmp_path)
    before = set(tmp_path.iterdir())
    for index, path in enumerate((PACKAGE_SNIPPET, DOC_SNIPPET)):
        py_compile.compile(str(path), cfile=str(tmp_path / f"snippet-{index}.pyc"), doraise=True)
        _load_module(path, f"typed_quickstart_{index}")
    assert set(tmp_path.iterdir()) - before == {tmp_path / "snippet-0.pyc", tmp_path / "snippet-1.pyc"}


def test_quickstart_declares_typed_input_result_agent_and_review_contracts():
    module = _load_module(PACKAGE_SNIPPET, "typed_quickstart_contracts")
    workflow_hints = get_type_hints(module.release_note_workflow)

    assert module.release_note_workflow.__workflow_input_type__ is module.ReleaseNoteInput
    assert workflow_hints == {
        "inputs": module.ReleaseNoteInput,
        "return": module.ReleaseNoteResult,
    }
    assert [(field.name, get_type_hints(module.ReleaseNoteInput)[field.name]) for field in fields(module.ReleaseNoteInput)] == [
        ("change", str),
    ]
    assert [(field.name, get_type_hints(module.ReleaseNoteResult)[field.name]) for field in fields(module.ReleaseNoteResult)] == [
        ("draft", module.Draft),
        ("decision", module.ReviewDecision),
        ("side_effects", module.SideEffects),
    ]

    source = inspect.getsource(module.release_note_workflow)
    tree = ast.parse(source)
    calls = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            calls.append((node.func.id, node))
    typed_returns = {}
    for call_name, node in calls:
        if call_name not in {"agent", "ask"}:
            continue
        returns_keyword = next(keyword for keyword in node.keywords if keyword.arg == "returns")
        assert isinstance(returns_keyword.value, ast.Name)
        typed_returns[call_name] = returns_keyword.value.id
    assert typed_returns == {"agent": "Draft", "ask": "ReviewDecision"}


def test_serialized_input_coerces_and_reaches_exact_typed_wait_without_credentials(tmp_path):
    module = _load_module(PACKAGE_SNIPPET, "typed_quickstart_wait")
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")

    waiting = engine.run_until_idle(
        module.release_note_workflow,
        _input_payload(),
        workflow_id=WORKFLOW_ID,
    )

    assert JsonCodec.dumps(run_result_payload(waiting)) == WAITING_JSON
    request = engine.workflow_status(WORKFLOW_ID)["review_requests"][0]
    assert request["key"] == "review_release_note"
    assert request["request_schema"]["name"] == "ReviewDecision"
    assert request["artifact"] == {"text": "Release note: Expose typed workflow contracts."}
    assert [command for command in engine.pending_commands(WORKFLOW_ID) if command["type"] == "external_agent"] == []


def test_typed_response_completes_with_exact_result_json(tmp_path):
    module = _load_module(PACKAGE_SNIPPET, "typed_quickstart_result")
    engine = WorkflowEngine(tmp_path / "workflow.sqlite")
    engine.run_until_idle(module.release_note_workflow, _input_payload(), workflow_id=WORKFLOW_ID)

    engine.submit_operator_response(
        workflow_id=WORKFLOW_ID,
        key="review_release_note",
        payload={"action": "approve", "feedback": "Ready to ship."},
        source={"kind": "human", "id": "reviewer", "channel": "test", "message_id": "typed-quickstart-1"},
    )
    completed = engine.drain(WORKFLOW_ID)

    assert isinstance(completed.result, module.ReleaseNoteResult)
    assert JsonCodec.dumps(run_result_payload(completed)) == RESULT_JSON
    assert engine.workflow_status(WORKFLOW_ID)["result"] == {
        "draft": {"text": "Release note: Expose typed workflow contracts."},
        "decision": {"action": "approve", "feedback": "Ready to ship."},
        "side_effects": {"published": False},
    }


def test_extracted_snippet_runs_with_exact_input_and_start_json(tmp_path):
    completed = subprocess.run(
        [
            sys.executable,
            str(DOC_SNIPPET),
            "--db",
            str(tmp_path / "workflow.sqlite"),
            "--id",
            WORKFLOW_ID,
            "--input-json",
            json.dumps(_input_payload(), separators=(",", ":")),
        ],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )

    assert completed.stderr == ""
    assert completed.stdout.strip() == STARTED_JSON


def test_primary_quickstart_snippets_have_no_untyped_workflow_boundary():
    for path in (PACKAGE_SNIPPET, DOC_SNIPPET):
        tree = ast.parse(path.read_text(), filename=str(path))
        workflows = [
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(isinstance(decorator, ast.Name) and decorator.id == "workflow" for decorator in node.decorator_list)
        ]
        assert workflows
        for workflow_fn in workflows:
            assert len(workflow_fn.args.args) == 1
            assert workflow_fn.args.args[0].annotation is not None
            assert workflow_fn.returns is not None
