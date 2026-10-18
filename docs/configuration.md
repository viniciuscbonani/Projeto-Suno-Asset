# Configuração

O modo simulado funciona sem credenciais e sem chamadas de inferência. O modo real exige configuração de provedor e é selecionado explicitamente no pedido ou na interface.

O arquivo `.env.example` contém somente nomes de variáveis e valores de exemplo. Para uma instalação nova, copie-o para `.env` e preencha as credenciais localmente. Preserve configurações existentes ao atualizar esse arquivo. `.env`, cotas, dados de conta e resultados não devem ser publicados.

## Armazenamento

`SUNO_DATA_ROOT` define a raiz de armazenamento local; o padrão é `data/`. Exporte essa variável no ambiente antes de iniciar a API. O caminho deve permitir leitura e gravação pela aplicação.

## Seleção de arquitetura e perfil

Consulte `GET /capabilities` para conhecer os padrões efetivos. A arquitetura literal aceita os três públicos e formatos; a arquitetura de compatibilidade aceita texto analítico para iniciantes.

| Perfil | Provedor | Comportamento |
|---|---|---|
| `economy` | Definido por `SUNO_ECONOMY_PROVIDER`, padrão Gemini | Preparação híbrida quando usa Gemini; configuração econômica de modelos quando usa Groq. |
| `single` | Groq | Um modelo configurado para todas as etapas. |
| `quality` | Groq | Modelo de maior capacidade nas etapas. |
| `hybrid` | Groq | Modelo menor na extração e maior nas demais etapas. |

Para usar a preparação híbrida Gemini pela API, informe `architecture: literal-v1`, `model_profile: economy` e `mode: real`. Definir o provedor econômico não altera os demais perfis nem garante que a interface o selecione. Não há troca automática de provedor quando ocorre falha.

## Gemini

Configuração mínima no `.env` ou no ambiente:

```dotenv
SUNO_ECONOMY_PROVIDER=gemini
GEMINI_API_KEY=
GEMINI_MODEL=gemini-3.5-flash-lite
GEMINI_FREE_ONLY=1
```

Preencha a chave somente no ambiente local. O ambiente prevalece sobre `.env`, inclusive para valores explicitamente vazios. O leitor do arquivo não executa substituições de shell.

`GEMINI_MODEL` deve corresponder a um modelo aceito pelo adaptador e disponível na conta. O código mantém a lista de modelos aceitos e a configuração nominal usada para orçamento em `services/gemini.py` e `services/pricing.py`. Aceitação local não comprova disponibilidade no provedor.

`GEMINI_FREE_ONLY=1` declara que a pessoa operadora conferiu a condição gratuita da conta. A chave não comprova plano ou saldo, e a variável não desativa faturamento em uma conta paga.

| Variável opcional | Finalidade |
|---|---|
| `GEMINI_TPM_LIMIT` | Limite de tokens por minuto. |
| `GEMINI_RPM_LIMIT` | Limite de requisições por minuto. |
| `GEMINI_RPD_LIMIT` | Limite de requisições por dia. |
| `GEMINI_TPD_LIMIT` | Limite de tokens por dia. |
| `GEMINI_QUOTA_SCOPE` | Escopo local compartilhado do controle de cota. |
| `GEMINI_FREE_QUOTA_DIR` | Diretório do registro persistente de cota. |

Limites devem ser inteiros positivos conferidos na conta; valores ausentes não representam saldo ilimitado. O controle Gemini é separado do Groq. Consumo desconhecido mantém reserva conservadora, inclusive após reinício. Limitação com prazo informado persiste a espera; sem prazo, exige conferência operacional.

O adaptador usa respostas estruturadas, validadas localmente por schema e pelos contratos de domínio. JSON válido não comprova fidelidade factual. Erros de rede, cota ou indisponibilidade não provocam troca de modelo ou repetição automática. Reparos de conteúdo são tentativas adicionais sujeitas ao orçamento.

## Groq

`GROQ_API_KEY` pode ser lida do ambiente ou do `.env`. As demais opções abaixo devem ser exportadas no ambiente antes de iniciar a aplicação:

```bash
export GROQ_FREE_ONLY=1
export GROQ_FREE_QUOTA_EVIDENCE=/caminho/local/comprovacao-de-cota.json
export GROQ_MODEL=openai/gpt-oss-20b
```

A inferência real exige comprovação recente de plano gratuito e limites da conta, com identificação da conta ou vínculo à credencial e um arquivo de evidência conferido por hash. Um caminho preenchido, isoladamente, não satisfaz a validação. O contrato é definido por `validate_free_quota_evidence` em `services/quota_ledger.py`; não use um relatório de outra conta ou credencial. Esses arquivos são locais e não integram a documentação pública.

`GROQ_MODEL` seleciona o modelo do perfil `single`. Os perfis `quality`, `hybrid` e `economy` definem modelos por etapa. `GROQ_TPM_LIMIT`, `GROQ_RPM_LIMIT`, `GROQ_RPD_LIMIT` e `GROQ_TPD_LIMIT` configuram limites sujeitos às guardas e à evidência da conta. `GROQ_FREE_QUOTA_DB` permite definir o arquivo SQLite do controle de cota.

Prepare o vocabulário local antes de usar a contagem de tokens Groq:

```bash
uv run --frozen --extra dev python scripts/prepare_tokenizer.py
```

O comando baixa apenas um vocabulário público e confere seu hash. Sem esse arquivo, o planejamento usa uma estimativa conservadora por bytes, que pode bloquear pedidos maiores. O orçamento não autoriza cortar fatos obrigatórios para fazer a solicitação caber.

## Dados enviados e falhas

O arquivo original permanece no computador. No modo real, trechos de texto, fatos, instruções, rascunhos, avaliações e contexto educacional necessários podem ser enviados ao provedor. Confira as condições de privacidade e retenção da conta antes de processar documentos. A configuração local não comprova a política efetiva do serviço externo.

Contadores reportados, estimativas e uso desconhecido são distintos. Custos nominais são controles de admissão e não comprovam cobrança ou gratuidade. Não apague registros de cota para repetir uma tentativa cujo consumo é desconhecido.

Uma falha após o envio pode ter consumido cota. Consulte o estado da execução e reutilize a chave de idempotência, conforme o [guia da API](api.md).
