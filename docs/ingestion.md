# Ingestão de documentos

A ingestão recebe PDF textual ou texto colado, valida a entrada e registra origem, identificadores e hash. As duas entradas convergem para os contratos de documento, unidades extraídas e evidências.

## Entradas e limites

| Entrada | Validação | Limite | Localização |
|---|---|---|---|
| PDF | Extensão, MIME, assinatura `%PDF-` e abertura estrutural | 10 MiB e 100 páginas | Página física a partir de 1 e offsets no texto extraído. |
| Texto | UTF-8, não vazio e sem NUL | 100.000 caracteres e 1 MiB | Offsets Unicode no texto preservado; sem página fictícia. |

O nome do arquivo é sanitizado e não define o caminho de armazenamento. Identificadores de origem e documento são distintos do hash: os mesmos bytes podem ter origens diferentes. URL e título declarados pela pessoa usuária não comprovam autenticidade.

## Ingestão pela API

```bash
curl -X POST http://127.0.0.1:8000/documents \
  -H 'Content-Type: application/pdf' \
  -H 'X-Filename: ata.pdf' \
  --data-binary @/caminho/ata.pdf
```

```bash
curl -X POST http://127.0.0.1:8000/documents \
  -H 'Content-Type: application/json' \
  -d '{"text":"Texto integral da fonte pública."}'
```

Cada pedido aceita um meio de entrada e retorna `source_id`. A extração e as etapas seguintes são executadas no processamento do documento. Veja [API](api.md).

## Ingestão pela linha de comando

Estes comandos registram e extraem a fonte sem gerar conteúdo:

```bash
uv run --frozen --extra dev python -m suno_asset.ingest --pdf /caminho/ata.pdf --storage data
uv run --frozen --extra dev python -m suno_asset.ingest --text-file /caminho/fonte.txt --storage data
```

A saída inclui identificadores, hash, parser, unidades e diagnóstico. O diretório definido por `--storage` deve permanecer fora do repositório.

## Diagnóstico e proveniência

O parser trabalha por página, sem pressupor títulos, numeração de itens ou posição de tabelas de uma edição específica. PDF protegido, corrompido, acima dos limites, sem texto, com extração parcial suspeita ou estrutura incompatível produz diagnóstico próprio.

Uma página com texto não comprova extração completa: imagens e tabelas podem conter informação material. Células, notas e parágrafos que atravessam páginas exigem preservação de contexto. Ambiguidade material impede aprovação silenciosa. OCR não é executado.

`start` e `end` são índices no texto da unidade e do parser registrados, não coordenadas visuais do PDF. Novo parsing ou segmentação não deve redirecionar referências antigas. A identidade dos artefatos preserva a relação entre afirmação, evidência e fonte.

As [anotações de referência](fixtures/copom281-20260916-annotations.md) exemplificam a conferência de páginas, fatos e ressalvas de uma fonte identificada por hash; não representam suporte a qualquer PDF.
