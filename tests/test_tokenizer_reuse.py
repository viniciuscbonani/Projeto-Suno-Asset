"""Reusing BPE construction never skips local vocabulary integrity checks."""
import base64
from hashlib import sha256

import pytest

from suno_asset.services import llm_budget
from suno_asset.services.protocols import ExpectedServiceError


def test_equal_verified_vocabularies_share_encoding_but_changed_file_is_rejected(tmp_path,monkeypatch):
    vocabulary=b'\n'.join(base64.b64encode(bytes([value]))+b' '+str(value).encode() for value in range(256))
    path=tmp_path/'synthetic-bpe.tiktoken'
    path.write_bytes(vocabulary)
    monkeypatch.setattr(llm_budget,'TOKENIZER_SHA256',sha256(vocabulary).hexdigest())
    first=llm_budget.TokenCounter(path)
    second=llm_budget.TokenCounter(path)
    assert first.encoding is second.encoding
    assert first.count('Selic: 13,75%')==second.count('Selic: 13,75%')
    path.write_bytes(b'changed vocabulary')
    with pytest.raises(ExpectedServiceError,match='hash divergente'):
        llm_budget.TokenCounter(path)
