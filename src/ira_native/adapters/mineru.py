"""Strict adapter for the documented MinerU 4.2 structured-content shape."""

import json
import math
import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from html.parser import HTMLParser

from pydantic import JsonValue

from ira_native.domain.models import MatchSource
from ira_native.errors import IraError

ADAPTER_VERSION = "mineru-structured-content/3"
NORMALIZED_PAGE = "normalized_page"
_CONTEXT_FIELDS = ("level", "sub_type", "captions", "footnotes", "image_source", "structured_table")
_KNOWN_KINDS = {
    "text",
    "title",
    "doc_title",
    "paragraph_title",
    "ref_text",
    "list",
    "table",
    "image",
    "equation",
    "code",
    "caption",
    "header",
    "footer",
    "page_number",
    "footnote",
    "discarded",
}


def _unique_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    obj: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("Duplicate JSON key")
        obj[key] = value
    return obj


def _finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("Non-finite JSON number")
    return number


def _validate_native_json(value: JsonValue, depth: int = 0) -> None:
    if depth > 64:
        raise IraError("resource_limit")
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
    elif isinstance(value, list):
        for item in value:
            _validate_native_json(item, depth + 1)
    elif isinstance(value, dict):
        for key, item in value.items():
            key.encode("utf-8", errors="strict")
            _validate_native_json(item, depth + 1)


@dataclass(frozen=True)
class NativeBlock:
    pointer: str
    kind: str
    content: JsonValue
    bbox: JsonValue
    coord_system: str | None
    continues_prev: bool | None
    native_context: dict[str, JsonValue]
    risk: tuple[str, ...]


@dataclass(frozen=True)
class ParsedArtifact:
    parser_version: str
    parser_config: JsonValue
    pages: tuple[tuple[int, tuple[NativeBlock, ...]], ...]
    is_full_document: bool
    claimed_source_sha256: str | None


def _block(raw: JsonValue, pointer: str) -> NativeBlock:
    if not isinstance(raw, dict) or not isinstance(raw.get("type"), str) or "content" not in raw:
        raise IraError("parse_failed")
    kind = raw["type"]
    if not kind.strip() or len(kind) > 64 or not kind.isprintable():
        raise IraError("parse_failed")
    continues = raw.get("continues_prev")
    if "continues_prev" in raw and type(continues) is not bool:
        raise IraError("parse_failed")
    bbox = raw.get("bbox")
    coord = raw.get("coord_system", NORMALIZED_PAGE if bbox is not None else None)
    if coord is not None and (not isinstance(coord, str) or len(coord) > 64):
        raise IraError("parse_failed")
    risks = ["needs_review", "parser_content_unverified"]
    if kind not in _KNOWN_KINDS:
        risks.append("unknown_block_kind")
    if kind == "table":
        risks.append("cell_provenance_unavailable")
    if continues:
        risks.append("continuation_unverified")
    if not raw["content"]:
        risks.append("empty_content")
    context = {key: raw[key] for key in _CONTEXT_FIELDS if key in raw}
    return NativeBlock(pointer, kind, raw["content"], bbox, coord, continues, context, tuple(risks))


def _recoveries(extensions: dict) -> dict[str, dict]:
    extension = extensions.get("exp03_table_recovery")
    if (
        not isinstance(extension, dict)
        or extension.get("adapter_version") != "exp03-table-adapter/1"
    ):
        return {}
    entries = extension.get("recoveries")
    if not isinstance(entries, list):
        raise IraError("parse_failed")
    tables = {}
    for entry in entries:
        pointer = entry.get("b0_native_pointer") if isinstance(entry, dict) else None
        if not isinstance(pointer, str) or pointer in tables:
            raise IraError("parse_failed")
        tables[pointer] = entry
    return tables


def _attach_recovery(block: NativeBlock, recovery: dict) -> NativeBlock:
    if block.kind != "table":
        raise IraError("parse_failed")
    projection = recovery.get("projection")
    projection_input = (
        isinstance(projection, dict)
        and projection.get("source_content") == "experimental_projection"
    )
    context = {
        key: recovery[key]
        for key in (
            "table_key",
            "b0_native_pointer",
            "selected_parser",
            "selected_native_pointer",
            "header_state",
            "header_is_original",
            "bbox_pdf",
            "coordinate_system",
        )
        if key in recovery
    }
    context["projection_input"] = projection_input
    unconfirmed = recovery.get("header_is_original") is not True
    return replace(
        block,
        native_context=block.native_context | {"table_recovery": context},
        risk=block.risk
        + ("table_recovery_unverified",)
        + (("continuation_unverified",) if unconfirmed and not block.continues_prev else ()),
    )


def read_artifact(data: bytes, max_pages: int, max_blocks: int) -> ParsedArtifact:
    try:
        root = json.loads(
            data.decode("utf-8-sig"),
            object_pairs_hook=_unique_object,
            parse_constant=_finite_float,
            parse_float=_finite_float,
        )
        _validate_native_json(root)
    except RecursionError:
        raise IraError("resource_limit") from None
    except ValueError:
        raise IraError("parse_failed") from None

    if (
        not isinstance(root, dict)
        or not isinstance(root.get("pages"), list)
        or not isinstance(root.get("metadata"), dict)
        or type(root.get("is_full_document")) is not bool
        or not isinstance(root.get("extensions"), dict)
    ):
        raise IraError("unsupported_format")
    metadata = root["metadata"]
    producer = metadata.get("producer")
    if not isinstance(producer, dict):
        raise IraError("unsupported_format")
    version = producer.get("version")
    name = producer.get("name", "MinerU")
    if (
        not isinstance(name, str)
        or name.casefold() != "mineru"
        or not isinstance(version, str)
        or not re.fullmatch(r"4\.2(?:\.\d+)?", version)
    ):
        raise IraError("unsupported_format")
    if len(root["pages"]) > max_pages:
        raise IraError("resource_limit")
    claims: set[str] = set()
    for mapping in (root, metadata, producer, root["extensions"]):
        if "source_sha256" in mapping:
            value = mapping["source_sha256"]
            if not isinstance(value, str) or not re.fullmatch(r"[A-Fa-f0-9]{64}", value):
                raise IraError("parse_failed")
            claims.add(value.lower())
    if len(claims) > 1:
        raise IraError("source_mismatch")

    recoveries = _recoveries(root["extensions"])
    pages: list[tuple[int, tuple[NativeBlock, ...]]] = []
    seen: set[int] = set()
    block_count = 0
    for i, page in enumerate(root["pages"]):
        if not isinstance(page, dict) or not isinstance(page.get("blocks"), list):
            raise IraError("parse_failed")
        index = page.get("page_idx")
        if type(index) is not int or index < 0 or index in seen:
            raise IraError("invalid_location")
        seen.add(index)
        block_count += len(page["blocks"])
        if block_count > max_blocks:
            raise IraError("resource_limit")
        blocks = tuple(_block(b, f"/pages/{i}/blocks/{j}") for j, b in enumerate(page["blocks"]))
        blocks = tuple(
            _attach_recovery(block, recoveries.pop(block.pointer))
            if block.pointer in recoveries
            else block
            for block in blocks
        )
        pages.append((index, blocks))
    if recoveries:
        raise IraError("invalid_location")
    config = producer.get("config", metadata.get("parser_config"))
    return ParsedArtifact(
        version, config, tuple(pages), root["is_full_document"], next(iter(claims), None)
    )


class _TableText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strings(value: JsonValue) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (int, float)):
        yield str(value)


def normalize_search_text(text: str) -> str:
    return " ".join(text.replace("\x00", " ").casefold().split())


def search_segments(
    kind: str, content: JsonValue, native_context: dict[str, JsonValue]
) -> Iterator[tuple[MatchSource, str]]:
    parts = []
    for text in _strings(content):
        if kind == "table":
            html = _TableText()
            html.feed(text)
            html.close()
            parts.extend(html.parts)
        else:
            parts.append(text)
    yield "body", normalize_search_text(" ".join(parts))
    attachment_fields: tuple[tuple[str, MatchSource], ...] = (
        ("captions", "caption"),
        ("footnotes", "footnote"),
    )
    for key, source in attachment_fields:
        annotations = native_context.get(key)
        if isinstance(annotations, list):
            for annotation in annotations:
                if isinstance(annotation, dict) and isinstance(annotation.get("content"), str):
                    yield source, normalize_search_text(annotation["content"])


def search_text(block: NativeBlock) -> str:
    return " ".join(
        text for _, text in search_segments(block.kind, block.content, block.native_context) if text
    )
