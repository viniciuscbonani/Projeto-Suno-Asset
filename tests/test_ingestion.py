from __future__ import annotations

from dataclasses import replace
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import UUID

import pytest
from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen import canvas
from reportlab.lib.pdfencrypt import StandardEncryption

from suno_asset.domain.models import (
    Audience, DocumentMetadata, ExecutionStatus, GenerationRequest, OutputFormat,
    SourceOrigin,
)
from suno_asset.services.fakes import fake_services
from suno_asset.services.local_storage import LocalStorage, MAX_PDF_BYTES
from suno_asset.services.ingestion import register_document
from suno_asset.services.parsing import ManualParser, PdfPlumberParser, PlainTextParser
from suno_asset.services.protocols import ExpectedServiceError
from suno_asset.workflow.graph import run_workflow


def make_pdf(*pages: str, image_page: bool = False, vector_page: bool = False,
             password: str | None = None) -> bytes:
    output = BytesIO()
    encryption = StandardEncryption(password) if password else None
    document = canvas.Canvas(output, encrypt=encryption, pageCompression=1)
    for value in pages:
        if value:
            document.drawString(60, 750, value)
        document.showPage()
    if image_page:
        picture = Image.new("RGB", (30, 30), "black")
        document.drawImage(ImageReader(picture), 60, 650)
        document.showPage()
    if vector_page:
        document.rect(60, 650, 100, 100)
        document.showPage()
    document.save()
    return output.getvalue()


def metadata(source) -> DocumentMetadata:
    return DocumentMetadata(
        document_id=UUID(int=42), source_id=source.source_id,
        origin=source.origin, sha256="a" * 64,
        artifact_ref=source.artifact_ref,
        original_filename=source.original_filename,
    )


def check_error(code: str, call) -> None:
    with pytest.raises(ExpectedServiceError) as error:
        call()
    assert error.value.code == code


def test_pdf_storage_parsing_and_workflow_are_persistent(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    raw = make_pdf("Ata do Copom: decisão de 13,75% ao ano.",
                   "A projeção para 2027 é de 3,9%.")
    source = storage.put_pdf(raw, filename="../Ata 281.pdf")
    assert source.original_filename == "Ata_281.pdf"
    assert source.artifact_ref.startswith("raw/") and ".." not in source.artifact_ref
    request = GenerationRequest(
        request_id=UUID(int=43), source=source,
        audience=Audience.BEGINNER, output_format=OutputFormat.ANALYTICAL_TEXT,
    )
    services = replace(fake_services(storage), parser=ManualParser())
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.APPROVED  # Aprovação apenas do workflow simulado.
    assert result.document_id is not None
    reopened = LocalStorage(tmp_path)
    doc = reopened.load_document(result.document_id)
    parsed = reopened.load_parse_result(result.document_id)
    assert doc.sha256 == sha256(raw).hexdigest()
    assert doc.parser_version == parsed.parser_version
    assert [unit.page for unit in parsed.units] == [1, 2]
    assert [diag.status for diag in parsed.diagnostics] == ["text", "text"]
    assert reopened.load_units(result.document_id) == parsed.units
    assert reopened.resolve_evidence(result.references[0]).ref.page == 1
    assert run_workflow(request, services=services).document_id == result.document_id


def test_text_skips_pdf_parser_and_has_no_fake_page(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    content = "Primeiro parágrafo.\n\nA Selic está em 13,75% ao ano."
    source = storage.put_text(content)
    assert source.origin == SourceOrigin.TEXT
    parsed = PlainTextParser().parse(storage.read_raw(source), metadata(source))
    assert parsed.units[0].page is None
    assert parsed.units[0].text == content
    assert parsed.units[0].start == 0 and parsed.units[0].end == len(content)
    request = GenerationRequest(
        request_id=UUID(int=44), source=source,
        audience=Audience.BEGINNER, output_format=OutputFormat.ANALYTICAL_TEXT,
    )
    result = run_workflow(request, services=replace(fake_services(storage), parser=ManualParser()))
    assert result.status == ExecutionStatus.APPROVED
    assert result.references[0].page is None
    assert storage.load_document(result.document_id).sha256 == sha256(content.encode()).hexdigest()


def test_pdf_blank_page_is_distinct_from_missing_text(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    source = storage.put_pdf(make_pdf("Conteúdo relevante.", ""), filename="ata.pdf")
    parsed = PdfPlumberParser().parse(storage.read_raw(source), metadata(source))
    assert [diagnostic.status for diagnostic in parsed.diagnostics] == ["text", "blank"]
    assert [unit.page for unit in parsed.units] == [1]


def test_pdf_scanned_and_mixed_pages_are_blocked(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    scanned = storage.put_pdf(make_pdf(image_page=True), filename="scan.pdf")
    mixed = storage.put_pdf(make_pdf("Texto válido.", image_page=True), filename="mixed.pdf")
    check_error("ocr_required", lambda: PdfPlumberParser().parse(
        storage.read_raw(scanned), metadata(scanned)))
    check_error("partial_extraction", lambda: PdfPlumberParser().parse(
        storage.read_raw(mixed), metadata(mixed)))
    vector = storage.put_pdf(make_pdf(vector_page=True), filename="vector.pdf")
    check_error("unsupported_structure", lambda: PdfPlumberParser().parse(
        storage.read_raw(vector), metadata(vector)))


def test_pdf_empty_corrupt_type_limits_and_protection(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    valid = make_pdf("Texto de teste.")
    check_error("invalid_artifact", lambda: storage.put_pdf(b"", filename="ata.pdf"))
    check_error("invalid_extension", lambda: storage.put_pdf(valid, filename="ata.txt"))
    check_error("invalid_mime", lambda: storage.put_pdf(valid, filename="ata.pdf", declared_mime="text/plain"))
    check_error("invalid_mime", lambda: storage.put_pdf(b"texto", filename="ata.pdf"))
    check_error("invalid_pdf", lambda: storage.put_pdf(b"%PDF-1.7\ninvalid", filename="ata.pdf"))
    check_error("size_limit", lambda: storage.put_pdf(b"%PDF-" + b"x" * MAX_PDF_BYTES, filename="ata.pdf"))
    check_error("page_limit", lambda: storage.put_pdf(make_pdf(*([""] * 101)), filename="many.pdf"))
    check_error("pdf_protected", lambda: storage.put_pdf(
        make_pdf("Segredo", password="senha"), filename="protected.pdf"))


def test_text_validation_and_artifact_identity(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    check_error("invalid_text", lambda: storage.put_text(" \n "))
    check_error("invalid_text", lambda: storage.put_text("a\x00b"))
    check_error("text_limit", lambda: storage.put_text("a" * 100_001))
    first = storage.put_text("O mesmo texto.")
    second = storage.put_text("O mesmo texto.")
    assert first.source_id != second.source_id
    assert storage.read_raw(first) == storage.read_raw(second)
    fake = first.model_copy(update={"artifact_ref": second.artifact_ref})
    check_error("invalid_artifact_ref", lambda: storage.read_raw(fake))
    assert sha256(storage.read_raw(first)).hexdigest() == sha256(storage.read_raw(second)).hexdigest()
    assert register_document(storage, first).document_id != register_document(storage, second).document_id


def test_partial_pdf_fails_workflow_before_generation(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path)
    source = storage.put_pdf(make_pdf("Texto válido.", image_page=True), filename="mixed.pdf")
    request = GenerationRequest(
        request_id=UUID(int=45), source=source,
        audience=Audience.BEGINNER, output_format=OutputFormat.ANALYTICAL_TEXT,
    )
    services = replace(fake_services(storage), parser=ManualParser())
    result = run_workflow(request, services=services)
    assert result.status == ExecutionStatus.FAILED
    assert result.errors[0].stage == "parse"
    assert result.errors[0].code == "partial_extraction"
    assert services.generator is not None and result.content is None
