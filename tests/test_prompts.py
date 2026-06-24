from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from hermes_workflows import prompt_file
from hermes_workflows.prompts import render_prompt


def test_prompt_file_renders_template_and_preserves_hashes(tmp_path: Path) -> None:
    template = tmp_path / "plan.md"
    template.write_text("# Plan for {{ goal }}\n\n{{ details }}\n")

    rendered = prompt_file("plan.md", base_dir=tmp_path).render(goal="Artifacts", details={"tests": ["pytest -q"]})
    payload = rendered.to_json()

    assert rendered.rendered_prompt == '# Plan for Artifacts\n\n{\n  "tests": [\n    "pytest -q"\n  ]\n}\n'
    assert payload["kind"] == "prompt.rendered.v1"
    assert payload["prompt_path"] == str(template.resolve())
    assert payload["template_path"] == str(template.resolve())
    assert payload["prompt_sha256"] == hashlib.sha256(template.read_text().encode("utf-8")).hexdigest()
    assert payload["template_sha256"] == payload["prompt_sha256"]
    assert payload["variables_sha256"] == hashlib.sha256(
        json.dumps({"details": {"tests": ["pytest -q"]}, "goal": "Artifacts"}, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    assert payload["rendered_prompt_sha256"] == hashlib.sha256(rendered.rendered_prompt.encode("utf-8")).hexdigest()
    assert payload["rendered_prompt"] == rendered.rendered_prompt


def test_prompt_file_can_suppress_rendered_text_in_json(tmp_path: Path) -> None:
    template = tmp_path / "private.md"
    template.write_text("Secret-ish {{ value }}")

    payload = prompt_file(template).render(value="payload", include_rendered_text=False).to_json()

    assert payload["prompt_path"] == str(template.resolve())
    assert payload["rendered_prompt_sha256"]
    assert "prompt_text" not in payload
    assert "template_text" not in payload
    assert "rendered_prompt" not in payload


def test_render_prompt_reports_missing_variables() -> None:
    with pytest.raises(KeyError, match="missing prompt variables: topic"):
        render_prompt("Write about {{ topic }}", {})
