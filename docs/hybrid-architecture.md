# Arquitetura híbrida econômica

O perfil `economy` na arquitetura literal combina regras locais, extração semântica, verificação compartilhada e geração de um único formato por solicitação. O provedor econômico é configurável; o caminho híbrido descrito aqui usa Gemini. Consulte [configuração](configuration.md) para selecionar a integração e [API](api.md) para enviar pedidos explícitos.

## Preparação da fonte

1. O parser preserva páginas, offsets, texto, tabelas, notas e continuidade. Regras conservadoras reconhecem decisão monetária, orientação futura explícita, votação nominal, data e padrões tabulares completos. Valores repetidos mantêm associação com linha, coluna e período.
2. Fatos obtidos por regra recebem proveniência, texto literal, offsets e referências de suporte. Hipóteses não são convertidas em decisões e uma lista de votos não implica unanimidade.
3. A extração semântica recebe o conteúdo restante agrupado por seção, com catálogo de textos e seletores para evitar repetição. IDs, offsets e literalidade são atribuídos ou conferidos localmente.
4. A verificação confronta a fonte integral com todos os fatos, incluindo os obtidos por regras. Ela deve preservar decisão, orientação futura, números, condições, negações, modalidade e atribuição. Pendência obrigatória impede aprovação e cache.
5. Problemas localizados permitem um reparo dirigido, seguido de nova verificação, dentro do orçamento. Falhas de transporte com uso desconhecido não provocam repetição automática.

Parágrafos que atravessam páginas mantêm todas as partes do contexto. Conferências lexicais e numéricas são controles auxiliares; não equivalem a uma medida de cobertura semântica.

## Cache e seleção editorial

A chave do cache inclui fonte, texto preparado, parser, segmentação, regras, prompts, schemas, política, provedor, modelos, configuração e identidade do código. A trava por chave serializa a preparação entre processos.

Na leitura, identidade, literais, offsets e pareceres são revalidados. Entradas rejeitadas são separadas antes da reconstrução. Somente fichas aprovadas entram no cache; outro formato pode reutilizar a mesma preparação.

A seleção exige decisão monetária inequívoca e inclui orientações futuras explícitas verificadas, com suas dependências. Núcleo ausente ou ambíguo produz `needs_review`. Os demais fatos seguem o orçamento editorial. O manifesto distingue fatos selecionados, obrigatórios, dependências e omissões editoriais. A ficha cobre a fonte; a peça cobre a seleção e seus qualificadores necessários.

## Geração e revisão

Uma chamada gera somente o formato solicitado, usando fatos selecionados e apoios necessários:

- texto analítico: até 500 palavras;
- carrossel híbrido econômico: seis telas, com abertura, quatro cards e conclusão;
- roteiro: até 60 segundos, considerando 130 palavras por minuto e pausas.

Controles locais conferem estrutura, referências, números e apresentação. A revisão semântica examina as afirmações e os fatos selecionados contra a fonte, incluindo condições, negações e atribuição. Omissões críticas e verificações inconclusivas impedem aprovação.

Correções de geração e revisões editoriais são limitadas e cada nova saída passa por avaliação. Falta de orçamento ou erro preserva a última versão válida avaliada, acompanhada dos problemas encontrados. O empacotamento final não altera texto ou referências.

## Chamadas e orçamento

O caminho sem reparos é planejado com as seguintes contagens:

| Caminho | Chamadas de inferência |
|---|---:|
| Regras e seleção locais | 0 |
| Extração e verificação da ficha | 2 |
| Geração e revisão do formato | 2 |
| Primeira solicitação | 4 |
| Solicitação com ficha aprovada em cache | 2 |
| Reenvio idempotente de resultado concluído | 0 |

São contagens do fluxo normal, não garantia de consumo, latência ou qualidade de uma execução real. Reparos e revisões consomem tentativas adicionais. O orçamento limita tokens, chamadas, duração e custo nominal; a cota do provedor pode ser mais restritiva.

Uso reportado e reservas de consumo desconhecido são registrados separadamente. Uma falha incerta conserva a reserva. Insuficiência de orçamento bloqueia o processamento sem cortar silenciosamente fatos obrigatórios.

## Identidade e rastreabilidade

A API resolve padrões antes de aplicar idempotência. A configuração efetiva participa da identidade da execução e do cache. Pedidos equivalentes recuperam o mesmo resultado; mudanças incompatíveis geram conflito em vez de reutilizar silenciosamente a chave.

Os manifestos registram seleção, cache, provedor, modelos, política e consumo disponível. Os artefatos locais podem conter fonte preparada, ficha factual, planos, tentativas e respostas brutas. Eles exigem os mesmos cuidados de retenção do documento e ficam fora do repositório público.

O método de avaliação e as limitações da aprovação automática estão no [guia de avaliação](evaluation.md).
