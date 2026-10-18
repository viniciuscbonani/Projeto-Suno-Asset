"""Cache factual imutável por chave completa de dependências."""
from __future__ import annotations

from hashlib import sha256
import json
import os
from pathlib import Path
from contextlib import contextmanager
from uuid import uuid4


def cache_key(dependencies: dict) -> str:
    required = {"source_sha256", "block_id", "context_sha256", "parser_version", "segmentation_version",
        "extractor_version", "model_config", "prompt_version", "schema_version", "verification_policy"}
    missing = required - dependencies.keys()
    if missing:
        raise ValueError(f"chave de cache sem dependências: {sorted(missing)}")
    return sha256(json.dumps(dependencies, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class VerifiedFactCache:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
            raise ValueError("chave de cache inválida")
        return self.root / f"{key}.json"

    def quarantine(self, key: str):
        """Preserve a rejected entry before rebuilding it; caller holds key lock."""
        path = self._path(key)
        if path.exists():
            rejected = self.root / 'rejected'
            rejected.mkdir(exist_ok=True)
            os.replace(path,rejected/f'{key}-{uuid4().hex}.json')

    @contextmanager
    def lock(self, key: str):
        """Serialize preparation for one key across API workers on POSIX."""
        import fcntl
        path = self.root / f".{key}.lock"
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def get(self, key: str, dependencies: dict) -> dict | None:
        path = self._path(key)
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            body = envelope["body"]
            digest = sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            if envelope.get("sha256") != digest or body.get("dependencies") != dependencies:
                return None
            if body.get("verification", {}).get("complete") is not True or body.get("pending_material_checks"):
                return None
            return body
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def put(self, key: str, dependencies: dict, facts: dict, *, verified: bool,
            pending_material_checks: tuple[str, ...] = ()) -> None:
        if not verified or pending_material_checks:
            raise ValueError("cache factual aceita apenas ficha totalmente verificada")
        body = {"dependencies": dependencies, "facts": facts,
            "verification": {"complete": True}, "pending_material_checks": []}
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        envelope = json.dumps({"version": "verified-fact-cache-v1", "body": body,
            "sha256": sha256(canonical.encode()).hexdigest()}, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
        path = self._path(key)
        if path.exists():
            previous = self.get(key, dependencies)
            if previous is not None:
                if previous != body:
                    raise ValueError("cache factual imutável divergente")
                return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(envelope, encoding="utf-8")
        os.replace(temporary, path)

    def get_annotation(self, key: str, dependencies: dict) -> dict | None:
        path = self.root / f"annotation-{key}.json"
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            body = envelope["body"]
            digest = sha256(json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
            if (envelope.get("sha256") != digest or body.get("dependencies") != dependencies
                or body.get("status") != "complete"):
                return None
            return body.get("value")
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None

    def put_annotation(self, key: str, dependencies: dict, value: dict, *, complete: bool):
        if not complete:
            raise ValueError("anotação pendente não entra no cache")
        body = {"dependencies":dependencies,"status":"complete","value":value}
        canonical = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        envelope = json.dumps({"version":"verified-annotation-cache-v1","body":body,
            "sha256":sha256(canonical.encode()).hexdigest()}, sort_keys=True, ensure_ascii=False, indent=2) + "\n"
        path = self.root / f"annotation-{key}.json"
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_text(envelope, encoding="utf-8")
        os.replace(temporary, path)
