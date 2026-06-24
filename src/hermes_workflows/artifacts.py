from __future__ import annotations

import hashlib
import json
import mimetypes
from dataclasses import dataclass, field
from urllib.parse import urlparse
from typing import Any, Literal

from .types import JsonObject, JsonValue, to_json_value

ArtifactRenderMode = Literal[
    "inline-json",
    "inline-text",
    "inline-markdown",
    "python-source",
    "media-reference",
    "file-reference",
    "external-link",
    "external-reference",
    "none",
]

_MEDIA_KINDS = {"image", "audio", "video"}
_ARTIFACT_PATH_KEYS = {"path", "file_path", "local_path", "absolute_path", "filesystem_path"}
_ARTIFACT_REF_KEYS = _ARTIFACT_PATH_KEYS | {"uri", "href", "url"}
_VALID_RENDER_MODES = set(ArtifactRenderMode.__args__)
_SAFE_EXTERNAL_SCHEMES = {"http", "https"}


@dataclass(frozen=True)
class ArtifactMetadata:
    title: str
    description: str | None = None
    tags: tuple[str, ...] = ()
    source: JsonObject = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        payload: dict[str, JsonValue] = {"title": self.title, "tags": list(self.tags), "source": self.source}
        if self.description is not None:
            payload["description"] = self.description
        return payload


@dataclass(frozen=True)
class ArtifactRender:
    mode: ArtifactRenderMode
    media_type: str | None = None
    reference: JsonObject = field(default_factory=dict)

    def to_json(self) -> JsonObject:
        payload: dict[str, JsonValue] = {"mode": self.mode, "render": self.mode, "reference": self.reference}
        if self.media_type is not None:
            payload["media_type"] = self.media_type
        return payload


@dataclass(frozen=True)
class Artifact:
    id: str
    kind: str
    metadata: ArtifactMetadata
    value: JsonValue
    render: ArtifactRender
    sha256: str | None = None

    def to_json(self) -> JsonObject:
        payload: dict[str, JsonValue] = {
            "__hermes_type__": "Artifact",
            "id": self.id,
            "kind": self.kind,
            "metadata": self.metadata.to_json(),
            "value": self.value,
            "render": self.render.to_json(),
        }
        if self.sha256 is not None:
            payload["sha256"] = self.sha256
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "Artifact":
        if payload.get("__hermes_type__") != "Artifact":
            raise ValueError("serialized Artifact is missing __hermes_type__")
        metadata_payload = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        render_payload = payload.get("render") if isinstance(payload.get("render"), dict) else {}
        source = to_json_value(metadata_payload.get("source") or {})
        reference = to_json_value(render_payload.get("reference") or {})
        artifact_id = str(payload.get("id") or _artifact_id(str(payload.get("kind") or "json"), str(metadata_payload.get("title") or "Artifact"), payload.get("value")))
        return cls(
            id=artifact_id,
            kind=str(payload.get("kind") or "json"),
            metadata=ArtifactMetadata(
                title=str(metadata_payload.get("title") or payload.get("title") or "Artifact"),
                description=str(metadata_payload["description"]) if metadata_payload.get("description") is not None else None,
                tags=tuple(str(tag) for tag in metadata_payload.get("tags", ()) if isinstance(metadata_payload.get("tags", ()), (list, tuple))),
                source=source if isinstance(source, dict) else {},
            ),
            value=to_json_value(payload.get("value")),
            render=ArtifactRender(
                str(render_payload.get("mode") or render_payload.get("render") or "inline-json"),  # type: ignore[arg-type]
                media_type=str(render_payload.get("media_type")) if render_payload.get("media_type") else None,
                reference=reference if isinstance(reference, dict) else {},
            ),
            sha256=str(payload.get("sha256")) if payload.get("sha256") is not None else None,
        )


def MarkdownArtifact(
    title: str,
    markdown: str,
    *,
    artifact_id: str | None = None,
    description: str | None = None,
    tags: tuple[str, ...] = (),
    source: JsonObject | None = None,
) -> Artifact:
    return _artifact(
        kind="markdown",
        title=title,
        value=markdown,
        render=ArtifactRender("inline-markdown", media_type="text/markdown"),
        artifact_id=artifact_id,
        description=description,
        tags=tags,
        source=source,
    )


def TextArtifact(
    title: str,
    text: str,
    *,
    artifact_id: str | None = None,
    description: str | None = None,
    tags: tuple[str, ...] = (),
    source: JsonObject | None = None,
) -> Artifact:
    return _artifact(
        kind="text",
        title=title,
        value=text,
        render=ArtifactRender("inline-text", media_type="text/plain"),
        artifact_id=artifact_id,
        description=description,
        tags=tags,
        source=source,
    )


def JsonArtifact(
    title: str,
    data: object,
    *,
    artifact_id: str | None = None,
    description: str | None = None,
    tags: tuple[str, ...] = (),
    source: JsonObject | None = None,
) -> Artifact:
    return _artifact(
        kind="json",
        title=title,
        value=to_json_value(data),
        render=ArtifactRender("inline-json", media_type="application/json"),
        artifact_id=artifact_id,
        description=description,
        tags=tags,
        source=source,
    )


def FileArtifact(
    title: str,
    path: str,
    *,
    media_type: str | None = None,
    artifact_id: str | None = None,
    description: str | None = None,
    tags: tuple[str, ...] = (),
    source: JsonObject | None = None,
) -> Artifact:
    guessed_type = media_type or mimetypes.guess_type(path)[0]
    return _artifact(
        kind=_artifact_kind_from_media_type(guessed_type) or "file",
        title=title,
        value={"path": path},
        render=ArtifactRender("file-reference", media_type=guessed_type, reference={"type": "local_path", "field": "path", "href": path}),
        artifact_id=artifact_id,
        description=description,
        tags=tags,
        source=source,
    )


def LinkArtifact(
    title: str,
    url: str,
    *,
    media_type: str | None = None,
    artifact_id: str | None = None,
    description: str | None = None,
    tags: tuple[str, ...] = (),
    source: JsonObject | None = None,
) -> Artifact:
    kind = _artifact_kind_from_media_type(media_type) or "link"
    parsed = urlparse(url)
    if parsed.scheme and parsed.scheme.lower() not in _SAFE_EXTERNAL_SCHEMES:
        render = ArtifactRender("file-reference", media_type=media_type, reference={"type": "local_path", "field": "url", "href": url}) if parsed.scheme.lower() == "file" else ArtifactRender("inline-json", media_type="application/json")
    elif not parsed.scheme and _looks_like_local_path(url):
        guessed_type = media_type or mimetypes.guess_type(url)[0]
        kind = _artifact_kind_from_media_type(guessed_type) or kind
        render = ArtifactRender("file-reference", media_type=guessed_type, reference={"type": "local_path", "field": "url", "href": url})
    else:
        render_mode: ArtifactRenderMode = "media-reference" if kind in _MEDIA_KINDS else "external-link"
        render = ArtifactRender(render_mode, media_type=media_type, reference={"type": "url", "href": url})
    return _artifact(
        kind=kind,
        title=title,
        value={"url": url},
        render=render,
        artifact_id=artifact_id,
        description=description,
        tags=tags,
        source=source,
    )


def PythonSourceArtifact(
    title: str,
    source: str,
    *,
    symbol: str | None = None,
    artifact_id: str | None = None,
    description: str | None = None,
    tags: tuple[str, ...] = (),
    provenance: JsonObject | None = None,
) -> Artifact:
    value: dict[str, JsonValue] = {"source": source, "source_sha256": _sha256_text(source)}
    if symbol is not None:
        value["symbol"] = symbol
    return _artifact(
        kind="python_source",
        title=title,
        value=value,
        render=ArtifactRender("python-source", media_type="text/x-python", reference={"symbol": symbol} if symbol else {}),
        artifact_id=artifact_id,
        description=description,
        tags=tags,
        source=provenance,
        sha256=str(value["source_sha256"]),
    )


def normalize_artifact(value: object, *, title: str | None = None) -> Artifact | None:
    """Normalize arbitrary persisted values into the framework artifact model.

    Legacy workflow history can contain strings or dicts. This function is the
    read-time adapter: new workflows can emit Artifact directly, while old runs
    still become Json/Text/Link/File artifacts for renderers.
    """

    if value is None:
        return None
    if isinstance(value, Artifact):
        return value
    if isinstance(value, dict) and value.get("__hermes_type__") == "Artifact":
        return Artifact.from_json(value)
    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            return LinkArtifact(title or "Link artifact", value)
        return TextArtifact(title or "Text artifact", value)
    workflow_source = workflow_source_preview(value)
    if workflow_source is not None:
        return PythonSourceArtifact(
            title or "Generated workflow source",
            str(workflow_source["source"]),
            symbol=str(workflow_source.get("symbol") or "workflow"),
            artifact_id=_artifact_id("workflow_source", title or "Generated workflow source", workflow_source),
            provenance={"workflow_name": str(workflow_source.get("workflow_name") or "")},
        )
    if isinstance(value, dict):
        descriptor = artifact_descriptor(value)
        render = str(descriptor.get("render") or "inline-json")
        kind = str(descriptor.get("kind") or "json")
        if render == "inline-markdown":
            return MarkdownArtifact(title or str(value.get("title") or "Markdown artifact"), str(value.get("markdown") or value.get("content") or ""))
        if render == "inline-text":
            return TextArtifact(title or str(value.get("title") or "Text artifact"), str(value.get("text") or value.get("content") or ""))
        if render in {"file-reference", "media-reference", "external-link", "external-reference"}:
            ref = descriptor.get("reference")
            if isinstance(ref, dict) and isinstance(ref.get("href"), str):
                href = ref["href"]
                if render == "file-reference":
                    return FileArtifact(title or str(value.get("title") or "File artifact"), href, media_type=descriptor.get("media_type") if isinstance(descriptor.get("media_type"), str) else None)
                return LinkArtifact(title or str(value.get("title") or "Link artifact"), href, media_type=descriptor.get("media_type") if isinstance(descriptor.get("media_type"), str) else None)
        return JsonArtifact(title or str(value.get("title") or f"{kind.title()} artifact"), value)
    return JsonArtifact(title or "JSON artifact", to_json_value(value))


def artifact_descriptor(artifact: object) -> JsonObject:
    """Return the low-risk rendering seam for approval/run artifacts."""

    descriptor: dict[str, JsonValue] = {
        "kind": "json",
        "render": "inline-json",
        "persisted": "workflow_history",
        "servable_by_dashboard": False,
    }
    if artifact is None:
        return {**descriptor, "kind": "none", "render": "none"}
    if isinstance(artifact, Artifact):
        result = _descriptor_from_render(artifact.kind, artifact.render)
        if artifact.sha256 is not None:
            result["sha256"] = artifact.sha256
        return result
    if isinstance(artifact, dict) and artifact.get("__hermes_type__") == "Artifact":
        try:
            restored = Artifact.from_json(artifact)
        except (TypeError, ValueError):
            return {**descriptor, "warning": "Invalid serialized Artifact shape; falling back to raw JSON rendering."}
        result = _descriptor_from_render(restored.kind, restored.render)
        if restored.sha256 is not None:
            result["sha256"] = restored.sha256
        return result
    workflow_source = workflow_source_preview(artifact)
    if workflow_source is not None:
        return {
            **descriptor,
            "kind": "workflow_source",
            "render": "python-source",
            "language": "python",
            "highlight_class": "language-python",
            "source_hash": workflow_source["source_sha256"],
            "symbol": workflow_source["symbol"],
            "hash_verified": workflow_source["source_hash_verified"],
        }
    if isinstance(artifact, str):
        if artifact.startswith(("http://", "https://")):
            return {**descriptor, "kind": "link", "render": "external-link", "reference": {"type": "url", "href": artifact}}
        return {**descriptor, "kind": "text", "render": "inline-text"}
    if isinstance(artifact, dict):
        media_type = artifact.get("media_type") or artifact.get("mime_type") or artifact.get("content_type")
        media_type = str(media_type) if media_type else None
        explicit_kind = str(artifact.get("kind") or artifact.get("type") or "").lower()
        kind = _artifact_kind_from_media_type(media_type) or (explicit_kind if explicit_kind in {"text", "json", "markdown", "image", "audio", "video", "file", "link"} else "json")
        ref = None
        ref_key = None
        for key in ("url", "uri", "href", "path", "file_path", "local_path"):
            raw = artifact.get(key)
            if isinstance(raw, str) and raw.strip():
                ref = raw.strip()
                ref_key = key
                break
        if ref and _is_safe_external_url(ref):
            render = "external-link" if kind == "link" else "media-reference" if kind in _MEDIA_KINDS else "external-reference"
            return {**descriptor, "kind": kind, "render": render, "media_type": media_type, "reference": {"type": "url", "href": ref}}
        if ref:
            guessed_type = media_type or mimetypes.guess_type(ref)[0]
            guessed_kind = _artifact_kind_from_media_type(guessed_type) or kind
            return {
                **descriptor,
                "kind": guessed_kind,
                "render": "file-reference",
                "media_type": guessed_type,
                "reference": {"type": "local_path", "field": ref_key, "href": ref},
                "warning": "Local/private files are not served by the dashboard; attach or expose them through an explicit artifact store before rendering media inline.",
            }
        if kind == "markdown" or "markdown" in artifact:
            return {**descriptor, "kind": "markdown", "render": "inline-markdown", "media_type": media_type}
        if kind == "text" or "text" in artifact:
            return {**descriptor, "kind": "text", "render": "inline-text", "media_type": media_type}
        return {**descriptor, "kind": kind, "render": "inline-json", "media_type": media_type}
    return descriptor


def workflow_source_preview(value: object) -> JsonObject | None:
    try:
        from .workflow_values import Workflow
    except Exception:  # pragma: no cover - import-cycle defense.
        Workflow = None  # type: ignore[assignment]

    if Workflow is not None and isinstance(value, Workflow):
        source = value.source
        symbol = value.symbol
        source_sha256 = value.source_sha256
        provenance = value.provenance
        module_name = value.module_name
        approval_required = value.approval_required
        approval_key = value.approval_key
    elif isinstance(value, dict) and value.get("__hermes_type__") == "Workflow" and isinstance(value.get("source"), str):
        source = value["source"]
        symbol = str(value.get("symbol") or "workflow")
        source_sha256 = str(value.get("source_sha256") or _sha256_text(source))
        provenance = value.get("provenance")
        module_name = value.get("module_name")
        approval_required = bool(value.get("approval_required", False))
        approval_key = value.get("approval_key")
    elif isinstance(value, dict) and value.get("kind") == "generated_workflow.approval.v1" and isinstance(value.get("source"), str):
        source = value["source"]
        symbol = str(value.get("symbol") or "workflow")
        source_sha256 = str(value.get("source_sha256") or _sha256_text(source))
        provenance = {
            "runner_provenance": value.get("runner_provenance"),
            "agent_request": value.get("agent_request"),
            "agent_response": value.get("agent_response"),
        }
        module_name = None
        approval_required = True
        approval_key = value.get("approval_key")
    else:
        return None
    actual_sha256 = _sha256_text(source)
    return {
        "kind": "generated_workflow_source",
        "language": "python",
        "highlight_class": "language-python",
        "source": source,
        "symbol": symbol,
        "source_sha256": source_sha256,
        "source_hash_verified": actual_sha256 == source_sha256,
        "workflow_name": str(value.get("workflow_name") or f"generated:{source_sha256}:{symbol}") if isinstance(value, dict) else f"generated:{source_sha256}:{symbol}",
        "module_name": module_name,
        "provenance": provenance,
        "approval_required": approval_required,
        "approval_key": approval_key,
    }


def _artifact(
    *,
    kind: str,
    title: str,
    value: object,
    render: ArtifactRender,
    artifact_id: str | None,
    description: str | None,
    tags: tuple[str, ...],
    source: JsonObject | None,
    sha256: str | None = None,
) -> Artifact:
    json_value = to_json_value(value)
    digest = sha256 or _sha256_json(json_value)
    return Artifact(
        id=artifact_id or _artifact_id(kind, title, json_value),
        kind=kind,
        metadata=ArtifactMetadata(title=title, description=description, tags=tags, source=source or {}),
        value=json_value,
        render=render,
        sha256=digest,
    )


def _descriptor_from_render(kind: str, render: ArtifactRender) -> JsonObject:
    mode = _safe_render_mode(str(render.mode))
    descriptor: dict[str, JsonValue] = {
        "kind": kind,
        "render": mode,
        "persisted": "workflow_history",
        "servable_by_dashboard": False,
    }
    if render.media_type is not None:
        descriptor["media_type"] = render.media_type
    if mode != str(render.mode):
        descriptor["warning"] = f"Unsupported artifact render mode {render.mode!r}; falling back to inline JSON."
        return descriptor
    reference = _safe_reference(mode, render.reference)
    if reference:
        descriptor["reference"] = reference
    if render.reference and mode in {"external-link", "external-reference", "media-reference"} and not reference:
        descriptor["warning"] = "Unsafe artifact reference scheme; falling back to raw JSON rendering."
        descriptor["render"] = "inline-json"
        descriptor.pop("reference", None)
        return descriptor
    if mode == "file-reference":
        descriptor["warning"] = "Local/private files are not served by the dashboard; attach or expose them through an explicit artifact store before rendering media inline."
    if mode == "python-source":
        descriptor.setdefault("language", "python")
        descriptor.setdefault("highlight_class", "language-python")
        symbol = render.reference.get("symbol") if render.reference else None
        if symbol is not None:
            descriptor["symbol"] = symbol
    return descriptor


def _safe_render_mode(mode: str) -> ArtifactRenderMode:
    return mode if mode in _VALID_RENDER_MODES else "inline-json"  # type: ignore[return-value]


def _safe_reference(mode: str, reference: JsonObject) -> JsonObject:
    href = reference.get("href") if isinstance(reference, dict) else None
    if not isinstance(href, str) or not href.strip():
        return {}
    href = href.strip()
    if mode in {"external-link", "external-reference", "media-reference"}:
        if _is_safe_external_url(href):
            return {"type": "url", "href": href}
        return {}
    if mode == "file-reference":
        field = reference.get("field") if isinstance(reference.get("field"), str) else None
        return {"type": "local_path", "field": field or "path", "href": href}
    return {}


def _is_safe_external_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme.lower() in _SAFE_EXTERNAL_SCHEMES and bool(parsed.netloc)


def _looks_like_local_path(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme == "file":
        return True
    if parsed.scheme:
        return False
    return value.startswith(("/", "./", "../", "~")) or "\\" in value


def _artifact_kind_from_media_type(media_type: str | None) -> str | None:
    if not media_type:
        return None
    major = media_type.split("/", 1)[0].lower()
    if major in _MEDIA_KINDS:
        return major
    if media_type in {"text/markdown", "application/markdown"}:
        return "markdown"
    if media_type.startswith("text/"):
        return "text"
    if media_type == "application/json":
        return "json"
    return None


def _artifact_id(kind: str, title: str, value: object) -> str:
    safe_title = "-".join("".join(ch.lower() if ch.isalnum() else " " for ch in title).split()) or "artifact"
    digest = _sha256_json({"kind": kind, "title": title, "value": value})[:12]
    return f"{kind}:{safe_title[:48]}:{digest}"


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_json(value: object) -> str:
    return hashlib.sha256(json.dumps(to_json_value(value), sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
