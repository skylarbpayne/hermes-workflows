from __future__ import annotations

import hashlib

from hermes_workflows import Artifact, AudioArtifact, CustomArtifact, DiffArtifact, FileArtifact, HtmlArtifact, ImageArtifact, JsonArtifact, LinkArtifact, MarkdownArtifact, PythonSourceArtifact, TextArtifact, VideoArtifact
from hermes_workflows.artifacts import artifact_descriptor, normalize_artifact, workflow_source_preview
from hermes_workflows.types import to_json_value
from hermes_workflows.workflow_values import Workflow


def test_artifact_constructors_create_jsonable_render_contracts() -> None:
    markdown = MarkdownArtifact(
        "Review packet",
        "# Ship it",
        tags=("review",),
        source={"event": "ApprovalRequested", "key": "review_packet"},
    )

    assert isinstance(markdown, Artifact)
    assert markdown.kind == "markdown"
    assert markdown.metadata.title == "Review packet"
    descriptor = artifact_descriptor(markdown)
    assert descriptor["kind"] == "markdown"
    assert descriptor["render"] == "inline-markdown"
    assert descriptor["persisted"] == "workflow_history"
    assert descriptor["servable_by_dashboard"] is False
    assert descriptor["media_type"] == "text/markdown"
    assert descriptor["sha256"] == markdown.sha256
    serialized = to_json_value(markdown)
    assert serialized["__hermes_type__"] == "Artifact"
    assert serialized["metadata"]["title"] == "Review packet"
    assert serialized["render"]["render"] == "inline-markdown"


def test_artifact_descriptors_cover_common_inline_and_reference_shapes() -> None:
    assert artifact_descriptor(TextArtifact("Plain text", "hello"))["render"] == "inline-text"
    assert artifact_descriptor(JsonArtifact("Payload", {"ok": True}))["render"] == "inline-json"
    assert artifact_descriptor(HtmlArtifact("Page", "<h1>Hello</h1>"))["render"] == "inline-html"
    assert artifact_descriptor(DiffArtifact("Patch", "@@ -1 +1 @@\n-old\n+new"))["render"] == "inline-diff"
    image_descriptor = artifact_descriptor(ImageArtifact("Chart", "https://example.invalid/chart.png"))
    assert image_descriptor["kind"] == "image"
    assert image_descriptor["render"] == "media-reference"
    assert image_descriptor["reference"] == {"type": "url", "href": "https://example.invalid/chart.png"}
    assert artifact_descriptor(AudioArtifact("Audio", "https://example.invalid/review.mp3"))["render"] == "media-reference"
    assert artifact_descriptor(VideoArtifact("Video", "https://example.invalid/demo.mp4"))["render"] == "media-reference"
    custom_descriptor = artifact_descriptor(CustomArtifact("Chart", "chart", {"points": [1, 2]}, render="custom-render", renderer="acme.chart.v1"))
    assert custom_descriptor["kind"] == "chart"
    assert custom_descriptor["render"] == "custom-render"
    assert custom_descriptor["reference"] == {"type": "custom_renderer", "renderer": "acme.chart.v1"}
    link_descriptor = artifact_descriptor(LinkArtifact("Reference", "https://example.invalid/report"))
    assert link_descriptor["kind"] == "link"
    assert link_descriptor["render"] == "external-link"
    assert link_descriptor["persisted"] == "workflow_history"
    assert link_descriptor["servable_by_dashboard"] is False
    assert link_descriptor["reference"] == {"type": "url", "href": "https://example.invalid/report"}
    assert "sha256" in link_descriptor
    file_descriptor = artifact_descriptor(FileArtifact("Image", "/tmp/chart.png"))
    assert file_descriptor["kind"] == "image"
    assert file_descriptor["render"] == "file-reference"
    assert file_descriptor["media_type"] == "image/png"
    assert file_descriptor["reference"] == {"type": "local_path", "field": "path", "href": "/tmp/chart.png"}
    assert "not served by the dashboard" in file_descriptor["warning"]


def test_legacy_artifact_dicts_still_normalize_to_render_descriptors() -> None:
    legacy_markdown = {"kind": "markdown", "markdown": "# Existing packet"}
    assert artifact_descriptor(legacy_markdown)["render"] == "inline-markdown"
    assert artifact_descriptor({"kind": "html", "html": "<strong>Hi</strong>"})["render"] == "inline-html"
    assert artifact_descriptor({"kind": "diff", "diff": "-old\n+new"})["render"] == "inline-diff"
    custom = artifact_descriptor({"kind": "chart", "renderer": "acme.chart.v1", "data": [1, 2]})
    assert custom["render"] == "custom-render"
    assert custom["reference"] == {"type": "custom_renderer", "renderer": "acme.chart.v1"}
    normalized_custom = normalize_artifact({"kind": "chart", "renderer": "acme.chart.v1", "data": [1, 2]})
    assert normalized_custom is not None
    assert artifact_descriptor(normalized_custom)["reference"] == {"type": "custom_renderer", "renderer": "acme.chart.v1"}

    local_media = {"kind": "image", "media_type": "image/png", "uri": "/Users/operator/private/generated.png"}
    assert artifact_descriptor(local_media) == {
        "kind": "image",
        "render": "file-reference",
        "persisted": "workflow_history",
        "servable_by_dashboard": False,
        "media_type": "image/png",
        "reference": {"type": "local_path", "field": "uri", "href": "/Users/operator/private/generated.png"},
        "warning": "Local/private files are not served by the dashboard; attach or expose them through an explicit artifact store before rendering media inline.",
    }
    normalized = normalize_artifact(legacy_markdown, title="Existing packet")
    assert normalized is not None
    assert normalized.kind == "markdown"
    assert artifact_descriptor(normalized)["render"] == "inline-markdown"


def test_generated_workflow_values_render_as_python_source_artifacts() -> None:
    source = "from hermes_workflows import workflow\n\n@workflow\nasync def generated(inputs):\n    return inputs\n"
    source_hash = hashlib.sha256(source.encode("utf-8")).hexdigest()
    workflow = Workflow(
        source=source,
        symbol="generated",
        source_sha256=source_hash,
        path="/tmp/generated.py",
        module_name="generated_mod",
        provenance={"agent": "builder"},
        approval_required=True,
        approval_key="approve_generated",
    )

    preview = workflow_source_preview(workflow)
    descriptor = artifact_descriptor(workflow)

    assert preview is not None
    assert preview["source"] == source
    assert preview["symbol"] == "generated"
    assert preview["source_hash_verified"] is True
    assert descriptor["kind"] == "workflow_source"
    assert descriptor["render"] == "python-source"
    assert descriptor["language"] == "python"
    assert descriptor["source_hash"] == source_hash

    explicit = PythonSourceArtifact("Generated", source, symbol="generated")
    explicit_descriptor = artifact_descriptor(explicit)
    assert explicit_descriptor["render"] == "python-source"
    assert explicit_descriptor["symbol"] == "generated"


def test_serialized_artifacts_round_trip_without_losing_content_or_receipts() -> None:
    artifact = MarkdownArtifact(
        "Review packet",
        "# Ship it",
        artifact_id="artifact:review",
        description="Human review text",
        tags=("review", "markdown"),
        source={"event": "ApprovalRequested"},
    )
    serialized = to_json_value(artifact)

    restored = normalize_artifact(serialized)
    descriptor = artifact_descriptor(serialized)

    assert restored == artifact
    assert descriptor["kind"] == "markdown"
    assert descriptor["render"] == "inline-markdown"
    assert descriptor["sha256"] == artifact.sha256


def test_serialized_artifact_render_instructions_are_validated() -> None:
    unsafe_link = {
        "__hermes_type__": "Artifact",
        "id": "artifact:unsafe",
        "kind": "link",
        "metadata": {"title": "Unsafe"},
        "value": {"url": "javascript:alert(1)"},
        "render": {"mode": "external-link", "reference": {"type": "url", "href": "javascript:alert(1)"}},
        "sha256": "abc123",
    }
    unknown_mode = {
        **unsafe_link,
        "render": {"mode": "inline-evil", "media_type": "text/html"},
    }
    html_mode = {
        **unsafe_link,
        "kind": "html",
        "render": {"mode": "inline-html", "media_type": "text/html"},
    }

    unsafe_descriptor = artifact_descriptor(unsafe_link)
    unknown_descriptor = artifact_descriptor(unknown_mode)
    html_descriptor = artifact_descriptor(html_mode)

    assert unsafe_descriptor["render"] == "inline-json"
    assert "reference" not in unsafe_descriptor
    assert "Unsafe artifact reference" in unsafe_descriptor["warning"]
    assert unsafe_descriptor["sha256"] == "abc123"
    assert unknown_descriptor["render"] == "inline-json"
    assert "Unsupported artifact render mode" in unknown_descriptor["warning"]
    assert html_descriptor["render"] == "inline-html"


def test_link_artifact_does_not_advertise_unsafe_external_schemes() -> None:
    descriptor = artifact_descriptor(LinkArtifact("Bad link", "javascript:alert(1)"))

    assert descriptor["kind"] == "link"
    assert descriptor["render"] == "inline-json"
    assert "reference" not in descriptor
