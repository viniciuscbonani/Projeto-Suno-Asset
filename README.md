# Suno Asset

Sistema acadêmico de geração assistida de conteúdo financeiro, com adaptação ao público, avaliação automática e rastreabilidade até a fonte.

O projeto transforma atas do Copom em PDF textual ou texto colado em texto analítico, carrossel informativo e roteiro de vídeo curto. Cada solicitação combina um formato com o nível iniciante, intermediário ou avançado. Carrosséis e roteiros são entregues como texto estruturado.

O processamento valida a entrada, preserva a localização das evidências, extrai e verifica fatos, gera o conteúdo e avalia estrutura, cobertura, sustentação e linguagem. Correções são limitadas por tentativas e orçamento. O resultado inclui referências, alertas e a avaliação da versão entregue.

O conteúdo é informativo. A aprovação automática exige revisão editorial antes da publicação e não constitui recomendação personalizada de investimento.

## Instalação

Requisitos: Python 3.12 e [uv](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv sync --frozen --extra dev
```

## Execução local

```bash
uv run --frozen --extra dev uvicorn suno_asset.api.app:app --host 127.0.0.1 --port 8000
```

- Estúdio de conteúdo: <http://127.0.0.1:8000/>
- Documentação interativa da API: <http://127.0.0.1:8000/docs>
- Capacidades e configuração efetiva: <http://127.0.0.1:8000/capabilities>

A interface permite enviar uma fonte, escolher público e formato, consultar evidências e abrir análises recentes. O modo de demonstração está disponível nas opções da interface e não exige credenciais. A busca por tema usa um exemplo fictício fixo, identificado como demonstração; não faz pesquisa na web.

Para inferência real, configure um provedor conforme o [guia de configuração](docs/configuration.md). A interface solicita análise real por padrão; configurar uma chave não muda automaticamente o perfil utilizado. Consulte as capacidades do servidor e escolha explicitamente o modo desejado.

## Exemplo pela API

Registre um texto de exemplo:

```bash
curl -X POST http://127.0.0.1:8000/documents \
  -H 'Content-Type: application/json' \
  -d '{"text":"O Comitê decidiu manter a taxa de juros. Exemplo fictício para demonstração."}'
```

Use o `source_id` retornado para solicitar uma saída simulada:

```bash
curl -X POST http://127.0.0.1:8000/generations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: exemplo-conteudo-001' \
  -d '{"source_id":"UUID-RETORNADO","architecture":"literal-v1","audience":"beginner","output_format":"analytical_text","mode":"simulated"}'
```

O modo simulado verifica controles determinísticos e identifica a saída como demonstração. Ele não comprova qualidade factual ou semântica de um modelo real. Veja uploads de PDF, formatos e consultas no [guia da API](docs/api.md).

## Documentação

| Documento | Conteúdo |
|---|---|
| [Projeto](docs/project.md) | Objetivos, requisitos, regras, arquitetura e critérios de qualidade. |
| [Configuração](docs/configuration.md) | Ambiente, provedores, credenciais e cotas. |
| [Ingestão](docs/ingestion.md) | Entradas aceitas, limites e diagnósticos. |
| [API](docs/api.md) | Rotas, contratos, exemplos e idempotência. |
| [Estúdio de conteúdo](docs/frontend.md) | Interface, referências e histórico. |
| [Arquitetura híbrida](docs/hybrid-architecture.md) | Preparação compartilhada, cache, seleção e revisão. |
| [Operação local](docs/operations.md) | Armazenamento, recuperação e exclusão de dados. |
| [Avaliação](docs/evaluation.md) | Método de avaliação, testes e limitações. |

## Tecnologias

Python, FastAPI, LangGraph, Pydantic, pdfplumber e SQLite no backend; HTML, CSS e JavaScript na interface. Os adaptadores de inferência integram Gemini e Groq. As dependências estão fixadas em `uv.lock`.

```text
.
├── .github/workflows/  # Integração contínua
├── docs/              # Documentação pública
├── scripts/           # Utilitários
├── src/suno_asset/    # API, domínio, serviços, workflow e interface
├── tests/             # Testes e dados de referência
├── .env.example       # Configuração sem credenciais
├── pyproject.toml
└── uv.lock
```

## Testes e contribuição

```bash
uv run --frozen --extra dev python scripts/prepare_tokenizer.py
uv run --frozen --extra dev pytest -m 'not live'
```

A preparação baixa somente o vocabulário público do tokenizador e confere seu hash. Depois dela, a suíte funciona offline com serviços simulados, sem chaves. A conferência com um PDF oficial é opcional e está descrita no [guia de avaliação](docs/evaluation.md). Testes de navegação usam Playwright, conforme o [guia da interface](docs/frontend.md).

Ao alterar regras ou contratos, atualize a documentação correspondente e verifique os testes de comportamento. Mudanças de dependências devem incluir a atualização de `uv.lock`. Credenciais, documentos enviados e resultados locais devem permanecer fora do repositório.

## Limitações

O uso é local, sem autenticação multiusuário. O suporte de documentos é restrito a atas do Copom textuais em português. OCR, pesquisa web funcional, publicação em redes sociais e renderização de imagens ou vídeos não fazem parte das capacidades oferecidas. A avaliação automática pode falhar; confira o conteúdo e as evidências antes de publicar.

## Licença

O repositório não declara licença de distribuição do projeto. Avisos de componentes de terceiros ficam em [docs/licenses](docs/licenses/). Marcas e elementos visuais pertencem aos respectivos titulares.
