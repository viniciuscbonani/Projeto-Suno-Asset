"""Extração manual de PDF textual e normalização de texto colado para M3."""

from __future__ import annotations

from io import BytesIO
from importlib.metadata import version
import re

import pdfplumber

from suno_asset.domain.models import (
    DocumentMetadata,
    PageDiagnostic,
    ParsedUnit,
    ParseResult,
    SourceOrigin,
)
from suno_asset.services.protocols import ExpectedServiceError


MAX_PDF_PAGES = 100
MAX_PAGE_CHARS = 100_000
MAX_TOTAL_CHARS = 500_000
MAX_TEXT_CHARS = 100_000


def _pdf_error(exc: Exception) -> ExpectedServiceError:
    causes = (exc, *exc.args)
    if any(
        "Password" in type(item).__name__ or "Encrypt" in type(item).__name__
        for item in causes
    ):
        return ExpectedServiceError("pdf_protected", "PDF protegido por senha ou restrição de extração")
    return ExpectedServiceError("invalid_pdf", "PDF corrompido ou com estrutura não suportada")


class PdfPlumberParser:
    """Extrai uma unidade por página física; não presume layout de uma edição."""

    version = f"pdfplumber-{pdfplumber.__version__}-pdfminer-{version('pdfminer.six')}-page-v1"

    def parse(self, raw: bytes, document: DocumentMetadata) -> ParseResult:
        if document.origin != SourceOrigin.UPLOAD:
            raise ExpectedServiceError("invalid_source", "parser PDF exige origem de upload")
        if not raw.startswith(b"%PDF-"):
            raise ExpectedServiceError("invalid_mime", "conteúdo não é PDF")
        try:
            pdf = pdfplumber.open(BytesIO(raw))
        except Exception as exc:
            raise _pdf_error(exc) from exc
        try:
            if not pdf.doc.is_extractable:
                raise ExpectedServiceError("pdf_protected", "PDF não permite extração de texto")
            if not pdf.pages or len(pdf.pages) > MAX_PDF_PAGES:
                raise ExpectedServiceError("page_limit", "PDF sem páginas ou acima do limite de páginas")
            units: list[ParsedUnit] = []
            diagnostics: list[PageDiagnostic] = []
            pages_missing_text: list[int] = []
            pages_with_images: list[int] = []
            pages_with_vectors: list[int] = []
            total_chars = 0
            for number, page in enumerate(pdf.pages, start=1):
                try:
                    char_count = len(page.chars)
                    image_count = len(page.images)
                    vector_count = sum(len(items) for items in (page.rects, page.curves, page.lines))
                    extracted = page.extract_text(layout=False) or ""
                except Exception as exc:
                    raise ExpectedServiceError(
                        "partial_extraction", f"falha na extração da página {number}"
                    ) from exc
                text = extracted.strip()
                if not text:
                    if char_count:
                        pages_missing_text.append(number)
                        status = "extraction_failed"
                    elif image_count:
                        pages_with_images.append(number)
                        status = "image_without_text"
                    elif vector_count:
                        pages_with_vectors.append(number)
                        status = "graphics_without_text"
                    else:
                        status = "blank"
                    diagnostics.append(PageDiagnostic(
                        page=number, status=status, extracted_chars=0,
                        image_count=image_count,
                    ))
                    continue
                if re.search(r"\(cid:\d+\)|\ufffd", text):
                    raise ExpectedServiceError(
                        "partial_extraction", f"glifos não decodificados na página {number}"
                    )
                if len(text) > MAX_PAGE_CHARS:
                    raise ExpectedServiceError("text_limit", f"texto excessivo na página {number}")
                total_chars += len(text)
                if total_chars > MAX_TOTAL_CHARS:
                    raise ExpectedServiceError("text_limit", "texto extraído acima do limite")
                units.append(ParsedUnit(
                    unit_id=f"page-{number}", document_id=document.document_id,
                    parser_version=self.version, page=number,
                    start=0, end=len(text), text=text,
                ))
                diagnostics.append(PageDiagnostic(
                    page=number, status="text", extracted_chars=len(text),
                    image_count=image_count,
                    note="imagens presentes; conteúdo visual não verificado" if image_count else None,
                ))
            if pages_missing_text:
                raise ExpectedServiceError(
                    "partial_extraction", f"texto presente mas ilegível nas páginas {pages_missing_text}"
                )
            if pages_with_vectors:
                raise ExpectedServiceError(
                    "unsupported_structure", f"gráficos sem texto nas páginas {pages_with_vectors}"
                )
            if pages_with_images:
                code = "partial_extraction" if units else "ocr_required"
                raise ExpectedServiceError(
                    code, f"páginas com imagem sem texto: {pages_with_images}; OCR necessário"
                )
            if not units:
                raise ExpectedServiceError("text_unavailable", "PDF não contém texto utilizável")
            return ParseResult(
                parser_version=self.version,
                units=tuple(units), diagnostics=tuple(diagnostics),
            )
        finally:
            pdf.close()


class PlainTextParser:
    """Preserva o texto UTF-8 e localiza trechos por índices de caracteres Unicode."""

    version = "text-utf8-v1"

    def parse(self, raw: bytes, document: DocumentMetadata) -> ParseResult:
        if document.origin != SourceOrigin.TEXT:
            raise ExpectedServiceError("invalid_source", "parser de texto exige texto colado")
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExpectedServiceError("invalid_text", "texto não está em UTF-8") from exc
        if not content.strip() or "\x00" in content:
            raise ExpectedServiceError("invalid_text", "texto vazio ou contém caracteres inválidos")
        if len(content) > MAX_TEXT_CHARS:
            raise ExpectedServiceError("text_limit", "texto acima do limite de caracteres")
        unit = ParsedUnit(
            unit_id="text-1", document_id=document.document_id,
            parser_version=self.version, page=None,
            start=0, end=len(content), text=content,
        )
        return ParseResult(parser_version=self.version, units=(unit,))


class ManualParser:
    """Roteia apenas as duas origens manuais suportadas no M3."""

    def __init__(self) -> None:
        self.pdf = PdfPlumberParser()
        self.text = PlainTextParser()

    def parse(self, raw: bytes, document: DocumentMetadata) -> ParseResult:
        if document.origin == SourceOrigin.UPLOAD:
            return self.pdf.parse(raw, document)
        if document.origin == SourceOrigin.TEXT:
            return self.text.parse(raw, document)
        raise ExpectedServiceError("unsupported_origin", "origem ainda não suportada")
