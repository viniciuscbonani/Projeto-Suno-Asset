# Projeto Suno Asset

> Sistema para geração de conteúdo financeiro fiel à fonte, adaptado ao público e ao formato, com avaliação automática e rastreabilidade.

## Status

 Projeto em fase de documentação e fundação. A aplicação ainda não possui uma versão executável.

## Sobre

O projeto transforma documentos financeiros públicos e densos — como atas do Copom, fatos relevantes e releases de resultados — em conteúdo adequado a diferentes níveis de conhecimento.

O pipeline previsto recupera evidências, estrutura fatos relevantes, gera o conteúdo, avalia sua qualidade e tenta refiná-lo quando necessário. Afirmações importantes devem permanecer rastreáveis ao documento original.

## Funcionalidades planejadas

- upload manual de documentos;
- recuperação controlada de fontes públicas na web;
- extração e segmentação com referência à origem;
- geração para públicos beginner, intermediate e advanced;
- texto analítico, carrossel informativo e roteiro de vídeo curto;
- avaliação estrutural, factual e semântica;
- refinamento automático limitado;
- citações e relatório de qualidade.

## Stack

- Python 3.12;
- uv;
- FastAPI;
- LangGraph;
- Pydantic;
- pytest.

## Documentação

A especificação completa de produto, requisitos, arquitetura, regras de negócio, segurança e roadmap está em [docs/project.md](docs/project.md).

## Começando

Os comandos abaixo serão preenchidos quando a fundação do projeto estiver implementada.

### Pré-requisitos

- Python 3.12;
- uv.

### Instalação

```bash
# TODO: adicionar comando após a criação do pyproject.toml
```

### Execução

```bash
# TODO: adicionar comando após a criação da aplicação FastAPI
```

### Testes

```bash
# TODO: adicionar comando após a configuração do pytest
```

## Estrutura esperada

```text
.
├── docs/              # documentação do produto
├── src/               # código da aplicação
├── tests/             # testes automatizados
├── pyproject.toml     # projeto e dependências
└── README.md
```

A estrutura será atualizada quando os diretórios forem criados.

## Roadmap resumido

1. Contratos de domínio e esqueleto do workflow.
2. Vertical slice com upload de uma ata do Copom.
3. Três públicos e três formatos.
4. Recuperação de documentos na web.
5. Calibração de qualidade e preparação operacional.

## Contribuição

As convenções de desenvolvimento e o fluxo de contribuição serão adicionados junto da primeira implementação.

## Licença

Ainda não definida.
