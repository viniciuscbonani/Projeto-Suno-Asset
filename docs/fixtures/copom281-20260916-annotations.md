# Anotações de referência — ata da 281ª reunião do Copom

Fonte: [página oficial](https://www.bcb.gov.br/publicacoes/atascopom/16092026) e [PDF oficial](https://www.bcb.gov.br/content/copom/atascopom/Copom281-not20260916281.pdf). Reunião de 15–16/09/2026; publicação em 22/09/2026, conforme a página do BC. PDF consultado em 25/09/2026: 6 páginas, 401.502 bytes, SHA-256 `f03423dc25625aa14ae29c10fdacf7b4665b6714ab0667c0dc86a6f77c43665c`.

Esta é uma amostra manual de referência para detectar perdas de **parsing e recuperação**. As formulações abaixo são paráfrases; a ata é a fonte de verdade. A página indicada é a **página física do PDF** (nesta edição coincide com o número impresso). O item é o parágrafo numerado na ata, não um offset estável. A seleção não é um roteiro de geração: não alimentar o gerador com estes rótulos nem tratar a lista como cobertura exaustiva. Se o hash mudar, revisar as anotações antes de reutilizá-las.

## Fatos críticos

| ID | Localização | Fato a preservar | Condição ou ressalva indispensável |
|---|---|---|---|
| C01 | p. 3, item 2 | A atividade doméstica mostra moderação gradual, sobretudo em setores mais cíclicos; inflação cheia e medidas subjacentes desaceleraram. | A atividade ainda é resiliente e o mercado de trabalho segue aquecido; a inflação está abaixo do limite superior da tolerância, mas acima da meta. Não converter moderação em recessão nem inflação abaixo do limite em inflação dentro da meta central. |
| C02 | p. 3, item 3 | A pesquisa Focus aponta expectativas de inflação de **4,9% para 2026** e **4,3% para 2027**, ambas acima da meta. | São expectativas de mercado, não projeções do Copom. O **3,2% para o 1º trimestre de 2028** no mesmo item é projeção do Copom no cenário de referência e corresponde ao horizonte relevante. |
| C03 | p. 4, item 10 | O Comitê avalia que expectativas desancoradas exigem restrição monetária maior e por mais tempo. | É avaliação para o ambiente descrito na ata; não significa anúncio de duração ou trajetória futura da Selic. |
| C04 | p. 4, itens 11–12 | O cenário de referência projeta IPCA acumulado em quatro trimestres de **5,2% em 2026**, **3,9% em 2027** e **3,2% no 1º trimestre de 2028**. | Preservar os horizontes, a natureza de projeção e as hipóteses do item 11: trajetória de juros da Focus, câmbio inicial de R$ 5,15/US$ com PPC, petróleo conforme regra descrita e bandeira amarela em dezembro de 2026. Não apresentar projeções como resultados observados. |
| C05 | p. 4–5, item 13 | O balanço de riscos para inflação é mais elevado que o usual e tem assimetria de alta. | O item contém **riscos de alta e de baixa** e prossegue na p. 5; preservar ao menos a existência dos dois lados. São cenários condicionais, não fatos consumados. |
| C06 | p. 5, item 15 | O Comitê considera que há sinais graduais de transmissão da política contracionista para a atividade, mas que a inflação ainda pressionada pela demanda exige caráter restritivo. | Não afirmar que a desinflação está concluída ou que a restrição deixou de ser necessária. |
| C07 | p. 5, item 16 | O Copom diferencia efeitos primários de choques de oferta de possíveis efeitos de segunda ordem. | Diz que não deve reagir aos efeitos primários, mas deve monitorar e reagir com firmeza **se** os efeitos de segunda ordem se materializarem. |
| C08 | p. 5, item 17 | A magnitude do ciclo de calibração será ajustada segundo a evolução do cenário. | Não extrair uma promessa de cortes adicionais ou de tamanho fixo; o objetivo declarado é manter restrição adequada para convergir à meta. |
| C09 | p. 5, item 18 | O Copom decidiu reduzir a Selic **para 13,75% ao ano**. | A ata informa o nível final, não a magnitude numérica da redução no próprio item. Não atribuir `0,25 p.p.` à ata sem fonte adicional ou cálculo explicitamente identificado. |
| C10 | p. 5, item 19 | O Comitê indica serenidade e cautela e acompanhamento de novas informações. | Relaciona essa postura à incerteza elevada, expectativas desancoradas e riscos em torno do cenário base; não transformar em compromisso com decisão futura. |
| C11 | p. 5, item 20; p. 2, membros | O item 20 lista os sete membros que votaram pela decisão. | Preservar a atribuição ao colegiado; evitar inferir voto divergente ou motivação individual. |
| C12 | p. 6, Tabela 1 | A tabela confirma a linha do **IPCA: 5,2 / 3,9 / 3,2** para 2026 / 2027 / 1º tri. 2028. | Não confundir a linha do IPCA com **IPCA livres: 5,2 / 4,1 / 3,2** ou **IPCA administrados: 5,3 / 3,5 / 3,2**; preservar associação entre coluna, linha, período e unidade (% acumulado em quatro trimestres). |

## Contexto de suporte e armadilhas de layout

| ID | Localização | Verificação |
|---|---|---|
| S01 | p. 3, item 5 | O PIB do 2º trimestre de 2026 confirmou desaceleração, mais intensa em atividades e componentes da demanda sensíveis ao ciclo. Trata-se da leitura descrita pelo Comitê, sem taxa numérica nesse parágrafo. |
| S02 | p. 3–4, item 8 | O parágrafo sobre política fiscal começa na p. 3 e termina na p. 4. A extração não deve separar o início de seus qualificadores finais nem deslocar o início do item 9. |
| S03 | p. 3, nota 1 | Salvo indicação contrária, a atualização compara mudanças desde a reunião de agosto (280ª). A nota deve ser reconhecida como nota, não como início de novo item numerado. |
| S04 | p. 4, nota 2 | O câmbio inicial do cenário de referência deriva de procedimento de média e arredondamento descrito na nota; não é cotação atual. |
| S05 | p. 1–2 | Capa, cabeçalhos, lista de participantes, rodapés e numeração não devem ser confundidos com o corpo da análise. A data da publicação está na página oficial, não na capa do PDF. |

## Como usar na verificação da extração

- Conferir que cada página tenha texto útil, mas não tomar contagem de caracteres como prova de completude.
- Para C01–C12, conferir manualmente que a extração conserva **números, entidade, período, negação, condição e atribuição**. Busca por palavras isoladas não basta.
- Conferir as quebras p. 3–4 (S02) e p. 4–5 (C05), as notas e a associação de células da Tabela 1. Registrar perdas e erro por página.
- O parser deve preservar página física e offsets no texto extraído versionado. Itens numerados e estrutura de tabela são referências humanas nesta amostra, não formato obrigatório para toda ata futura.
- Esta amostra testa uma edição. Validar outras atas com layouts diferentes antes de afirmar suporte geral; se a estrutura for ambígua ou faltar conteúdo material, encaminhar para revisão em vez de gerar uma saída aparentemente completa.

## Limites da conferência

A presença de marcadores de C01–C12 não prova, isoladamente, preservação semântica nem cobertura de conteúdo visual. Confira contexto, continuidade e associação de valores conforme os [limites e diagnósticos de ingestão](../ingestion.md).
