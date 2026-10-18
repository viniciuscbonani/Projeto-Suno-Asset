# API local

A API é servida por FastAPI em `http://127.0.0.1:8000`. Os schemas completos estão em `/docs` e `/openapi.json`. A aplicação é destinada a uso local, sem autenticação multiusuário.

## Operações

| Método e rota | Finalidade |
|---|---|
| `GET /health` | Verificar disponibilidade. |
| `GET /capabilities` | Consultar combinações, padrões e configuração efetiva. |
| `POST /documents` | Registrar PDF ou texto colado. |
| `POST /generations` | Executar geração síncrona. |
| `GET /generations?limit=50` | Listar análises recentes; aceita de 1 a 100 itens. |
| `GET /generations/{request_id}` | Recuperar resultado persistido. |
| `GET /generations/{request_id}/status` | Consultar estado operacional. |
| `GET /generations/{request_id}/evidence/{index}` | Recuperar evidência vinculada ao resultado. |

## Documento

Envie `application/pdf` com `X-Filename` e bytes do arquivo, ou `application/json` com `text`. A resposta de criação retorna `source_id`. Tipos, limites e comandos estão em [ingestão](ingestion.md).

## Geração

Exemplo sem inferência externa:

```bash
curl -X POST http://127.0.0.1:8000/generations \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: analise-exemplo-001' \
  -d '{"source_id":"UUID-DO-DOCUMENTO","architecture":"literal-v1","audience":"intermediate","output_format":"informational_carousel","max_refinements":2,"mode":"simulated"}'
```

| Campo | Valores ou finalidade |
|---|---|
| `source_id` | UUID retornado pelo registro do documento. |
| `audience` | `beginner`, `intermediate`, `advanced`. |
| `output_format` | `analytical_text`, `informational_carousel`, `short_video_script`. |
| `mode` | `simulated` ou `real`; padrão `simulated`. |
| `architecture` | `literal-v1` para a matriz completa; `current` para compatibilidade. |
| `model_profile` | `single`, `quality`, `hybrid`, `economy`; resolução conforme capacidades. |
| `editorial_focus` | Foco opcional que não elimina fatos centrais nem ressalvas. |
| `max_refinements` | Inteiro de 0 a 2. |

Para usar Gemini com preparação híbrida, configure a integração e envie explicitamente `architecture: literal-v1`, `model_profile: economy` e `mode: real`. O perfil `economy` depende de `SUNO_ECONOMY_PROVIDER`. Veja [configuração](configuration.md).

Arquitetura e perfil omitidos seguem os padrões efetivos do servidor. Consulte `/capabilities` antes de pressupor uma combinação. A arquitetura `current` aceita somente `beginner` e `analytical_text`. Pedidos incompatíveis são recusados sem substituição silenciosa.

## Resultado e evidências

A resposta contém `request_id`, modo, estado, motivo, conteúdo quando disponível, avaliação, referências, diagnóstico de extração, erros e histórico da execução.

| Estado do resultado | Significado |
|---|---|
| `approved` | Verificações obrigatórias aplicáveis passaram para a versão entregue. |
| `needs_review` | Há conteúdo inspecionável, com problemas ou verificações inconclusivas. |
| `failed` | Não foi possível entregar conteúdo válido inspecionável. |

No modo simulado, a aprovação se limita aos controles determinísticos do cenário. No modo real, ela também depende da avaliação semântica configurada e não dispensa revisão editorial.

```bash
curl http://127.0.0.1:8000/generations/UUID-DA-EXECUCAO
curl http://127.0.0.1:8000/generations/UUID-DA-EXECUCAO/status
curl http://127.0.0.1:8000/generations/UUID-DA-EXECUCAO/evidence/0
```

O índice de evidência começa em zero e corresponde à posição na lista `references` daquela execução. A API verifica o vínculo antes de entregar texto e localização.

## Idempotência e falhas

Envie `Idempotency-Key` para identificar uma solicitação e reutilize a chave em caso de perda de conexão. Pedido equivalente recupera o resultado persistido ou acompanha a execução em andamento. Usar a mesma chave para configuração incompatível retorna conflito.

A geração é síncrona e pode demorar vários minutos. Desconexão ou timeout do cliente não confirma cancelamento nem ausência de consumo no provedor. Consulte o estado antes de iniciar outro pedido. A persistência local não garante execução exatamente uma vez no serviço externo.

Falhas esperadas retornam códigos estruturados. Entre as respostas HTTP estão 413 para limites de entrada, 422 para contrato inválido ou capacidade não suportada, 409 para conflitos, 410 para recurso excluído, 429 para limitação de cota Gemini e 503 para indisponibilidade. Após a geração, examine também o estado no envelope: uma resposta HTTP bem-sucedida pode conter `needs_review` ou `failed`.
