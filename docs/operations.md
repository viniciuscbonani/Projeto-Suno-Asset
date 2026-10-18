# Operação local

A aplicação mantém documentos e resultados no sistema de arquivos e metadados operacionais em SQLite. `SUNO_DATA_ROOT` define a raiz; o padrão é `data/`. Execute a API em localhost e mantenha o diretório de dados fora do repositório.

## Persistência e recuperação

Identidade da execução, configuração resolvida, idempotência, eventos e estados sobrevivem ao reinício. Consultas verificam a integridade dos artefatos antes de entregar resultados. Ausência ou corrupção de arquivo não deve produzir uma referência aparentemente válida.

Uma interrupção não confirma que o provedor deixou de processar um pedido. A recuperação é conservadora: tentativas incertas não são repetidas automaticamente e suas reservas de consumo permanecem registradas. Consulte o estado pela [API](api.md) antes de reenviar.

O histórico da interface é obtido do servidor local; o `localStorage` do navegador pode guardar solicitações pendentes, mas não substitui a persistência dos resultados.

## Retenção e exclusão

Não há expiração automática de documentos e resultados. A exclusão manual deve ser planejada considerando a perda de verificabilidade das referências. Os comandos não executam inferência.

Confira uma exclusão de análise:

```bash
uv run --frozen --extra dev python -m suno_asset.maintenance --data-root data delete-run UUID-DA-EXECUCAO --dry-run
```

Depois de conferir a prévia, execute:

```bash
uv run --frozen --extra dev python -m suno_asset.maintenance --data-root data delete-run UUID-DA-EXECUCAO --execute
```

A exclusão de análise preserva a fonte e outras análises vinculadas a ela. Para excluir a fonte e os artefatos vinculados, use `delete-source`:

```bash
uv run --frozen --extra dev python -m suno_asset.maintenance --data-root data delete-source UUID-DA-FONTE --dry-run
uv run --frozen --extra dev python -m suno_asset.maintenance --data-root data delete-source UUID-DA-FONTE --execute
```

Exclusões mantêm registro suficiente para impedir replay indevido. Uma exclusão parcialmente concluída pode ser retomada com o mesmo comando.

## Reconciliação e artefatos órfãos

```bash
uv run --frozen --extra dev python -m suno_asset.maintenance --data-root data reconcile
uv run --frozen --extra dev python -m suno_asset.maintenance --data-root data orphans --dry-run
```

A reconciliação verifica estados e integridade. A inspeção de órfãos mostra arquivos sem vínculo válido; use `--execute` somente depois de conferir a prévia. Mantenha cópias consistentes dos metadados e dos artefatos caso precise preservar verificabilidade.

## Logs e privacidade

Eventos operacionais registram identificadores, nó, duração, resultado e contagens. Documento integral, prompts, respostas e credenciais não devem aparecer nesses logs. Arquivos de auditoria podem conter texto e respostas do modelo e devem receber o mesmo tratamento de retenção da fonte.

Credenciais, arquivos de conta, registros de cota, bancos locais e resultados ficam fora da publicação. A retenção do provedor é independente do armazenamento no computador e deve ser conferida antes de inferência real.
