// Run against an isolated local API. No test sends a real request to Groq.
import { test, before, after } from 'node:test';
import assert from 'node:assert/strict';
import { chromium } from 'playwright';
import { demoSource, formats, audiences, exportText, escapeHTML } from '../../src/suno_asset/web/core.mjs';

const base = process.env.STUDIO_URL || 'http://127.0.0.1:8011';
let browser;
before(async () => { browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})}); });
after(async () => { await browser?.close(); });
async function run(fn, {url=base, setup, ...contextOptions}={}) {
  const context = await browser.newContext({viewport:{width:1440,height:1000}, ...contextOptions});
  const page = await context.newPage();
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  // Fail closed: unexpected real inference is never allowed from this suite.
  await page.route('**/generations', async route => {
    if (route.request().method()==='POST' && route.request().postDataJSON().mode==='real') {
      return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:{error:'groq_config_invalid'}})});
    }
    await route.continue();
  });
  try { await setup?.(page); await page.goto(url); await fn(page,context); assert.deepEqual(errors,[]); }
  finally { await context.close(); }
}
test('saved API result opens in the frontend by request ID', async () => {
  const requestId='46a235c3-5864-450b-8613-eafba5a5404d';
  const saved={request_id:requestId,mode:'real',status:'approved',reason:'verificações da política aprovadas',
    prepared_source:{units:[{text:'281ª Ata da Reunião do Copom'}]},references:[],errors:[],
    content:{output_format:'analytical_text',audience:'beginner',title:'Análise da ata do Copom',
      synthesis:'Síntese carregada da execução salva.',development:'Desenvolvimento salvo.',implications:'Implicações salvas.',claims:[]}};
  await run(async page => {
    await page.locator('.result-document').waitFor();
    assert.equal(await page.locator('.status.approved').count(),1);
    assert.ok((await page.locator('.result-document').innerText()).includes('Síntese carregada da execução salva.'));
    assert.ok((await page.locator('.result-byline').innerText()).includes('281ª Ata da Reunião do Copom'));
    await page.getByRole('button',{name:/Análises recentes/}).click();
    await page.locator('.history-row').waitFor();
    assert.ok((await page.locator('.history-row').first().innerText()).includes('281ª Ata da Reunião do Copom'));
    await page.locator('.history-row').first().click();
    await page.locator('.result-document').waitFor();
    assert.equal(await page.getByRole('button',{name:/Voltar às análises recentes/}).count(),1);
  },{url:`${base}/?request_id=${requestId}`,setup:async page=>{
    await page.route(`**/generations/${requestId}`,route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(saved)}));
    await page.route('**/generations?limit=50',route=>route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({items:[{
      request_id:requestId,name:'281ª Ata da Reunião do Copom',date:'2026-10-17T12:00:00Z',status:'approved',state:'completed',
      audience:'beginner',output_format:'analytical_text',mode:'real',source_id:'00000000-0000-0000-0000-000000000001'}]})}));
  }});
});
async function enterText(page,text=demoSource) {
  await page.getByRole('button',{name:'Colar texto',exact:true}).click();
  await page.getByLabel('Cole o texto da ata do Copom').fill(text);
  await page.getByRole('button',{name:'Continuar',exact:true}).click();
  await page.locator('#configure-form').waitFor();
}
async function select(page,format,audience) {
  await page.locator('.format-option').filter({hasText:formats[format].label}).click();
  await page.locator('.audience-option').filter({hasText:audiences[audience].label}).click();
}
async function demo(page) {
  await page.getByText('Opções de demonstração',{exact:true}).click();
  await page.locator('#demo-mode').check();
  await page.getByRole('button',{name:'Gerar demonstração',exact:true}).click();
  await page.locator('.result-document').waitFor();
}
for (const format of Object.keys(formats)) for (const audience of Object.keys(audiences)) {
  test(`live local API: ${format} / ${audience}, evidence and persisted history`, async () => run(async page => {
    await enterText(page); await select(page,format,audience);
    const responsePromise=page.waitForResponse(r=>r.url()===`${base}/generations` && r.request().method()==='POST');
    await demo(page);
    const result=await (await responsePromise).json();
    assert.equal(result.content.output_format,format); assert.equal(result.content.audience,audience);
    assert.equal(result.mode,'simulated');
    assert.ok(await page.locator('.notice').filter({hasText:'DEMONSTRAÇÃO'}).isVisible());
    assert.ok((await page.locator('.result-document').innerText()).includes(demoSource.split('\n')[0].slice(3)));
    await page.locator('.references button').first().click();
    await page.locator('#evidence-content blockquote').waitFor();
    assert.ok((await page.locator('#evidence-content blockquote').innerText()).length>10);
    await page.keyboard.press('Escape');
    const downloadPromise=page.waitForEvent('download');
    await page.getByRole('button',{name:'Baixar texto',exact:true}).click();
    assert.match((await downloadPromise).suggestedFilename(),/^suno-.*\.txt$/);
    assert.ok(exportText(result).includes('Referências'));
    await page.reload();
    await page.getByRole('button',{name:/Análises recentes/}).click();
    await page.locator('.history-row').first().click();
    await page.locator('.result-document').waitFor();
    assert.ok((await page.locator('.result-document').innerText()).includes(result.content.title));
  }));
}
function pdf() {
  const stream='BT /F1 12 Tf 50 750 Td (O Copom decidiu manter a Selic.) Tj ET';
  const objects=['<< /Type /Catalog /Pages 2 0 R >>','<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',
    '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',`<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`];
  let data='%PDF-1.4\n', offsets=[0];
  for (let i=0;i<objects.length;i++) { offsets.push(data.length); data+=`${i+1} 0 obj\n${objects[i]}\nendobj\n`; }
  const start=data.length;
  data+=`xref\n0 6\n0000000000 65535 f \n${offsets.slice(1).map(o=>`${String(o).padStart(10,'0')} 00000 n \n`).join('')}trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n${start}\n%%EOF`;
  return Buffer.from(data);
}
test('PDF upload defaults to real IA; selected pair and idempotency reach the boundary, without simulation fallback', async () => run(async page => {
  await page.locator('#file-input').setInputFiles({name:'ata-copom.pdf',mimeType:'application/pdf',buffer:pdf()});
  await page.getByRole('button',{name:'Continuar',exact:true}).click();
  await page.locator('#configure-form').waitFor();
  await select(page,'short_video_script','advanced');
  const requestPromise=page.waitForRequest(r=>r.url()===`${base}/generations`);
  await page.getByRole('button',{name:'Gerar análise',exact:true}).click();
  const request=await requestPromise;
  assert.equal(request.postDataJSON().mode,'real');
  assert.equal(request.postDataJSON().architecture,'literal-v1');
  assert.equal(request.postDataJSON().audience,'advanced');
  assert.equal(request.postDataJSON().output_format,'short_video_script');
  assert.ok(request.headers()['idempotency-key']);
  await page.getByRole('alert').waitFor();
  assert.ok((await page.getByRole('alert').innerText()).includes('configuração'));
  assert.equal(await page.locator('.result-document').count(),0);
  assert.equal(await page.locator('#demo-mode').isChecked(),false);
}));
test('topic uses a clearly disclosed fixed example with no web acquisition', async () => run(async page => {
  await page.getByRole('button',{name:'Pesquisar tema',exact:true}).click();
  await page.getByLabel('Qual tema você quer explorar?').fill('Qualquer tema solicitado');
  assert.ok((await page.locator('.mock-note').innerText()).includes('nenhuma pesquisa na web'));
  const uploadPromise=page.waitForRequest(r=>r.url()===`${base}/documents`);
  await page.getByRole('button',{name:'Continuar',exact:true}).click();
  assert.equal((await uploadPromise).postDataJSON().text,demoSource);
  await page.getByRole('button',{name:'Gerar demonstração',exact:true}).click();
  await page.locator('.result-document').waitFor();
  assert.ok((await page.locator('.notice').innerText()).includes('fonte é um exemplo fictício'));
}));
test('needs_review is preserved and untrusted source is plain text', async () => run(async page => {
  await enterText(page,'A atividade desacelerou. <img src=x onerror=alert(1)>');
  await select(page,'informational_carousel','advanced'); await demo(page);
  assert.equal(await page.locator('.status.needs_review').count(),1);
  assert.equal(await page.locator('.evaluation-issue').first().isVisible(),false);
  await page.locator('.evaluation-report summary').click();
  assert.equal(await page.locator('.evaluation-issue').first().isVisible(),true);
  assert.equal(await page.locator('.result-document img').count(),0);
  assert.ok((await page.locator('.result-document').innerText()).includes('<img'));
  assert.ok(escapeHTML('<script>') === '&lt;script&gt;');
}));
test('validation, failed result and server errors remain actionable', async () => run(async page => {
  assert.equal(await page.getByRole('button',{name:'Continuar',exact:true}).isDisabled(),true);
  await page.locator('#file-input').setInputFiles({name:'a.txt',mimeType:'text/plain',buffer:Buffer.from('test')});
  assert.ok((await page.getByRole('alert').innerText()).includes('PDF'));
  await enterText(page);
  await page.route('**/generations', async route => route.fulfill({status:200,contentType:'application/json',body:JSON.stringify({request_id:'00000000-0000-0000-0000-000000000001',mode:'real',status:'failed',reason:'Fonte sem evidências suficientes.',content:null,references:[],errors:[]})}));
  await page.getByRole('button',{name:'Gerar análise',exact:true}).click();
  await page.locator('.empty-result').waitFor();
  assert.equal(await page.locator('.status.failed').count(),1);
  assert.ok(await page.getByRole('button',{name:/Revisar configuração/}).isVisible());
}));
test('unavailable evidence reports an error and the dialog remains dismissible', async () => run(async page => {
  await enterText(page); await demo(page);
  await page.route('**/evidence/*',route=>route.fulfill({status:410,contentType:'application/json',body:JSON.stringify({detail:{error:'resource_deleted'}})}));
  await page.locator('.references button').first().click();
  await page.getByText('Esta análise ou fonte foi removida do servidor.',{exact:true}).waitFor();
  await page.getByRole('button',{name:'Fechar referência',exact:true}).click();
  assert.equal(await page.locator('dialog').isVisible(),false);
}));
test('connection loss reuses idempotency key even after reload', async () => run(async page => {
  await enterText(page); let firstKey, secondKey;
  await page.route('**/generations',async route=>{firstKey=route.request().headers()['idempotency-key']; await route.abort();},{times:1});
  await page.getByRole('button',{name:'Gerar análise',exact:true}).click();
  await page.getByRole('alert').waitFor(); await page.reload();
  await page.route('**/generations',async route=>{secondKey=route.request().headers()['idempotency-key']; await route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:{error:'groq_config_invalid',request_id:'00000000-0000-0000-0000-000000000001'}})});});
  await page.getByRole('button',{name:/Análises recentes/}).click();
  await page.locator('.history-row').click(); await page.getByRole('alert').waitFor();
  assert.equal(firstKey,secondKey);
  // A known terminal error permits a fresh key for the next explicit attempt.
  const fresh=page.waitForRequest(r=>r.url()===`${base}/generations`);
  await page.getByRole('button',{name:'Gerar análise',exact:true}).click();
  assert.notEqual((await fresh).headers()['idempotency-key'],firstKey);
}));
test('mobile, keyboard radios and reduced motion', async () => run(async page => {
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.equal(await page.locator('.hero').evaluate(el=>getComputedStyle(el).animationName),'none');
  await enterText(page);
  const first=page.locator('input[name="format"]').first(); await first.focus();
  await page.keyboard.press('ArrowRight');
  assert.equal(await page.locator('input[value="informational_carousel"]').isChecked(),true);
  await demo(page);
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.locator('.references button').first().click();
  await page.locator('#evidence-content blockquote').waitFor(); await page.keyboard.press('Escape');
  assert.equal(await page.locator('dialog').isVisible(),false);
}, {viewport:{width:390,height:844},reducedMotion:'reduce'}));

test('source tabs preserve the surrounding page and vertical progress', async () => run(async page => {
  await page.evaluate(() => {
    window.studioShell = ['.hero', '.source-card', '.stepper', '.topbar'].map(s => document.querySelector(s));
  });
  for (const name of ['Pesquisar tema', 'Colar texto', 'Adicionar documento']) {
    await page.getByRole('button', {name, exact:true}).click();
    assert(await page.evaluate(() => window.studioShell.every(node => node.isConnected)));
    await page.getByRole('button', {name, exact:true}).evaluate(button => {
      if (button.getAttribute('aria-pressed') !== 'true') throw new Error('Source tab not selected');
    });
  }
  assert.equal(await page.locator('.sidebar,.local-badge').count(), 0);
  const positions = await page.locator('.stepper li').evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().top));
  assert(positions[0] < positions[1] && positions[1] < positions[2]);
  await page.getByRole('button', {name:'Análises recentes', exact:true}).click();
  await page.locator('.history-page').waitFor();
  await page.getByRole('button', {name:'Criar conteúdo', exact:true}).click();
  await page.locator('.source-card').waitFor();
}));
