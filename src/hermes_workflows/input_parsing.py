from __future__ import annotations

import inspect
import types
from collections.abc import Mapping, Sequence
from dataclasses import MISSING, fields, is_dataclass
from typing import Any, Callable, Literal, Union, get_args, get_origin, get_type_hints


_BUILTIN_ANNOTATIONS = {
    "Any": Any,
    "object": object,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "dict": dict,
    "list": list,
    "tuple": tuple,
}

_NO_INPUT_TYPE = object()


def workflow_input_type(workflow_fn: Callable[..., Any]) -> Any:
    """Infer the author-facing input annotation for a workflow function."""

    try:
        signature = inspect.signature(workflow_fn)
    except (TypeError, ValueError):
        return _NO_INPUT_TYPE
    positional = [
        parameter
        for parameter in signature.parameters.values()
        if parameter.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if not positional:
        return _NO_INPUT_TYPE
    input_parameter = positional[0] if len(positional) == 1 else positional[1]
    annotation = input_parameter.annotation
    if annotation is inspect.Signature.empty:
        return _NO_INPUT_TYPE
    try:
        hints = get_type_hints(workflow_fn, include_extras=True)
    except Exception:
        hints = getattr(workflow_fn, "__annotations__", {}) or {}
    return hints.get(input_parameter.name, annotation)


def coerce_workflow_input(value: Any, input_type: Any) -> Any:
    """Coerce raw JSON-ish workflow input to the workflow's typed input contract."""

    if input_type is _NO_INPUT_TYPE or input_type in (Any, object):
        return value
    if isinstance(input_type, str):
        return _coerce_string_annotation(value, input_type)
    origin = get_origin(input_type)
    if origin is not None:
        return _coerce_generic(value, input_type)
    if _is_artifact_type(input_type):
        return _coerce_artifact(value)
    if is_dataclass(input_type) and isinstance(input_type, type):
        return _coerce_dataclass(value, input_type)
    if input_type in (str, int, float, bool):
        return _coerce_scalar(value, input_type)
    if input_type in (dict, list, tuple):
        if not isinstance(value, input_type):
            raise TypeError(f"expected workflow input {input_type.__name__}, got {type(value).__name__}")
        return value
    if _is_typed_dict_type(input_type):
        if not isinstance(value, Mapping):
            raise TypeError(f"expected object input for {input_type.__name__}, got {type(value).__name__}")
        return dict(value)
    if isinstance(input_type, type):
        if isinstance(value, input_type):
            return value
        return value
    return value


def _coerce_dataclass(value: Any, dataclass_type: type[Any]) -> Any:
    if isinstance(value, dataclass_type):
        return value
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise TypeError(f"expected object input for {dataclass_type.__name__}, got {type(value).__name__}")
    type_hints = _safe_type_hints(dataclass_type)
    kwargs: dict[str, Any] = {}
    for field in fields(dataclass_type):
        if field.name in value:
            kwargs[field.name] = coerce_workflow_input(value[field.name], type_hints.get(field.name, field.type))
            continue
        if field.default is not MISSING or field.default_factory is not MISSING:
            continue
        raise TypeError(f"missing required workflow input field: {field.name}")
    return dataclass_type(**kwargs)


def _safe_type_hints(target: Any) -> dict[str, Any]:
    try:
        return get_type_hints(target, include_extras=True)
    except Exception:
        return dict(getattr(target, "__annotations__", {}) or {})


def _is_artifact_type(input_type: Any) -> bool:
    try:
        from .artifacts import Artifact
    except Exception:  # pragma: no cover - import-cycle defense.
        return False
    return input_type is Artifact


def _coerce_artifact(value: Any) -> Any:
    from .artifacts import Artifact

    if isinstance(value, Artifact):
        return value
    if isinstance(value, Mapping) and value.get("__hermes_type__") == "Artifact":
        return Artifact.from_json(dict(value))
    raise TypeError(f"expected Artifact input, got {type(value).__name__}")


def _coerce_generic(value: Any, annotation: Any) -> Any:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is Literal:
        if value not in args:
            raise TypeError(f"expected one of {list(args)!r}, got {value!r}")
        return value
    if _is_union_origin(origin):
        return _coerce_union(value, args)
    if origin in (list, Sequence):
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise TypeError(f"expected list input, got {type(value).__name__}")
        item_type = args[0] if args else Any
        return [coerce_workflow_input(item, item_type) for item in value]
    if origin is tuple:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise TypeError(f"expected tuple input, got {type(value).__name__}")
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(coerce_workflow_input(item, args[0]) for item in value)
        if args and len(value) != len(args):
            raise TypeError(f"expected tuple input of length {len(args)}, got {len(value)}")
        return tuple(coerce_workflow_input(item, args[index] if index < len(args) else Any) for index, item in enumerate(value))
    if origin in (dict, Mapping):
        if not isinstance(value, Mapping):
            raise TypeError(f"expected object input, got {type(value).__name__}")
        key_type = args[0] if args else Any
        value_type = args[1] if len(args) > 1 else Any
        return {coerce_workflow_input(key, key_type): coerce_workflow_input(item, value_type) for key, item in value.items()}
    return value


def _coerce_union(value: Any, args: tuple[Any, ...]) -> Any:
    if value is None and type(None) in args:
        return None
    errors: list[str] = []
    for option in args:
        if option is type(None):
            continue
        try:
            return coerce_workflow_input(value, option)
        except Exception as exc:
            errors.append(str(exc))
    expected = " | ".join(_type_name(option) for option in args)
    detail = f": {'; '.join(errors)}" if errors else ""
    raise TypeError(f"expected workflow input {expected}, got {type(value).__name__}{detail}")


def _coerce_string_annotation(value: Any, annotation: str) -> Any:
    normalized = _strip_annotation_quotes(annotation.strip())
    if not normalized:
        return value
    union_parts = _split_top_level(normalized, "|")
    if len(union_parts) > 1:
        return _coerce_string_union(value, tuple(part.strip() for part in union_parts))
    if normalized in {"None", "NoneType"}:
        if value is None:
            return None
        raise TypeError(f"expected None input, got {type(value).__name__}")
    if normalized in _BUILTIN_ANNOTATIONS:
        return coerce_workflow_input(value, _BUILTIN_ANNOTATIONS[normalized])
    for prefix, collection_type in (("list[", list), ("dict[", dict), ("tuple[", tuple)):
        if normalized.startswith(prefix) and normalized.endswith("]"):
            inner = normalized[len(prefix) : -1]
            return _coerce_string_collection(value, collection_type, inner)
    return value


def _coerce_string_union(value: Any, parts: tuple[str, ...]) -> Any:
    if value is None and any(part in {"None", "NoneType"} for part in parts):
        return None
    errors: list[str] = []
    for part in parts:
        if part in {"None", "NoneType"}:
            continue
        try:
            return _coerce_string_annotation(value, part)
        except Exception as exc:
            errors.append(str(exc))
    detail = f": {'; '.join(errors)}" if errors else ""
    raise TypeError(f"expected workflow input {' | '.join(parts)}, got {type(value).__name__}{detail}")


def _coerce_string_collection(value: Any, collection_type: type[Any], inner: str) -> Any:
    if collection_type is list:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise TypeError(f"expected list input, got {type(value).__name__}")
        return [_coerce_string_annotation(item, inner) for item in value]
    if collection_type is tuple:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
            raise TypeError(f"expected tuple input, got {type(value).__name__}")
        item_types = tuple(part.strip() for part in _split_top_level(inner, ",") if part.strip())
        if len(item_types) == 2 and item_types[1] == "...":
            return tuple(_coerce_string_annotation(item, item_types[0]) for item in value)
        if item_types and len(value) != len(item_types):
            raise TypeError(f"expected tuple input of length {len(item_types)}, got {len(value)}")
        return tuple(_coerce_string_annotation(item, item_types[index] if index < len(item_types) else "Any") for index, item in enumerate(value))
    if not isinstance(value, Mapping):
        raise TypeError(f"expected object input, got {type(value).__name__}")
    parts = tuple(part.strip() for part in _split_top_level(inner, ",") if part.strip())
    key_type = parts[0] if parts else "Any"
    value_type = parts[1] if len(parts) > 1 else "Any"
    return {_coerce_string_annotation(key, key_type): _coerce_string_annotation(item, value_type) for key, item in value.items()}


def _strip_annotation_quotes(annotation: str) -> str:
    while len(annotation) >= 2 and annotation[0] == annotation[-1] and annotation[0] in {'"', "'"}:
        annotation = annotation[1:-1].strip()
    return annotation


def _split_top_level(value: str, delimiter: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    for index, char in enumerate(value):
        if char == "[":
            depth += 1
        elif char == "]" and depth:
            depth -= 1
        elif char == delimiter and depth == 0:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _coerce_scalar(value: Any, scalar_type: type[Any]) -> Any:
    if isinstance(value, scalar_type):
        return value
    if scalar_type is bool:
        if value in (0, 1):
            return bool(value)
        raise TypeError(f"expected bool input, got {type(value).__name__}")
    try:
        return scalar_type(value)
    except Exception as exc:
        raise TypeError(f"expected {scalar_type.__name__} input, got {type(value).__name__}") from exc


def _is_union_origin(origin: Any) -> bool:
    union_type = getattr(types, "UnionType", None)
    return origin is Union or (union_type is not None and origin is union_type)


def _is_typed_dict_type(value: Any) -> bool:
    return isinstance(value, type) and hasattr(value, "__required_keys__") and hasattr(value, "__optional_keys__")


def _type_name(value: Any) -> str:
    return getattr(value, "__name__", str(value))
