# Estúdio de conteúdo

O estúdio é a interface local para gerar e consultar conteúdo com suas evidências. Ele é servido pela própria API, sem build ou servidor de frontend separado.

## Acesso

```bash
uv run --frozen --extra dev uvicorn suno_asset.api.app:app --host 127.0.0.1 --port 8000
```

Abra <http://127.0.0.1:8000/>. Os recursos HTML, CSS, JavaScript e SVG fazem parte do pacote Python. A documentação da API permanece em `/docs`.

## Fluxo de uso

1. Envie um PDF textual ou cole o texto de uma ata do Copom.
2. Escolha Resumo, Carrossel ou Roteiro de vídeo e o público Iniciante, Médio ou Avançado.
3. Solicite a análise e consulte o conteúdo, a avaliação e as referências.

A interface solicita análise real por padrão e segue a configuração informada pelo servidor. Sem configuração específica de ativação, usa a arquitetura literal com perfil `quality`, que exige a integração Groq. Configurar apenas Gemini não altera esse padrão. Confira o [guia de configuração](configuration.md) e `GET /capabilities`.

Para usar sem credenciais, abra **Opções de demonstração** e selecione o modo de demonstração. A saída é identificada como simulada. Busca por tema também usa um exemplo fictício fixo e não recupera informações sobre o assunto digitado.

## Resultados e referências

- Resumo: síntese, desenvolvimento e implicações.
- Carrossel: abertura, cards ordenados e conclusão.
- Roteiro: abertura, falas, sugestões visuais, pausas e duração estimada.

O resultado preserva o texto avaliado e os estados `approved`, `needs_review` e `failed`, com alertas e referências. O painel de afirmações relaciona cada claim às evidências; cards e falas também oferecem referências por bloco. O modal de evidência consulta o trecho original na API.

Copiar e baixar texto incluem referências e identificador da execução. Aprovação automática não equivale a revisão editorial. Imagens de carrossel e vídeos não são renderizados.

## Histórico e reenvio

**Análises recentes** consulta até 50 execuções do SQLite local, incluindo análises criadas pela API ou por outro navegador. Abrir um item recupera o resultado persistido. Uma execução também pode ser aberta com `/?request_id=<UUID>`.

Cada solicitação recebe uma chave de idempotência antes do envio. Uma falha de conexão preserva essa chave para consulta e reenvio. A API é síncrona e a análise pode demorar vários minutos; o indicador de espera não representa percentual de conclusão. Pedidos ainda sem identificador de execução podem permanecer temporariamente no `localStorage` do navegador.

A exclusão de análises ou fontes é feita pelos comandos do [guia de operação](operations.md).

## Apresentação e acessibilidade

A interface usa a identidade visual da Suno Asset, com logotipo preservado em suas proporções e tipografia sans-serif disponível localmente. O uso desses elementos não concede direitos de marca.

As transições usam CSS nativo e respeitam `prefers-reduced-motion`. Estados, erros e referências permanecem identificados por texto. A navegação entre criação e histórico fica no topo, e o formulário apresenta as etapas de fonte, personalização e análise.

## Testes de navegação

Os testes usam a API local para upload, geração simulada, persistência e evidências. Requisições de inferência real são interceptadas pela suíte; os resultados não medem qualidade semântica de modelos externos.

Em um terminal, use armazenamento separado:

```bash
SUNO_DATA_ROOT=/tmp/suno-browser-tests uv run --frozen --extra dev uvicorn suno_asset.api.app:app --host 127.0.0.1 --port 8011
```

Em outro terminal, com Node.js e npm instalados:

```bash
npm ci --prefix tests/frontend
npm exec --prefix tests/frontend -- playwright install chromium
npm test --prefix tests/frontend
```

`STUDIO_URL` permite configurar outro endereço de teste; `CHROMIUM_PATH` permite usar um executável local de Chromium.
