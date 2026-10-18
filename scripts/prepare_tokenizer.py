"""Download only the public BPE vocabulary, verify SHA-256; no API credentials."""
from hashlib import sha256
from urllib.request import Request, urlopen

from suno_asset.services.llm_budget import TOKENIZER_PATH, TOKENIZER_SHA256, TOKENIZER_URL


def main():
    if TOKENIZER_PATH.is_file() and sha256(TOKENIZER_PATH.read_bytes()).hexdigest() == TOKENIZER_SHA256:
        print('Vocabulário já preparado e hash válido.')
        return
    with urlopen(Request(TOKENIZER_URL, headers={'User-Agent': 'suno-asset/0.1'}), timeout=30) as response:
        raw = response.read(8_000_001)
    if len(raw) > 8_000_000 or sha256(raw).hexdigest() != TOKENIZER_SHA256:
        raise ValueError('Vocabulário inválido; nenhum arquivo escrito')
    TOKENIZER_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKENIZER_PATH.write_bytes(raw)
    print('Vocabulário público preparado; contagem offline disponível.')


if __name__ == '__main__': main()
