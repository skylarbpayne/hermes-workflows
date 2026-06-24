import json

import pytest

from hermes_workflows.registry import WorkflowRegistry, looks_like_path


def test_registry_resolves_configured_workflow_and_env_db_aliases(tmp_path, monkeypatch):
    default_db = tmp_path / "default.sqlite"
    launch_db = tmp_path / "launch.sqlite"
    monkeypatch.setenv("HERMES_WORKFLOWS_DB", str(default_db))
    monkeypatch.setenv("HERMES_WORKFLOWS_DBS", json.dumps({"launch": str(launch_db)}))
    config = tmp_path / "workflows.registry.json"
    config.write_text(
        json.dumps(
            {
                "dbs": {"pilot": str(tmp_path / "pilot.sqlite")},
                "workflows": {
                    "trip": {
                        "workflow_ref": "hermes_workflows.examples.trip:trip_planning_workflow",
                        "db": "pilot",
                        "title": "Trip pilot",
                        "tags": ["demo", "approval"],
                        "default_input": {"reviewer": "operator"},
                        "trusted_resume": True,
                    }
                },
            }
        )
    )

    registry = WorkflowRegistry.from_sources(config_path=config)

    assert registry.resolve_db("default").path == str(default_db)
    assert registry.resolve_db("launch").path == str(launch_db)
    assert registry.resolve_db("pilot").path == str(tmp_path / "pilot.sqlite")
    trip = registry.resolve_workflow("trip")
    assert trip.name == "trip"
    assert trip.workflow_ref == "hermes_workflows.examples.trip:trip_planning_workflow"
    assert trip.db == "pilot"
    assert trip.default_input == {"reviewer": "operator"}
    assert trip.trusted_resume is True
    assert trip.tags == ("demo", "approval")


@pytest.mark.parametrize("raw", ["/tmp/workflow.sqlite", "./workflow.sqlite", "../workflow.db", "nested/workflow.sqlite3"])
def test_registry_rejects_raw_db_paths_in_gateway_token_context(raw):
    assert looks_like_path(raw)
    registry = WorkflowRegistry.from_sources(env={"HERMES_WORKFLOWS_DBS": json.dumps({"launch": "/tmp/launch.sqlite"})})

    with pytest.raises(ValueError, match="explicit DB paths are not accepted"):
        registry.resolve_db(raw, gateway_token_context=True)


def test_registry_gateway_db_requires_explicit_alias_even_with_default(tmp_path):
    registry = WorkflowRegistry.from_sources(env={"HERMES_WORKFLOWS_DB": str(tmp_path / "default.sqlite")})

    with pytest.raises(ValueError, match="gateway DB alias is required"):
        registry.resolve_gateway_db("")
    with pytest.raises(ValueError, match="gateway DB alias is required"):
        registry.resolve_gateway_db(None)


def test_registry_fails_closed_for_unknown_aliases_and_bad_workflow_refs(tmp_path):
    registry = WorkflowRegistry.from_sources(env={"HERMES_WORKFLOWS_DBS": json.dumps({"launch": str(tmp_path / "launch.sqlite")})})

    with pytest.raises(ValueError, match="Unknown workflow DB alias"):
        registry.resolve_db("missing")

    with pytest.raises(ValueError, match="Unknown workflow alias"):
        registry.resolve_workflow("missing-workflow")

    # Direct workflow refs are allowed for manual CLI use, but malformed refs are not.
    direct = registry.resolve_workflow("hermes_workflows.examples.trip:trip_planning_workflow", db="launch")
    assert direct.workflow_ref == "hermes_workflows.examples.trip:trip_planning_workflow"
    assert direct.db == "launch"
    with pytest.raises(ValueError, match="workflow ref must look like module:function"):
        registry.resolve_workflow("not-a-ref", db="launch")


def test_registry_rejects_string_trusted_resume_and_resolves_relative_paths_from_config_file(tmp_path):
    config = tmp_path / "nested" / "workflows.registry.json"
    config.parent.mkdir()
    config.write_text(
        json.dumps(
            {
                "dbs": {"pilot": "relative.sqlite"},
                "workflows": {
                    "bad": {
                        "workflow_ref": "hermes_workflows.examples.trip:trip_planning_workflow",
                        "db": "pilot",
                        "trusted_resume": "false",
                    }
                },
            }
        )
    )

    with pytest.raises(ValueError, match="trusted_resume must be a boolean"):
        WorkflowRegistry.from_sources(config_path=config)

    config.write_text(
        json.dumps(
            {
                "dbs": {"pilot": "relative.sqlite"},
                "workflows": {
                    "good": {
                        "workflow_ref": "hermes_workflows.examples.trip:trip_planning_workflow",
                        "db": "pilot",
                        "trusted_resume": False,
                    }
                },
            }
        )
    )
    registry = WorkflowRegistry.from_sources(config_path=config)
    assert registry.resolve_db("pilot").path == str(config.parent / "relative.sqlite")
