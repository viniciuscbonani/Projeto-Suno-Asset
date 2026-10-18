"""Operações de ingestão reutilizadas pelo workflow e pela execução manual de M3."""

from __future__ import annotations

from hashlib import sha256
from uuid import NAMESPACE_URL, uuid5

from suno_asset.domain.models import DocumentMetadata, ParseResult, SourceOrigin, SourceRequest
from suno_asset.services.protocols import ExpectedServiceError, Parser, Storage


def register_document(storage: Storage, source: SourceRequest) -> DocumentMetadata:
    raw = storage.read_raw(source)
    if not isinstance(raw, bytes) or not raw:
        raise ExpectedServiceError("invalid_artifact", "artefato ausente ou vazio")
    digest = sha256(raw).hexdigest()
    document = DocumentMetadata(
        document_id=uuid5(NAMESPACE_URL, f"{source.source_id}:{digest}"),
        source_id=source.source_id,
        origin=source.origin,
        sha256=digest,
        artifact_ref=source.artifact_ref,
        original_url=source.original_url,
        accessed_at=source.accessed_at,
        original_filename=source.original_filename,
    )
    storage.save_document(document)
    return document


def parse_document(
    storage: Storage, parser: Parser, document: DocumentMetadata,
) -> tuple[DocumentMetadata, ParseResult]:
    source = SourceRequest(
        source_id=document.source_id, origin=document.origin,
        artifact_ref=document.artifact_ref, original_url=document.original_url,
        accessed_at=document.accessed_at, original_filename=document.original_filename,
    )
    raw = storage.read_raw(source)
    candidate = parser.parse(raw, document)
    if isinstance(candidate, ParseResult):
        candidate = candidate.model_dump(mode="python")
    parsed = ParseResult.model_validate(candidate)
    units = parsed.units
    if (
        not units
        or len({unit.unit_id for unit in units}) != len(units)
        or any(
            unit.document_id != document.document_id
            or unit.parser_version != parsed.parser_version
            or (unit.page is None) != (document.origin == SourceOrigin.TEXT)
            for unit in units
        )
    ):
        raise ExpectedServiceError("invalid_parsing", "unidades extraídas inválidas")
    updated = document.model_copy(update={"parser_version": parsed.parser_version})
    storage.save_document(updated)
    storage.save_units(document.document_id, units)
    storage.save_parse_result(document.document_id, parsed)
    return updated, parsed
