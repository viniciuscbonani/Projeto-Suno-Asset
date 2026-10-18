"""Armazenamento local com identificadores internos e metadados persistentes para M3."""

from __future__ import annotations

from hashlib import sha256
from io import BytesIO
import os
import json
from pathlib import Path
import re
from uuid import UUID, uuid4

import pdfplumber

from suno_asset.domain.models import (
    Chunk, DocumentMetadata, Evidence, EvidenceRef, ParsedUnit, ParseResult,
    SourceOrigin, SourceRequest,
)
from suno_asset.services.parsing import MAX_PDF_PAGES, MAX_TEXT_CHARS, _pdf_error
from suno_asset.services.protocols import ExpectedServiceError


MAX_PDF_BYTES = 10 * 1024 * 1024
MAX_TEXT_BYTES = 1024 * 1024
_ARTIFACT_REF = re.compile(r"raw/([0-9a-f]{32})\.(pdf|txt)\Z")


def safe_filename(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", base).lstrip("._")[:100]
    if not cleaned or cleaned in {".", ".."}:
        raise ExpectedServiceError("invalid_filename", "nome de arquivo inválido")
    return cleaned


class LocalStorage:
    """Guarda bytes, unidades, diagnósticos e evidências em uma raiz controlada."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        for folder in ("raw", "sources", "documents", "units", "parse_results", "chunks", "evidence", "runs"):
            candidate = self.root / folder
            if candidate.is_symlink():
                raise ExpectedServiceError("invalid_storage_path", "diretório de armazenamento inválido")
            candidate.mkdir(parents=True, exist_ok=True)
        from suno_asset.services.sqlite_repository import SQLiteRepository
        self.repository = SQLiteRepository(self.root)
        from suno_asset.services.artifact_lifecycle import import_legacy
        self.legacy_issues = import_legacy(self)

    def _path(self, relative: str) -> Path:
        rel = Path(relative)
        if rel.is_absolute() or ".." in rel.parts or not rel.parts:
            raise ExpectedServiceError("invalid_storage_path", "caminho de armazenamento inválido")
        path = self.root
        for part in rel.parts:
            path = path / part
            if path.is_symlink():
                raise ExpectedServiceError("invalid_storage_path", "symlink não permitido")
        if not path.is_relative_to(self.root):
            raise ExpectedServiceError("invalid_storage_path", "caminho de armazenamento inválido")
        return path

    def _owners(self, relative, content):
        folder, name = relative.split('/', 1)
        if folder in {'raw', 'sources'}:
            return [('source', str(UUID(name.split('.')[0])))]
        if folder == 'documents':
            document = DocumentMetadata.model_validate_json(content)
            self.repository.register_document(str(document.document_id), str(document.source_id))
            return [('document', str(document.document_id))]
        if folder in {'units', 'parse_results', 'chunks'}:
            return [('document', str(UUID(name[:32])))]
        if folder == 'evidence':
            item = Evidence.model_validate_json(content)
            return [('document', str(item.ref.document_id))]
        if folder == 'runs':
            return [('run', str(UUID(name[:32])))]
        raise ExpectedServiceError('invalid_storage_path', 'diretório não gerenciado')

    def _write(self, relative: str, content: bytes, *, replace: bool = False) -> None:
        path = self._path(relative)
        digest = sha256(content).hexdigest()
        owners = self._owners(relative, content)
        if path.exists() and not replace and path.read_bytes() != content:
            raise ExpectedServiceError("immutable_artifact", "artefato versionado não pode ser sobrescrito")
        previous = self.repository.get_artifact(relative)
        if previous and previous['state'] == 'available' and previous['digest'] == digest and path.exists() and path.read_bytes() == content:
            self.repository.ensure_artifact_available(relative)
            return
        self.repository.begin_artifact(relative, digest, len(content), owners, replace=replace)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self.repository.publish_artifact(relative)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self, relative: str) -> bytes:
        path = self._path(relative)
        artifact = self.repository.ensure_artifact_available(relative)
        try:
            content = path.read_bytes()
        except FileNotFoundError as exc:
            raise ExpectedServiceError("artifact_missing", "artefato não encontrado") from exc
        if sha256(content).hexdigest() != artifact['digest']:
            raise ExpectedServiceError('artifact_unavailable', 'integridade do artefato não confirmada')
        return content

    def put_pdf(
        self, raw: bytes, *, filename: str, declared_mime: str = "application/pdf"
    ) -> SourceRequest:
        name = safe_filename(filename)
        if not name.lower().endswith(".pdf"):
            raise ExpectedServiceError("invalid_extension", "arquivo deve ter extensão PDF")
        if declared_mime.lower().split(";", 1)[0].strip() != "application/pdf":
            raise ExpectedServiceError("invalid_mime", "MIME declarado não corresponde a PDF")
        if not isinstance(raw, bytes) or not raw:
            raise ExpectedServiceError("invalid_artifact", "arquivo ausente ou vazio")
        if len(raw) > MAX_PDF_BYTES:
            raise ExpectedServiceError("size_limit", "PDF acima do limite de 10 MiB")
        if not raw.startswith(b"%PDF-"):
            raise ExpectedServiceError("invalid_mime", "conteúdo não é PDF")
        try:
            with pdfplumber.open(BytesIO(raw)) as pdf:
                if not pdf.doc.is_extractable:
                    raise ExpectedServiceError("pdf_protected", "PDF não permite extração de texto")
                if not pdf.pages or len(pdf.pages) > MAX_PDF_PAGES:
                    raise ExpectedServiceError("page_limit", "PDF sem páginas ou acima de 100 páginas")
        except ExpectedServiceError:
            raise
        except Exception as exc:
            raise _pdf_error(exc) from exc
        source_id = uuid4()
        self.repository.register_source(str(source_id))
        ref = f"raw/{source_id.hex}.pdf"
        source = SourceRequest(
            source_id=source_id, origin=SourceOrigin.UPLOAD,
            artifact_ref=ref, original_filename=name,
        )
        self._write(ref, raw)
        self._write(f"sources/{source_id.hex}.json", source.model_dump_json().encode("utf-8"))
        return source

    def put_text(self, content: str) -> SourceRequest:
        if not isinstance(content, str) or not content.strip() or "\x00" in content:
            raise ExpectedServiceError("invalid_text", "texto vazio ou contém caracteres inválidos")
        if len(content) > MAX_TEXT_CHARS:
            raise ExpectedServiceError("text_limit", "texto acima do limite de caracteres")
        raw = content.encode("utf-8")
        if len(raw) > MAX_TEXT_BYTES:
            raise ExpectedServiceError("size_limit", "texto acima do limite de 1 MiB")
        source_id = uuid4()
        self.repository.register_source(str(source_id))
        ref = f"raw/{source_id.hex}.txt"
        source = SourceRequest(source_id=source_id, origin=SourceOrigin.TEXT, artifact_ref=ref)
        self._write(ref, raw)
        self._write(f"sources/{source_id.hex}.json", source.model_dump_json().encode("utf-8"))
        return source

    def load_source(self, source_id: UUID) -> SourceRequest:
        self.repository.ensure_available("source", str(source_id))
        source = SourceRequest.model_validate_json(self._read(f"sources/{source_id.hex}.json"))
        if source.source_id != source_id or not _ARTIFACT_REF.fullmatch(source.artifact_ref):
            raise ExpectedServiceError('invalid_artifact_ref', 'referência de origem inválida')
        self._read(source.artifact_ref)
        return source

    def read_raw(self, source: SourceRequest) -> bytes:
        self.repository.ensure_available('source', str(source.source_id))
        match = _ARTIFACT_REF.fullmatch(source.artifact_ref)
        if not match or (match.group(2) == "pdf") != (source.origin == SourceOrigin.UPLOAD):
            raise ExpectedServiceError("invalid_artifact_ref", "referência de artefato inválida")
        stored = SourceRequest.model_validate_json(self._read(f"sources/{source.source_id.hex}.json"))
        if stored != source or match.group(1) != source.source_id.hex:
            raise ExpectedServiceError("invalid_artifact_ref", "referência não pertence à origem")
        return self._read(source.artifact_ref)

    def save_document(self, document: DocumentMetadata) -> None:
        relative = f"documents/{document.document_id.hex}.json"
        path = self.root / relative
        if path.exists():
            previous = DocumentMetadata.model_validate_json(self._read(relative))
            if previous == document:
                return
            old = previous.model_copy(update={"parser_version": None})
            incoming = document.model_copy(update={"parser_version": None})
            if old != incoming:
                raise ExpectedServiceError("immutable_artifact", "metadados de outra origem ou versão")
            if previous.parser_version and document.parser_version is None:
                return  # Repetição de ingestão antes de confirmar a mesma versão do parser.
            if previous.parser_version is not None:
                raise ExpectedServiceError("immutable_artifact", "parser de documento já versionado")
            if document.parser_version is None:
                return
            self._write(relative, document.model_dump_json().encode("utf-8"), replace=True)
            return
        self._write(relative, document.model_dump_json().encode("utf-8"))

    def _ensure_known_document(self, document_id):
        try:
            self.repository.ensure_available('document', str(document_id))
        except ExpectedServiceError as exc:
            if exc.code != 'resource_not_found':
                raise

    def load_document(self, document_id: UUID) -> DocumentMetadata:
        self.repository.ensure_available("document", str(document_id))
        return DocumentMetadata.model_validate_json(self._read(f"documents/{document_id.hex}.json"))

    def save_units(self, document_id: UUID, units: tuple[ParsedUnit, ...]) -> None:
        from pydantic import TypeAdapter
        data = TypeAdapter(tuple[ParsedUnit, ...]).dump_json(units)
        self._write(f"units/{document_id.hex}.json", data)

    def load_units(self, document_id: UUID) -> tuple[ParsedUnit, ...]:
        self._ensure_known_document(document_id)
        from pydantic import TypeAdapter
        return TypeAdapter(tuple[ParsedUnit, ...]).validate_json(
            self._read(f"units/{document_id.hex}.json")
        )

    def save_parse_result(self, document_id: UUID, result: ParseResult) -> None:
        self._write(
            f"parse_results/{document_id.hex}.json", result.model_dump_json().encode("utf-8")
        )

    def load_parse_result(self, document_id: UUID) -> ParseResult:
        self._ensure_known_document(document_id)
        return ParseResult.model_validate_json(self._read(f"parse_results/{document_id.hex}.json"))

    def save_chunks(self, document_id: UUID, chunks: tuple[Chunk, ...]) -> None:
        from pydantic import TypeAdapter
        self._write(
            f"chunks/{document_id.hex}.json", TypeAdapter(tuple[Chunk, ...]).dump_json(chunks)
        )

    def save_evidence(self, evidence: tuple[Evidence, ...]) -> None:
        for item in evidence:
            key = sha256(item.ref.model_dump_json().encode("utf-8")).hexdigest()
            self._write(f"evidence/{key}.json", item.model_dump_json().encode("utf-8"))

    def resolve_evidence(self, ref: EvidenceRef) -> Evidence:
        try:
            self.repository.ensure_available('document', str(ref.document_id))
        except ExpectedServiceError as exc:
            if exc.code != 'resource_not_found':
                raise
        key = sha256(ref.model_dump_json().encode("utf-8")).hexdigest()
        item = Evidence.model_validate_json(self._read(f"evidence/{key}.json"))
        if item.ref != ref:
            raise ExpectedServiceError("invalid_evidence", "evidência armazenada diverge da referência")
        return item


    def save_run_artifact(self, request_id: UUID, name: str, value: object) -> None:
        if not re.fullmatch(r'[a-z][a-z-]{0,50}', name):
            raise ExpectedServiceError('invalid_storage_path', 'nome de artefato inválido')
        self._write(f'runs/{request_id.hex}-{name}.json',
            (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)+'\n').encode('utf-8'))


    def save_source_chunks(self, document_id: UUID, chunks: tuple[Chunk, ...]) -> None:
        from pydantic import TypeAdapter
        data = TypeAdapter(tuple[Chunk, ...]).dump_json(chunks)
        version = sha256(data).hexdigest()[:24]
        self._write(f'chunks/{document_id.hex}-literal-{version}.json',data)


    def load_execution_result(self, request_id: UUID):
        from suno_asset.domain.models import ExecutionResult
        self.repository.ensure_available('run', str(request_id))
        row = self.repository.get_run(str(request_id))
        if row['state'] != 'completed':
            raise ExpectedServiceError('generation_unavailable', 'execução não está concluída')
        from pydantic import ValidationError
        try:
            for artifact in self.repository.artifacts_for('run', str(request_id)):
                self._read(artifact['path'])
            result = ExecutionResult.model_validate_json(self._read(f'runs/{request_id.hex}-result.json'))
            self.validate_references(result)
            self.validate_audit(result, legacy=row["configuration"].get("legacy", False))
            return result
        except (ExpectedServiceError, ValidationError) as exc:
            if isinstance(exc, ExpectedServiceError) and exc.code in {'storage_busy', 'storage_unavailable', 'resource_deleted'}:
                raise
            self.repository.fail_run(str(request_id), 'artifact_unavailable', 503)
            raise ExpectedServiceError('artifact_unavailable', 'Resultado e dependências indisponíveis.') from None


    def save_provider_artifact(self, request_id, attempt, kind, value):
        if kind not in {'request', 'response'} or type(attempt) is not int or attempt < 1:
            raise ValueError('invalid provider artifact')
        self._write(f'runs/{request_id.hex}-attempt-{attempt:04d}-{kind}.json',
                    (json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)+'\n').encode())

    def begin_execution(self, request):
        request_id = str(request.request_id)
        row = self.repository.get_run(request_id)
        relative = f'runs/{request.request_id.hex}-request.json'
        if row and row['state'] == 'completed':
            try:
                stored = json.loads(self._read(relative))
            except ExpectedServiceError:
                raise ExpectedServiceError('execution_identity_conflict', 'Pedido original não verificável.') from None
            if stored != request.model_dump(mode='json'):
                raise ExpectedServiceError('execution_identity_conflict', 'Pedido diverge da execução existente.')
            return self.load_execution_result(request.request_id)
        self.repository.ensure_run(request_id, str(request.source.source_id),
                                   mode=request.mode.value, configuration=request.resolved_configuration)
        self.save_run_artifact(request.request_id, 'request', request.model_dump(mode='json'))
        return None

    def validate_references(self, result):
        refs = set(result.references)
        for revision in result.history:
            if revision.content:
                refs.update(revision.content.references)
        for ref in refs:
            try:
                self.resolve_evidence(ref)
            except ExpectedServiceError:
                raise ExpectedServiceError('artifact_unavailable', 'evidência indisponível') from None

    def validate_audit(self, result, *, legacy=False):
        if result.mode.value != 'real' or legacy:
            return
        try:
            prefix = f'runs/{result.request_id.hex}'
            trace = json.loads(self._read(prefix + '-llm-trace.json'))
            attempts = self.repository.attempts_for(str(result.request_id))
            diagnostics = [a for a in trace['attempts'] if a['status'] != 'deferred']
            if len(attempts) != result.llm_attempts or len(diagnostics) != result.llm_attempts:
                raise ValueError('attempt count')
            journal = {a['attempt_id']: a for a in attempts}
            for diagnostic in diagnostics:
                number = diagnostic['attempt']
                attempt = journal[f'{result.request_id}:{number}']
                if attempt['status'] not in {'confirmed', 'unknown'}:
                    raise ValueError('unsettled attempt')
                request = json.loads(self._read(f'{prefix}-attempt-{number:04d}-request.json'))
                digest = sha256(json.dumps(request, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                if digest != diagnostic['payload_sha256'] or request['model'] != attempt['model'] or diagnostic['model'] != attempt['model'] or diagnostic['stage'] != attempt['stage']:
                    raise ValueError('attempt identity')
                if attempt['status'] == 'confirmed':
                    self._read(f'{prefix}-attempt-{number:04d}-response.json')
        except (ExpectedServiceError, KeyError, ValueError, TypeError):
            raise ExpectedServiceError('artifact_unavailable', 'Auditoria obrigatória não verificável.') from None

    def commit_execution(self, result):
        self.validate_references(result)
        self.validate_audit(result)
        paths = [item['path'] for item in self.repository.artifacts_for('run', str(result.request_id))]
        if result.mode.value == 'real' and f'runs/{result.request_id.hex}-llm-trace.json' not in paths:
            raise ExpectedServiceError('audit_missing', 'auditoria obrigatória ausente')
        for relative in paths:
            self._read(relative)
        self.repository.commit_result(str(result.request_id), f'runs/{result.request_id.hex}-result.json', paths)
