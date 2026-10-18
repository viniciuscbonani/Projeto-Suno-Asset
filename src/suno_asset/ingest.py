"""Entrada manual local de M3. A API HTTP será adicionada em M5."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from suno_asset.services.ingestion import parse_document, register_document
from suno_asset.services.local_storage import LocalStorage
from suno_asset.services.parsing import ManualParser
from suno_asset.services.protocols import ExpectedServiceError


def main(argv: list[str] | None = None) -> int:
    command = argparse.ArgumentParser(description="Ingere PDF textual ou texto colado localmente")
    choice = command.add_mutually_exclusive_group(required=True)
    choice.add_argument("--pdf", type=Path, help="caminho do PDF")
    choice.add_argument("--text", help="texto colado")
    choice.add_argument("--text-file", help="arquivo UTF-8 ou '-' para stdin")
    command.add_argument("--storage", type=Path, default=Path("data"))
    args = command.parse_args(argv)
    try:
        storage = LocalStorage(args.storage)
        if args.pdf is not None:
            source = storage.put_pdf(args.pdf.read_bytes(), filename=args.pdf.name)
        elif args.text_file is not None:
            content = sys.stdin.read() if args.text_file == "-" else Path(args.text_file).read_text(encoding="utf-8")
            source = storage.put_text(content)
        else:
            source = storage.put_text(args.text)
        document = register_document(storage, source)
        document, parsed = parse_document(storage, ManualParser(), document)
    except (ExpectedServiceError, OSError, UnicodeError) as exc:
        code = exc.code if isinstance(exc, ExpectedServiceError) else "input_unavailable"
        print(json.dumps({"error": code, "message": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps({
        "document_id": str(document.document_id),
        "source_id": str(source.source_id),
        "origin": source.origin.value,
        "sha256": document.sha256,
        "parser_version": parsed.parser_version,
        "unit_count": len(parsed.units),
        "diagnostics": [item.model_dump(mode="json") for item in parsed.diagnostics],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
