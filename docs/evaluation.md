# Avaliação e testes

A avaliação separa extração da fonte, verificação factual e qualidade do conteúdo. Uma resposta estruturada válida ou uma referência existente não comprova que a afirmação está correta.

## Controles de qualidade

Antes da geração, os fatos são confrontados com a fonte, incluindo números, unidades, períodos, entidade, condições, negações, atribuições e cobertura material. Fatos sem suporte ou verificações obrigatórias inconclusivas impedem a geração aprovada.

Após cada geração ou revisão, o sistema verifica:

- estrutura e limites do formato;
- presença dos fatos obrigatórios;
- sustentação das afirmações nas evidências originais;
- números, citações e localização;
- clareza, profundidade e adequação ao público.

Falhas críticas impedem aprovação independentemente da nota média. O relatório pertence à versão avaliada; alterações posteriores exigem nova avaliação. Revisões são limitadas e não podem substituir uma versão por outra com regressão conhecida.

## Dados de referência

Os dados estáticos usados pelos testes ficam em `tests/fixtures/evaluation/`. Incluem fontes identificadas por URL, hash e páginas, casos positivos e negativos, expectativas e formulários vazios de revisão. Eles não contêm credenciais nem comprovantes preenchidos de conta.

Os rótulos preparados pelo assistente são propostas de gabarito, não avaliação humana independente. Formulários pendentes não representam resultados nem métricas zero. As [anotações de uma fonte de referência](fixtures/copom281-20260916-annotations.md) ajudam a detectar perdas de parsing e recuperação, sem constituir cobertura exaustiva.

Casos usados para ajustar prompts e políticas devem ficar separados dos reservados para avaliação. Se um caso reservado orientar ajustes, sua independência precisa ser revista. Rótulos e respostas esperadas não são enviados ao gerador.

## Execução dos testes

```bash
uv sync --frozen --extra dev
uv run --frozen --extra dev python scripts/prepare_tokenizer.py
uv run --frozen --extra dev pytest -m 'not live'
```

O preparo do tokenizador baixa um vocabulário público, verifica seu hash e o mantém no cache local ignorado pelo Git. Essa etapa requer rede na primeira execução e não acessa credenciais. Com o vocabulário preparado, a suíte usa serviços simulados, documentos sintéticos e falhas controladas, sem inferência externa. Cobre contratos, extração, referências, idempotência, persistência, exclusão, orçamentos e comportamento do workflow.

A conferência opcional com a ata oficial usa um arquivo local cujo hash é validado antes da medição:

```bash
SUNO_COPOM281_PDF=/caminho/Copom281.pdf uv run --frozen --extra dev pytest tests/test_official_fixture.py
```

O PDF não é distribuído no repositório. Consulte a fonte e a identidade esperada nas [anotações](fixtures/copom281-20260916-annotations.md). Sem o arquivo, essa verificação é ignorada pela suíte.

Os testes de planejamento que medem o orçamento dessa ata também são opcionais. Eles usam `SUNO_COPOM281_PDF` ou uma cópia local já obtida e conferem o hash antes de processar. Para incluir todas as verificações da fonte real, execute a suíte com essa variável definida. Os testes gerais de cotas e persistência usam dados sintéticos.

Os testes de navegação estão descritos no [guia da interface](frontend.md). Execuções reais exigem configuração própria e não fazem parte da validação offline.

## Interpretação dos resultados

`approved` significa que a política aplicável passou naquela execução. No modo simulado, isso verifica somente o cenário determinístico. No modo real, a avaliação automática continua sujeita a erros e não substitui revisão editorial independente.

Resultados de testes simulados não demonstram acurácia de modelos, qualidade em todas as atas ou generalização para outros documentos financeiros. Medições publicadas devem identificar amostra, fonte, público, formato, política, método de revisão e limitações. Custos e latências de uma execução isolada não constituem comparação estatística.
