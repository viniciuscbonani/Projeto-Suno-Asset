export const formats = {
  analytical_text: { label: 'Resumo', description: 'O essencial, com contexto e implicações.', icon: 'text' },
  informational_carousel: { label: 'Carrossel', description: 'Uma narrativa organizada card a card.', icon: 'cards' },
  short_video_script: { label: 'Roteiro de vídeo', description: 'Falas e apoio visual para até 60 segundos.', icon: 'video' },
};
export const audiences = {
  beginner: { label: 'Iniciante', description: 'Linguagem simples e conceitos explicados.' },
  intermediate: { label: 'Médio', description: 'Mais contexto para quem já conhece o básico.' },
  advanced: { label: 'Avançado', description: 'Profundidade e linguagem técnica.' },
};
export const statuses = { approved: 'Aprovado pela avaliação', needs_review: 'Revisão necessária', failed: 'Análise não concluída', processing: 'Em andamento' };
// A fixed fictional example, never represented as scraped material or current data.
export const demoSource = '1. A atividade econômica desacelerou.\n2. A inflação permaneceu elevada.\n3. O consumo diminuiu.\n4. Há riscos no cenário externo.\n5. Não há antecipação de cortes de juros.';
export function escapeHTML(value = '') {
  return String(value).replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
}
export function generationPayload(sourceId, audience, outputFormat, simulated = false) {
  if (!audiences[audience] || !formats[outputFormat]) throw new Error('Seleção de conteúdo inválida.');
  return { source_id: sourceId, architecture: 'literal-v1', audience, output_format: outputFormat,
    mode: simulated ? 'simulated' : 'real', ...(simulated ? {} : { model_profile: 'economy' }) };
}
export function contentSections(content) {
  if (!content) return [];
  const block = (label, value) => ({ label, text: value.text, notes: value.educational_notes || [],
    claimIds: value.claim_ids || [], pause: value.pause_seconds });
  if (content.output_format === 'analytical_text') return [
    {label:'Síntese', text:content.synthesis}, {label:'Desenvolvimento', text:content.development},
    {label:'Implicações', text:content.implications},
  ];
  if (content.output_format === 'informational_carousel') return [
    block('Abertura', content.hook), ...content.cards.map(c => block(`Card ${String(c.order).padStart(2, '0')}`, c)),
    block('Conclusão', content.conclusion),
  ];
  return [block('Abertura', content.hook), ...content.spoken_blocks.map((c, i) => block(`Fala ${i + 1}`, c)),
    ...content.visual_support.map((c, i) => block(`Apoio visual ${i + 1}`, c))];
}
export function exportText(result) {
  const content = result.content;
  return [content?.title || 'Análise', `${statuses[result.status] || result.status}${result.mode === 'simulated' ? ' · DEMONSTRAÇÃO SIMULADA' : ''}`,
    content ? `${formats[content.output_format].label} · ${audiences[content.audience].label}` : '',
    ...contentSections(content).map(s => `${s.label}\n${s.text}${s.notes?.length ? '\n' + s.notes.join('\n') : ''}${s.pause ? `\nPausa: ${s.pause}s` : ''}`),
    content?.estimated_duration_seconds ? `Duração estimada: ${Math.ceil(content.estimated_duration_seconds)} segundos` : '',
    'Referências', ...(result.references || []).map((ref, i) => `[${i + 1}] ${ref.page ? `Página ${ref.page}` : 'Texto enviado'} · caracteres ${ref.start}–${ref.end} · ${ref.chunk_id}`),
    `Execução: ${result.request_id}`, result.reason,
  ].filter(Boolean).join('\n\n');
}
const errors = {
  groq_key_missing: 'A análise com IA ainda precisa ser configurada no servidor. Sua fonte foi mantida; tente novamente após a configuração.',
  groq_config_invalid: 'A configuração da análise com IA está indisponível. Sua fonte foi mantida.',
  free_quota_unverified: 'A cota gratuita da IA precisa ser validada no servidor antes de iniciar a análise.',
  generation_unavailable: 'Não foi possível concluir a análise agora. Tente consultar novamente esta mesma solicitação.',
  quota_exhausted: 'A cota disponível da IA foi atingida. Tente novamente quando houver disponibilidade.',
  size_limit: 'O arquivo ultrapassa o limite de 10 MB.',
  text_limit: 'O texto ultrapassa o limite permitido pelo servidor.',
  invalid_pdf: 'Não foi possível ler este PDF. Envie um PDF textual válido.',
  unsupported_media_type: 'Envie um arquivo PDF textual.',
  resource_deleted: 'Esta análise ou fonte foi removida do servidor.',
  artifact_missing: 'Esta análise não está mais disponível no servidor.',
  idempotency_conflict: 'Esta solicitação já está associada a outra configuração. Inicie uma nova análise.',
};
export async function api(path, options = {}) {
  let response;
  try { response = await fetch(path, options); }
  catch { throw new Error('Não foi possível conectar ao servidor. Verifique a conexão e tente novamente.'); }
  let data;
  try { data = await response.json(); }
  catch { throw new Error('O servidor retornou uma resposta inesperada. Tente novamente.'); }
  if (!response.ok) {
    const code = data.detail?.error || `HTTP ${response.status}`;
    const err = new Error(errors[code] || `Não foi possível concluir a solicitação (${code}). Verifique a fonte e tente novamente.`);
    err.requestId = data.detail?.request_id;
    err.code = code;
    throw err;
  }
  return data;
}
