import { formats, audiences, statuses, demoSource, escapeHTML as esc, generationPayload, contentSections, exportText, api } from './core.mjs';

const $ = selector => document.querySelector(selector);
const icons = {
  plus: '<path d="M12 5v14M5 12h14"/>', arrow: '<path d="M5 12h14m-5-5 5 5-5 5"/>',
  back: '<path d="M19 12H5m5-5-5 5 5 5"/>', upload: '<path d="M12 16V3m-5 5 5-5 5 5M4 15v5h16v-5"/>',
  file: '<path d="M14 3H5v18h14V8l-5-5Zm0 0v5h5M8 12h8M8 16h5"/>',
  search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  text: '<path d="M4 5h16M4 10h16M4 15h16M4 20h10"/>',
  cards: '<rect x="6" y="3" width="14" height="15" rx="2"/><path d="M3 7v14h13M10 8h6M10 12h4"/>',
  video: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="m10 9 5 3-5 3Z"/>',
  check: '<path d="m5 12 4 4L19 6"/>', close: '<path d="m6 6 12 12M6 18 18 6"/>',
  sparkle: '<path d="m12 3 2.5 6.5L21 12l-6.5 2.5L12 21l-2.5-6.5L3 12l6.5-2.5Z"/>',
  shield: '<path d="m12 3 8 3v6c0 5-8 9-8 9s-8-4-8-9V6Z"/><path d="m8 12 3 3 5-6"/>',
  copy: '<rect x="8" y="8" width="12" height="13" rx="2"/><path d="M16 8V3H3v13h5"/>',
  download: '<path d="M12 3v13m-5-5 5 5 5-5M4 17v4h16v-4"/>',
  link: '<path d="m9 15 6-6m-7 2-3 3a4 4 0 0 0 6 6l3-3m-4-10 3-3a4 4 0 0 1 6 6l-3 3"/>',
};
const icon = name => `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.file}</svg>`;
const key = 'suno-studio-history-v1';
let storageWarning = false;
function readHistory() {
  try {
    const data = JSON.parse(localStorage.getItem(key) || '[]');
    return Array.isArray(data) ? data.filter(r => r && typeof r.key === 'string' && typeof r.name === 'string' && r.payload && formats[r.payload.output_format] && audiences[r.payload.audience]).slice(0, 50) : [];
  } catch { return []; }
}
let history = readHistory();
const state = { view: 'create', step: 1, input: 'document', file: null, text: '', topic: '', source: null,
  audience: 'beginner', format: 'analytical_text', demo: false, busy: false, error: '', result: null, pending: null, sourceName: '', historyQuery: '', readOnlyResult:false, activeNav:'create' };
let toastTimer;
function toast(message) {
  $('#toast').textContent = message; $('#toast').hidden = false;
  clearTimeout(toastTimer); toastTimer = setTimeout(() => { $('#toast').hidden = true; }, 5000);
}
function persist() {
  try { localStorage.setItem(key, JSON.stringify(history)); }
  catch { if (!storageWarning) { storageWarning = true; toast('O navegador não permite salvar o histórico. Mantenha esta página aberta durante a análise.'); } }
}
function remember(entry) {
  history = [entry, ...history.filter(r => r.key !== entry.key)].slice(0, 50); persist();
}
async function refreshHistory() {
  try {
    const response = await api('/generations?limit=50');
    const remote = response.items.map(item => ({key:item.request_id,name:item.name,date:item.date,
      input:'document',payload:{source_id:item.source_id,audience:item.audience,output_format:item.output_format,mode:item.mode},
      requestId:item.request_id,status:item.status,readOnly:true}));
    const pending = history.filter(entry => !entry.requestId);
    history = [...remote,...pending].slice(0,50);
    persist();
  } catch (e) { state.error = `Não foi possível atualizar o histórico salvo: ${e.message}`; }
}
function humanDate(date) { return new Date(date).toLocaleDateString('pt-BR', {day:'2-digit', month:'short'}); }
function sourceName() { return state.sourceName || (state.input === 'topic' ? state.topic : state.file?.name || 'Texto da ata'); }
function sourceLabel() { return state.input === 'topic' ? 'Prévia de pesquisa' : state.input === 'text' ? 'Texto colado' : 'Documento PDF'; }
function reset() {
  Object.assign(state, { view:'create', step:1, input:'document', file:null, text:'', topic:'', source:null, sourceName:'', demo:false, result:null, pending:null, error:'', readOnlyResult:false, activeNav:'create' }); render();
}
function errorBox() { return state.error ? `<div class="notice error" role="alert">${icon('shield')}<div>${esc(state.error)}</div></div>` : ''; }
function stepper() {
  return `<ol class="stepper" aria-label="Etapas da análise">${['Sua fonte','Personalização','Resumo'].map((s, i) => `<li class="${state.step === i + 1 ? 'current' : state.step > i + 1 ? 'complete' : ''}" ${state.step === i + 1 ? 'aria-current="step"' : ''}><span aria-hidden="true">${icon(['upload','sparkle','file'][i])}</span><small>${s}</small></li>`).join('')}</ol>`;
}
function shell(content) {
  return `<div class="workspace"><header class="topbar">
    <a class="brand" href="/" aria-label="Suno Asset — nova análise"><img src="/studio/assets/suno-asset.svg" width="197" height="25" alt="Suno Asset"></a>
    <nav class="navigation-pill ${state.activeNav === 'history' || state.view === 'history' ? 'history-active' : ''} ${state.pillAnimation ? `animate-${state.pillAnimation}` : ''}" aria-label="Navegação principal">
      <button class="nav-item ${state.activeNav !== 'history' && state.view !== 'history' ? 'active' : ''}" aria-pressed="${state.activeNav !== 'history' && state.view !== 'history'}" data-action="create" ${state.busy ? 'disabled' : ''}>${icon('sparkle')} Criar conteúdo</button>
      <button class="nav-item ${state.activeNav === 'history' || state.view === 'history' ? 'active' : ''}" aria-pressed="${state.activeNav === 'history' || state.view === 'history'}" data-action="history" ${state.busy ? 'disabled' : ''}>${icon('clock')} Análises recentes</button>
    </nav>
  </header><main id="main" class="${state.animateStep ? 'step-forward' : ''}" tabindex="-1">${content}</main></div>`;
}
function sourceInput() {
  if (state.input === 'document') return `<input type="file" id="file-input" accept=".pdf,application/pdf" class="sr-only" aria-label="Selecionar documento PDF">
    <button class="dropzone ${state.file || state.source ? 'has-file' : ''}" id="dropzone" type="button" data-action="choose-file" ${state.busy ? 'disabled' : ''}>
      <span class="upload-symbol">${icon(state.file || state.source ? 'file' : 'upload')}</span>
      <strong>${state.file || state.source ? esc(sourceName()) : 'Arraste seu documento até aqui'}</strong>
      <span>${state.file ? `${(state.file.size/1024).toFixed(0)} KB · Clique para trocar o arquivo` : state.source ? 'Fonte já enviada · Clique para trocar o arquivo' : 'ou <em>escolha um arquivo</em> no computador'}</span>
      <small>PDF textual · Até 10 MB · Atas do Copom</small>
    </button>`;
  if (state.input === 'text') return `<div class="text-input"><label for="source-text">Cole o texto da ata do Copom</label><textarea id="source-text" placeholder="Insira o texto completo que você quer analisar…" maxlength="100000" ${state.busy ? 'disabled' : ''}>${esc(state.text)}</textarea><small>O conteúdo enviado será a fonte da análise.</small></div>`;
  return `<div class="text-input topic-input"><label for="topic">Qual tema você quer explorar?</label><textarea id="topic" rows="2" maxlength="200" placeholder="Ex.: O cenário de juros no Brasil" ${state.busy ? 'disabled' : ''}>${esc(state.topic)}</textarea><div class="topic-suggestions">${['Juros no Brasil','Inflação e consumo','Cenário econômico'].map(s => `<button type="button" data-topic="${s}">${s} ${icon('arrow')}</button>`).join('')}</div><p class="mock-note"><span class="tag">PRÉVIA</span> Busca por tema em demonstração. Usaremos um texto fictício sobre economia para mostrar o fluxo; nenhuma pesquisa na web será feita.</p></div>`;
}
function sourceView() {
  return `<section class="create-page source-page">${stepper()}<div class="hero"><h1>Da informação à <span>compreensão.</span></h1><p>Um documento. Diferentes maneiras de contar a história. <br>Comece com uma fonte e deixe o conteúdo ganhar forma.</p></div>
    <form id="source-form" class="source-card"><div class="input-tabs" role="group" aria-label="Tipo de fonte">${[['document','file','Adicionar documento'],['text','text','Colar texto']].map(([id, ico, label]) => `<button type="button" data-input="${id}" aria-pressed="${state.input === id}" class="${state.input === id ? 'selected' : ''}" ${state.busy ? 'disabled' : ''}>${icon(ico)}${label}</button>`).join('')}</div><div id="source-input-panel">${sourceInput()}</div><div id="source-errors">${errorBox()}</div><div class="source-card-footer"><span>${icon('shield')} Conteúdo conectado à fonte</span><button class="primary" type="submit" ${!canContinue() || state.busy ? 'disabled' : ''}>${state.busy ? 'Enviando…' : 'Continuar'} ${icon('arrow')}</button></div></form>
    </section>`;
}
function sourceChip() {
  return `<div class="source-chip">${icon(state.input === 'topic' ? 'search' : 'file')}<div><small>${sourceLabel()}</small><strong>${esc(sourceName())}</strong></div><span class="source-check">${icon('check')}</span></div>`;
}
function formatPreview(id) {
  if (id === 'informational_carousel') return '<div class="mini-cards"><i><b>01</b><span></span><span></span></i><i><b>02</b><span></span><span></span></i><i><b>03</b><span></span><span></span></i></div>';
  if (id === 'short_video_script') return `<div class="mini-video"><div>${icon('video')}</div><span></span><span></span><b>00:60</b></div>`;
  return '<div class="mini-document"><b></b><span></span><span></span><span></span><i></i><span></span></div>';
}
function configureView() {
  return `<section class="create-page configure-page">${stepper()}<div class="section-heading"><span class="eyebrow">DO SEU JEITO</span><h1>Como vamos contar <br><span>essa história?</span></h1><p>Escolha o formato e a profundidade. A fonte continua a mesma.</p></div>${sourceChip()}
    <form id="configure-form"><fieldset><legend><span>01</span> Qual é o formato do conteúdo?</legend><div class="format-grid">${Object.entries(formats).map(([id, f]) => `<label class="format-option"><input type="radio" name="format" value="${id}" ${state.format === id ? 'checked' : ''}><span class="option-body"><span class="radio-dot"></span><span class="format-preview">${formatPreview(id)}</span><strong>${f.label}</strong><small>${f.description}</small></span></label>`).join('')}</div></fieldset>
    <fieldset><legend><span>02</span> Para quem estamos falando?</legend><div class="audience-grid">${Object.entries(audiences).map(([id, a], i) => `<label class="audience-option"><input type="radio" name="audience" value="${id}" ${state.audience === id ? 'checked' : ''}><span class="option-body"><span class="level-bars" aria-hidden="true">${[0,1,2].map(n => `<i class="${n<=i ? 'on' : ''}"></i>`).join('')}</span><strong>${a.label}</strong><small>${a.description}</small><span class="radio-dot"></span></span></label>`).join('')}</div></fieldset>
    ${state.input === 'topic' ? '<div class="notice"><span class="tag">PRÉVIA</span><p>Esta pesquisa usa um exemplo fictício fixo sobre economia. O conteúdo gerado será uma demonstração, independentemente do tema informado.</p></div>' : ''}
    ${errorBox()}<div class="form-actions">${state.input === 'topic' ? '' : '<details class="demo-options"><summary>Opções de demonstração</summary><label><input type="checkbox" id="demo-mode" '+(state.demo ? 'checked' : '')+'> Testar com processamento simulado, sem chamar a IA</label><p>A análise real é o padrão para documentos. A demonstração valida somente o fluxo.</p></details>'}<button class="primary" type="submit">${icon('sparkle')} ${state.demo || state.input === 'topic' ? 'Gerar demonstração' : 'Gerar análise'} ${icon('arrow')}</button></div></form></section>`;
}
function loadingView() {
  return `<section class="create-page loading-page">${stepper()}<div class="processing-symbol">${icon('sparkle')}<span></span><span></span></div><span class="eyebrow">CONECTANDO AS IDEIAS</span><h1>Da sua fonte<br>ao seu conteúdo.</h1><p>A análise está em andamento. A fonte será processada,<br>adaptada ao seu público e verificada.</p><div class="processing-line" role="progressbar" aria-label="Análise em andamento"></div><div class="processing-config"><span>${formats[state.format].label}</span><span>${audiences[state.audience].label}</span><span>${state.pending?.payload.mode === 'simulated' ? 'Demonstração' : 'Análise com IA'}</span></div>${sourceChip()}<p class="processing-note">A análise com IA pode levar alguns minutos.<br>Se fechar a página, consulte a solicitação em Análises recentes.</p></section>`;
}
function blockRefs(section, content, result) {
  const claims = (content.claims || []).filter(c => section.claimIds?.includes(c.claim_id));
  const indexes = new Set();
  for (const c of claims) for (const ref of c.evidence_refs || []) {
    const index = result.references.findIndex(r => r.chunk_id === ref.chunk_id && r.start === ref.start && r.end === ref.end && r.document_id === ref.document_id);
    if (index >= 0) indexes.add(index);
  }
  return [...indexes].map(i => `<button class="citation" data-evidence="${i}" aria-label="Ver referência ${i+1}">[${i+1}]</button>`).join('');
}
function carouselGlossary(sections) {
  const notes = [...new Set(sections.flatMap(section => section.notes || []).map(note => note.trim()).filter(Boolean))];
  if (!notes.length) return '';
  return `<details class="carousel-glossary"><summary>Entenda os termos <span>(${notes.length})</span></summary><dl>${notes.map(note => {
    const definition = note.match(/^Contexto educacional\s*[—–-]\s*([^:]+):\s*([\s\S]+)$/);
    return `<div><dt>${esc(definition ? definition[1] : 'Nota')}</dt><dd>${esc(definition ? definition[2] : note)}</dd></div>`;
  }).join('')}</dl></details>`;
}
function resultView() {
  const r = state.result, c = r.content;
  const simulated = r.mode === 'simulated';
  const content = contentSections(c);
  const isCarousel = c?.output_format === 'informational_carousel';
  return `<section class="result-page">${stepper()}<div class="result-top"><span></span><div class="result-tools">${c ? `<button class="secondary" data-action="copy">${icon('copy')} Copiar</button><button class="secondary" data-action="download">${icon('download')} Baixar texto</button>` : ''}</div></div>
    ${simulated ? '<div class="notice"><span class="tag">DEMONSTRAÇÃO</span><p>Resultado simulado, sem análise por IA.'+(state.input === 'topic' ? ' A busca na web não foi realizada; a fonte é um exemplo fictício.' : '')+'</p></div>' : ''}
    <div class="result-layout"><article class="result-document"><div class="document-meta"><span class="eyebrow">${c ? formats[c.output_format].label : 'ANÁLISE'}</span><span class="status ${esc(r.status)}">${icon(r.status === 'approved' ? 'check' : 'shield')}${statuses[r.status] || esc(r.status)}</span></div><h1>${esc(c?.title || 'Não foi possível concluir a análise')}</h1><div class="result-byline"><span>${simulated ? 'Demonstração' : 'Análise com IA'}</span><span>${icon('file')}${esc(sourceName())}</span>${c ? `<span>${audiences[c.audience].label}</span>` : ''}${c?.estimated_duration_seconds ? `<span>${Math.ceil(c.estimated_duration_seconds)}s estimados</span>` : ''}</div>
    ${isCarousel ? carouselGlossary(content) : ''}
    ${c ? `<div class="content-sections ${c.output_format}">${content.map((s, i) => `<section class="content-section"><div class="content-label"><span>${isCarousel ? String(i+1).padStart(2,'0') : ''}</span><h2>${s.label}</h2></div><p>${esc(s.text)}</p>${isCarousel ? '' : (s.notes || []).map(n => `<aside class="education-note">${esc(n)}</aside>`).join('')}${s.pause ? `<small class="pause">Pausa de ${s.pause} segundos</small>` : ''}<div class="block-refs">${blockRefs(s,c,r)}</div></section>`).join('')}</div>` : `<div class="empty-result">${icon('file')}<p>${esc(r.reason)}</p><button class="primary" data-action="edit">Revisar configuração ${icon('arrow')}</button></div>`}
    <div class="document-footnote">Conteúdo informativo. Revise antes de publicar.</div></article>
    <aside class="result-aside"><div class="aside-panel"><h2>${icon('shield')} Avaliação do conteúdo</h2><p>${esc(r.reason)}</p>${r.evaluation?.issues?.length ? `<details class="evaluation-report"><summary>Ver parecer completo (${r.evaluation.issues.length})</summary>${r.evaluation.issues.map(issue => `<p class="evaluation-issue">${esc(issue.message)}</p>`).join('')}</details>` : ''}${r.errors?.length ? `<details><summary>Detalhes da análise</summary>${r.errors.map(e => `<p>${esc(e.message || e.code)}</p>`).join('')}</details>` : ''}</div>
    <div class="aside-panel"><div class="aside-title"><h2>${icon('link')} Referências</h2><span>${r.references.length}</span></div><p>Confira os trechos que sustentam o conteúdo.</p><div class="references">${r.references.length ? r.references.map((ref,i) => `<button data-evidence="${i}"><span class="ref-number">${String(i+1).padStart(2,'0')}</span><span><strong>${ref.page ? 'Página '+ref.page : 'Texto enviado'}</strong><small>Ver trecho original</small></span>${icon('arrow')}</button>`).join('') : '<p>Nenhuma referência disponível.</p>'}</div></div>
    ${c?.claims?.length ? `<details class="aside-panel"><summary>Afirmações e fontes (${c.claims.length})</summary>${c.claims.map(claim => `<div class="claim"><p>${esc(claim.text)}</p>${blockRefs({claimIds:[claim.claim_id]},c,r)}</div>`).join('')}</details>` : ''}<button class="secondary full-width" data-action="new">${icon('plus')} Começar outra análise</button></aside></div></section>`;
}
function historyRows() {
  const query = state.historyQuery.toLocaleLowerCase('pt-BR');
  const rows = history.filter(r => r.name.toLocaleLowerCase('pt-BR').includes(query));
  if (!rows.length) return `<div class="empty-history">${icon('clock')}<h2>${history.length ? 'Nenhuma análise encontrada' : 'Sua biblioteca começa aqui'}</h2><p>${history.length ? 'Tente buscar por outro nome.' : 'Documentos, pesquisas e conteúdos que você criar aparecerão neste espaço.'}</p>${history.length ? '' : '<button class="primary" data-action="new">Criar minha primeira análise '+icon('arrow')+'</button>'}</div>`;
  return `<div class="history-table"><div class="history-table-head"><span>FONTE / CONTEÚDO</span><span>FORMATO</span><span>NÍVEL</span><span>DATA</span><span></span></div>${rows.map(r => `<button class="history-row" data-open="${esc(r.key)}"><span class="history-name"><span class="history-icon">${icon(formats[r.payload.output_format].icon)}</span><span><strong>${esc(r.name)}</strong><small>${r.payload.mode === 'simulated' ? 'Demonstração · ' : ''}${r.status ? statuses[r.status] || esc(r.status) : 'Consultar solicitação'}</small></span></span><span>${formats[r.payload.output_format].label}</span><span>${audiences[r.payload.audience].label}</span><span>${humanDate(r.date)}</span>${icon('arrow')}</button>`).join('')}</div>`;
}
function historyView() {
  return `<section class="history-page"><div class="history-heading"><div><h1>Análises recentes<span>.</span></h1><p>Retome uma ideia. Encontre uma fonte. Continue de onde parou.</p></div><button class="primary" data-action="new">${icon('plus')} Nova análise</button></div><div class="history-stats"><div><span>Conteúdos criados</span><strong>${history.filter(r => r.status && r.status !== 'failed').length.toString().padStart(2,'0')}</strong></div><div><span>Documentos e textos</span><strong>${new Set(history.filter(r => r.input !== 'topic').map(r => r.payload.source_id)).size.toString().padStart(2,'0')}</strong></div><div><span>Pesquisas em demonstração</span><strong>${history.filter(r => r.input === 'topic').length.toString().padStart(2,'0')}</strong></div></div><div class="history-controls"><h2>Seu histórico <span>${history.length}</span></h2><label class="search-field">${icon('search')}<input id="history-search" type="search" placeholder="Buscar por nome…" aria-label="Buscar análise por nome" value="${esc(state.historyQuery)}"></label></div><p class="history-note">Execuções persistidas no SQLite deste servidor. Solicitações pendentes neste navegador também são mantidas.</p>${errorBox()}<div id="history-list">${historyRows()}</div></section>`;
}
function render(focus = true) {
  const unwrap = html => html.replace(/^<section[^>]*>/, '').replace(/<\/section>$/, '').replace(/<ol class="stepper"[\s\S]*?<\/ol>/, '');
  const flow = `<div class="create-page chat-flow">${stepper()}<section id="flow-step-1" class="flow-stage source-page ${state.step === 1 ? 'active-stage' : 'complete-stage'}">${unwrap(sourceView())}</section>${state.source || state.step >= 2 ? `<section id="flow-step-2" class="flow-stage configure-page ${state.step === 2 ? 'active-stage' : state.step > 2 ? 'complete-stage' : ''}">${unwrap(configureView())}</section>` : ''}${state.step === 3 ? `<section id="flow-step-3" class="flow-stage ${state.busy ? 'loading-page' : 'result-page'} ${state.busy ? 'active-stage' : 'complete-stage'}">${unwrap(state.busy ? loadingView() : resultView())}</section>` : ''}</div>`;
  const content = state.view === 'history' ? historyView() : flow;
  $('#app').innerHTML = shell(content);
  state.animateStep = false;
  state.pillAnimation = '';
  bindInputs();
  if (focus) { $('#main').focus({preventScroll:true}); window.scrollTo({top:0,behavior:'instant'}); }
}
function scrollToStage(selector, behavior = 'smooth') {
  const stage = document.querySelector(selector);
  if (!stage) return;
  const offset = (document.querySelector('.topbar')?.getBoundingClientRect().height || 0) + 12;
  window.scrollTo({top:Math.max(0, window.scrollY + stage.getBoundingClientRect().top - offset), behavior});
}
function updateSourcePanel() {
  $('#source-input-panel').innerHTML = sourceInput();
  $('#source-errors').innerHTML = errorBox();
  document.querySelectorAll('[data-input]').forEach(button => {
    const selected = button.dataset.input === state.input;
    button.classList.toggle('selected', selected);
    button.setAttribute('aria-pressed', String(selected));
  });
  $('#source-form button[type="submit"]').disabled = !canContinue() || state.busy;
  bindInputs();
}
function canContinue() { return Boolean(state.source) || (state.input === 'document' ? Boolean(state.file) : state.input === 'text' ? Boolean(state.text.trim()) : Boolean(state.topic.trim())); }
function chooseFile(file) {
  if (!file) return;
  state.error = '';
  if (!file.name.toLowerCase().endsWith('.pdf')) state.error = 'Escolha um arquivo PDF textual.';
  else if (file.size > 10*1024*1024) state.error = 'O arquivo ultrapassa o limite de 10 MB.';
  else if (file.size === 0) state.error = 'Este arquivo está vazio. Escolha outro PDF.';
  else { state.file = file; state.source = null; state.sourceName = ''; state.pending = null; }
  updateSourcePanel();
}
function bindInputs() {
  $('#file-input')?.addEventListener('change', e => chooseFile(e.target.files[0]));
  const drop = $('#dropzone');
  drop?.addEventListener('dragover', e => { e.preventDefault(); if (!state.busy) drop.classList.add('drag-over'); });
  drop?.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
  drop?.addEventListener('drop', e => { e.preventDefault(); if (!state.busy) { if (e.dataTransfer.files.length > 1) { state.error = 'Envie um documento por análise.'; updateSourcePanel(); } else chooseFile(e.dataTransfer.files[0]); } });
  for (const [id, field] of [['source-text','text'], ['topic','topic']]) $(`#${id}`)?.addEventListener('input', e => {
    state[field] = e.target.value; state.source = null; state.sourceName = ''; state.pending = null;
    $('#source-form button[type="submit"]').disabled = !canContinue();
  });
  $('#history-search')?.addEventListener('input', e => { state.historyQuery = e.target.value; $('#history-list').innerHTML = historyRows(); });
  if ($('#source-form')) $('#source-form').onsubmit = submitSource;
  $('#configure-form')?.addEventListener('submit', e => { e.preventDefault(); startGeneration(); });
  document.querySelectorAll('input[name="format"],input[name="audience"],#demo-mode').forEach(el => el.addEventListener('change', e => {
    if (e.target.id === 'demo-mode') state.demo = e.target.checked;
    else state[e.target.name] = e.target.value;
    state.pending = null;
    if (e.target.id === 'demo-mode') $('#configure-form button[type="submit"]').innerHTML = `${icon('sparkle')} ${state.demo ? 'Gerar demonstração' : 'Gerar análise'} ${icon('arrow')}`;
  }));
}
async function submitSource(event) {
  event.preventDefault(); if (!canContinue() || state.busy) return;
  state.busy = true; state.error = ''; render(false);
  try {
    if (!state.source) {
      const options = state.input === 'document' ? { method:'POST', headers:{'Content-Type':'application/pdf', 'X-Filename': encodeURIComponent(state.file.name)}, body:state.file }
        : { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({text:state.input === 'topic' ? demoSource : state.text}) };
      state.source = await api('/documents',options);
    }
    state.sourceName = sourceName(); state.step = 2;
    $('#announcer').textContent = 'Fonte adicionada. Escolha o formato e o nível do conteúdo.';
  } catch (e) { state.error = e.message; }
  finally { state.busy = false; state.animateStep = true; render(false); scrollToStage('#flow-step-2'); }
}
async function startGeneration(entry = null) {
  if (state.busy) return;
  state.pending = entry || state.pending || {
    key: crypto.randomUUID(), name:sourceName(), date:new Date().toISOString(), input:state.input,
    payload:generationPayload(state.source.source_id,state.audience,state.format,state.demo || state.input === 'topic'),
  };
  const pending = state.pending;
  remember(pending); state.step = 3; state.view = 'create'; state.busy = true; state.error = ''; render(false); scrollToStage('#flow-step-3');
  $('#announcer').textContent = 'Análise em andamento.';
  try {
    state.result = await api('/generations',{ method:'POST', headers:{'Content-Type':'application/json','Idempotency-Key':pending.key}, body:JSON.stringify(pending.payload) });
    remember({...pending, requestId:state.result.request_id, status:state.result.status});
    $('#announcer').textContent = statuses[state.result.status] || 'Análise concluída.';
    state.pending = null;
  } catch(e) {
    // A server error with an execution ID is terminal. A connection loss is
    // uncertain: retain the key so retrying cannot submit duplicate paid work.
    if (e.requestId) {
      remember({...pending, requestId:e.requestId, status:'failed', error:e.message});
      state.pending = null;
    }
    state.error = e.message; state.step = 2;
  } finally { state.busy = false; render(false); scrollToStage('#flow-step-3'); }
}
function restoreEntry(entry) {
  Object.assign(state, {input:entry.input, source:{source_id:entry.payload.source_id}, sourceName:entry.name,
    file:null, text:'', topic:entry.input === 'topic' ? entry.name : '',
    audience:entry.payload.audience, format:entry.payload.output_format, demo:entry.payload.mode === 'simulated',
    pending:null, error:'', readOnlyResult:Boolean(entry.readOnly)});
}
async function openHistory(entryKey) {
  const entry = history.find(r => r.key === entryKey); if (!entry || state.busy) return;
  state.activeNav = 'history';
  restoreEntry(entry);
  if (entry.error) { state.error = entry.error; state.step = 2; state.view = 'create'; render(); return; }
  if (!entry.status) { await startGeneration(entry); return; }
  state.busy = true;
  try {
    state.result = await api(`/generations/${encodeURIComponent(entry.requestId)}`);
    state.step = 3; state.view = 'create';
  } catch(e) { state.error = e.message; state.view = 'history'; }
  finally { state.busy = false; render(false); document.querySelector('#flow-step-3')?.scrollIntoView({behavior:'instant',block:'end'}); }
}
function storedRunName(result) {
  const text = result.prepared_source?.units?.find(unit => typeof unit.text === 'string' && unit.text.trim())?.text;
  return text?.split(/\r?\n/).map(line => line.trim()).find(Boolean)?.slice(0,80) || 'Resultado salvo';
}
async function loadStoredRun(requestId) {
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(requestId)) {
    state.error = 'O identificador desta análise não é válido.'; render(); return;
  }
  state.step = 3; state.view = 'create'; state.busy = true; state.sourceName = 'Resultado salvo'; render(false);
  try {
    state.result = await api(`/generations/${encodeURIComponent(requestId)}`);
    state.sourceName = storedRunName(state.result);
    if (state.result.content) {
      state.format = state.result.content.output_format;
      state.audience = state.result.content.audience;
    }
    state.readOnlyResult = true;
    remember({key:`saved:${requestId}`,name:state.sourceName,date:new Date().toISOString(),input:'document',
      payload:{source_id:null,audience:state.audience,output_format:state.format,mode:state.result.mode},
      requestId,status:state.result.status,readOnly:true});
  } catch (e) {
    state.step = 1; state.error = e.message;
  } finally { state.busy = false; render(); }
}
let evidenceRequest = 0;
async function showEvidence(index) {
  const dialog = $('#evidence-dialog'), request = ++evidenceRequest;
  dialog.innerHTML = `<div class="dialog-top"><span class="eyebrow">RASTREABILIDADE</span><button class="icon-button" data-action="close-dialog" aria-label="Fechar referência">${icon('close')}</button></div><h2 id="evidence-title">Referência ${index+1}</h2><div id="evidence-content" role="status">Carregando trecho original…</div>`;
  dialog.showModal();
  try {
    const e = await api(`/generations/${encodeURIComponent(state.result.request_id)}/evidence/${index}`);
    if (request !== evidenceRequest) return;
    $('#evidence-content').innerHTML = `<p class="evidence-location">${e.ref.page ? 'Página '+e.ref.page : 'Texto enviado'} · Caracteres ${e.ref.start}–${e.ref.end}</p><blockquote>${esc(e.text)}</blockquote><p class="evidence-caption">Trecho original usado para fundamentar a análise.</p>`;
  } catch(e) { if (request === evidenceRequest) $('#evidence-content').textContent = e.message; }
}
document.addEventListener('click', async event => {
  const button = event.target.closest('button'); if (!button || button.disabled) return;
  if (button.dataset.action === 'close-dialog') { $('#evidence-dialog').close(); return; }
  if (state.busy) return;
  if (button.dataset.input) { state.input = button.dataset.input; state.source = null; state.sourceName = ''; state.pending = null; state.error = ''; updateSourcePanel(); return; }
  if (button.dataset.topic) { state.topic = button.dataset.topic; state.source = null; state.sourceName = ''; state.pending = null; updateSourcePanel(); $('#topic').focus(); return; }
  if (button.dataset.open) { await openHistory(button.dataset.open); return; }
  if (button.dataset.evidence !== undefined) { await showEvidence(Number(button.dataset.evidence)); return; }
  switch(button.dataset.action) {
    case 'new': reset(); break;
    case 'create': state.pillAnimation = 'create'; state.view = 'create'; state.activeNav = 'create'; state.error = ''; render(); break;
    case 'history': state.pillAnimation = 'history'; state.view = 'history'; state.activeNav = 'history'; state.error = ''; render(false); await refreshHistory(); render(false); break;
    case 'choose-file': $('#file-input').click(); break;
    case 'edit': state.step = 2; state.error = ''; state.pending = null; render(false); document.querySelector('#flow-step-2')?.scrollIntoView({behavior:'instant',block:'start'}); break;
    case 'copy':
      try { await navigator.clipboard.writeText(exportText(state.result)); toast('Conteúdo copiado com suas referências.'); }
      catch { toast('Não foi possível acessar a área de transferência. Use Baixar texto.'); } break;
    case 'download': {
      const url = URL.createObjectURL(new Blob([exportText(state.result)],{type:'text/plain;charset=utf-8'}));
      const a = document.createElement('a'); a.href = url; a.download = `suno-${state.result.request_id}.txt`; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000); break;
    }
  }
});
$('#evidence-dialog').addEventListener('click', e => { if (e.target === $('#evidence-dialog')) $('#evidence-dialog').close(); });
const requestedRun = new URLSearchParams(window.location.search).get('request_id');
if (requestedRun) loadStoredRun(requestedRun); else render(false);
