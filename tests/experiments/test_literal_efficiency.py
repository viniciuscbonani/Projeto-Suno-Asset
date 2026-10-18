from __future__ import annotations

from hashlib import sha256
from uuid import uuid4

import pytest

from suno_asset.domain.models import ParsedUnit
from suno_asset.experiments.literal_efficiency.cache import VerifiedFactCache, cache_key
from suno_asset.experiments.literal_efficiency.contracts import (CompactFact, CompactFactPack,
    adapt_to_legacy,is_explicit_decision,is_explicit_guidance,normalize_model_facts, occurrence_coverage,
    selection_suggestion, validate_selection)
from suno_asset.experiments.literal_efficiency.pipeline import FACT_SCHEMA, compact_extract, compact_extraction_batches
from suno_asset.services.source_preparation import prepare_source
from suno_asset.services.llm_budget import TokenCounter
from suno_asset.services.protocols import ExpectedServiceError


def source(text="O Comitê decidiu manter a Selic em 10,5%. Se necessário, poderá rever a projeção."):
    doc = uuid4()
    unit = ParsedUnit(unit_id="p1", document_id=doc, page=1, start=0, end=len(text),
        text=text, parser_version="test-parser-v1")
    return prepare_source((unit,))


def pack_with(source, rows):
    digest = sha256(b"source").hexdigest()
    facts = tuple(CompactFact(id=f"F{i}", tipo=kind, texto_literal=literal, apoio_ids=(source.excerpts[0].excerpt_id,),
        owner_block_id=source.blocks[0].block_id, page=1, start=source.excerpts[0].ref.start,
        end=source.excerpts[0].ref.end) for i, (kind, literal) in enumerate(rows, 1))
    return CompactFactPack(source_sha256=digest, segmentation_version=source.segmentation_version, facts=facts)


def test_compact_schema_is_static_and_literal_support_must_resolve_once():
    assert FACT_SCHEMA["properties"]["facts"]["items"]["properties"]["apoio_ids"]["items"] == {"type":"string"}
    prepared = source()
    block = prepared.blocks[0]
    fact = normalize_model_facts(prepared, block.block_id,
        [{"tipo":"decision","texto_literal":"decidiu manter a Selic em 10,5%", "apoio_ids":[block.excerpt_ids[0]]}],
        source_sha256=sha256(b"source").hexdigest())
    assert len(fact) == 1 and fact[0].start > 0
    repeated = source("Decidiu manter a Selic. Decidiu manter a Selic.")
    with pytest.raises(ValueError, match="ambígua"):
        normalize_model_facts(repeated, repeated.blocks[0].block_id,
            [{"tipo":"decision","texto_literal":"Decidiu manter a Selic.", "apoio_ids":[repeated.blocks[0].excerpt_ids[0]]}],
            source_sha256=sha256(b"source").hexdigest())


def test_context_excerpt_cannot_create_a_fact_without_primary_support():
    doc=uuid4()
    prepared=prepare_source((ParsedUnit(unit_id='p1',document_id=doc,page=1,start=0,end=13,
        text='Fato vizinho.',parser_version='test'),ParsedUnit(unit_id='p2',document_id=doc,page=2,
        start=0,end=15,text='Outro conteúdo.',parser_version='test')))
    first,second=prepared.blocks
    primary=prepared.excerpts[0]
    with pytest.raises(ValueError,match="fora do escopo"):
        normalize_model_facts(prepared,first.block_id,[{"tipo":"rationale",
            "texto_literal":prepared.excerpts[1].text.strip(),"apoio_ids":[prepared.excerpts[1].excerpt_id]}],
            source_sha256=sha256(b"source").hexdigest())


def test_pdf_line_wrap_is_resolved_to_the_exact_source_literal():
    prepared=source("O ambiente externo permanece incerto em função da indefinição dos\nconflitos armados.")
    block=prepared.blocks[0]
    fact=normalize_model_facts(prepared,block.block_id,[{"tipo":"risk",
        "texto_literal":"O ambiente externo permanece incerto em função da indefinição dos conflitos armados.",
        "apoio_ids":[block.excerpt_ids[0]]}],source_sha256=sha256(b"wrapped").hexdigest())[0]
    assert fact.texto_literal==prepared.excerpts[0].text
    assert prepared.excerpts[0].text[fact.start-prepared.excerpts[0].ref.start:
        fact.end-prepared.excerpts[0].ref.start]==fact.texto_literal


def test_occurrence_coverage_tracks_repeated_numbers_by_location():
    prepared = source("A taxa foi 10%. A taxa voltou a 10%.")
    excerpt = prepared.excerpts[0]
    fact = normalize_model_facts(prepared, prepared.blocks[0].block_id,
        [{"tipo":"number","texto_literal":"A taxa foi 10%", "apoio_ids":[excerpt.excerpt_id]}],
        source_sha256=sha256(b"source").hexdigest())
    coverage = occurrence_coverage(prepared, fact)
    ten_percent = [row for row in coverage.occurrences if row.text == "10%"]
    assert len(ten_percent) == 2
    assert [row.disposition for row in ten_percent] == ["covered", "required_context"]
    assert coverage.semantic_recall == "not_measured_here"
    proposal=occurrence_coverage(prepared,fact,ignored={f"{excerpt.excerpt_id}:percent:{ten_percent[0].start}:{ten_percent[0].end}":"proposta manual"})
    assert any(row.disposition=="pending_review" for row in proposal.occurrences)


def test_verified_cache_requires_complete_controls_and_detects_corruption(tmp_path):
    dependencies = {"source_sha256":"a","block_id":"b","context_sha256":"c","parser_version":"p",
        "segmentation_version":"s","extractor_version":"e","model_config":"m","prompt_version":"v",
        "schema_version":"compact-facts-v1","verification_policy":"policy"}
    key = cache_key(dependencies)
    cache = VerifiedFactCache(tmp_path)
    with pytest.raises(ValueError, match="totalmente verificada"):
        cache.put(key, dependencies, {"facts":[]}, verified=False)
    cache.put(key, dependencies, {"facts":[1]}, verified=True)
    assert cache.get(key, dependencies)["facts"] == {"facts":[1]}
    path = tmp_path / f"{key}.json"
    path.write_text(path.read_text().replace("compact", "corrupt"))
    assert cache.get(key, dependencies) is None


def test_selection_requires_dependency_closure_and_confirmation_hash():
    prepared = source()
    pack = pack_with(prepared, [("decision", "O Comitê decidiu manter a Selic em 10,5%."),
        ("outlook", "Se necessário, poderá rever a projeção.")])
    roles = {"F1":{"role":"decision","mandatory":True,"dependency_ids":[],"reason":"decisão"},
        "F2":{"role":"guidance","mandatory":True,"dependency_ids":["F1"],"reason":"guidance condicionado"}}
    selection = selection_suggestion(pack, "beginner", "short_video_script", roles)
    assert selection.status == "suggested"
    assert {row.fact_id for row in selection.facts} == {"F1", "F2"}
    with pytest.raises(ValueError, match="dependência removida"):
        validate_selection(selection.model_copy(update={"facts":(selection.facts[1],)}), pack)
    with pytest.raises(ValueError, match="outra versão"):
        validate_selection(selection, pack.model_copy(update={"facts":(pack.facts[0],)}))


def test_selection_cannot_remove_or_demote_decision_or_explicit_guidance():
    prepared=source("O Comitê decidiu manter a Selic em 10,5%. O Comitê manterá vigilância nas próximas reuniões.")
    literals=[part.strip() for part in prepared.excerpts[0].text.split(".") if part.strip()]
    pack=pack_with(prepared,[("decision",literals[0]+"."),("outlook",literals[1]+".")])
    suggested=selection_suggestion(pack,"beginner","analytical_text")
    with pytest.raises(ValueError,match="núcleo obrigatório"):
        validate_selection(suggested.model_copy(update={"facts":suggested.facts[1:]}),pack)
    demoted=suggested.model_copy(update={"facts":(suggested.facts[0].model_copy(update={"mandatory":False}),suggested.facts[1])})
    with pytest.raises(ValueError,match="marcado como opcional"):
        validate_selection(demoted,pack)


def test_explicit_guidance_with_pdf_line_wrap_is_mandatory():
    text='O Copom decidiu reduzir a Selic para 13,75% a.a. O Comitê seguirá\nincorporando novas informações e acompanhando a evolução do cenário de forma a\nmanter a restrição adequada para assegurar a convergência da inflação à meta.'
    prepared=source(text)
    decision='O Copom decidiu reduzir a Selic para 13,75% a.a.'
    guidance='O Comitê seguirá\nincorporando novas informações e acompanhando a evolução do cenário de forma a\nmanter a restrição adequada para assegurar a convergência da inflação à meta.'
    pack=pack_with(prepared,[('decision',decision),('outlook',guidance)])
    selection=selection_suggestion(pack,'beginner','analytical_text')
    assert {row.fact_id for row in selection.facts}=={'F1','F2'}
    assert {row.fact_id for row in selection.facts if row.mandatory}=={'F1','F2'}


def test_carousel_suggestion_reserves_two_cards_and_uncertainty_stays_pending():
    prepared=source("Uma decisão. Uma projeção. Um risco. Uma justificativa. Outro contexto. Mais um fato.")
    literals=[sentence.strip() for sentence in prepared.excerpts[0].text.split(".") if sentence.strip()]
    pack=pack_with(prepared,[("decision",literals[0]),("outlook",literals[1]),("risk",literals[2]),
        ("rationale",literals[3]),("number",literals[4]),("number",literals[5])])
    selection=selection_suggestion(pack,"beginner","informational_carousel")
    assert len(selection.facts)==4
    uncertain={fact.id:{"role":"other","mandatory":False,"dependency_ids":[],"reason":"ambiguous","uncertain":True}
        for fact in pack.facts}
    pending=selection_suggestion(pack,"beginner","analytical_text",uncertain)
    assert pending.status=="pending"


def test_compact_extraction_repairs_only_invalid_rows_and_keeps_valid_rows():
    prepared = source("A decisão segue em vigor. A taxa permanece elevada.")
    block = prepared.blocks[0]
    class FakeSession:
        calls = 0
        def preflight(self,*args,**kwargs): pass
        def call(self,*args,**kwargs):
            self.calls += 1
            block_id=block.block_id
            if self.calls == 1:
                return {"facts":[
                    {"block_id":block_id,"tipo":"rationale","texto_literal":"A decisão segue em vigor.","apoio_ids":[block.excerpt_ids[0]]},
                    {"block_id":block_id,"tipo":"number","texto_literal":"ausente da fonte","apoio_ids":[block.excerpt_ids[0]]}]}
            return {"facts":[{"block_id":block_id,"tipo":"rationale","texto_literal":"A taxa permanece elevada.","apoio_ids":[block.excerpt_ids[0]]}]}
        counter=TokenCounter()
    session=FakeSession()
    pack=compact_extract(prepared,session,variant="B1",source_sha256=sha256(b"source").hexdigest())
    assert session.calls == 2
    assert [fact.texto_literal for fact in pack.facts] == ["A decisão segue em vigor.","A taxa permanece elevada."]


def test_compact_extraction_escalates_only_after_terminal_schema_rejection():
    prepared=source("A decisão foi mantida.")
    block=prepared.blocks[0]
    class FakeSession:
        calls=0
        model_overrides={}
        attempt_records=[]
        counter=TokenCounter()
        def preflight(self,*args,**kwargs): pass
        def call(self,*args,**kwargs):
            self.calls+=1
            if self.calls==1:
                self.attempt_records.append({'status':'http_error','http_status':400,'provider_code':'json_validate_failed'})
                raise ExpectedServiceError('invalid_llm_response','schema rejected')
            assert self.model_overrides['extract']=='openai/gpt-oss-120b'
            return {'facts':[{'block_id':block.block_id,'tipo':'decision','texto_literal':prepared.excerpts[0].text,
                'apoio_ids':[block.excerpt_ids[0]]}]}
    session=FakeSession()
    pack=compact_extract(prepared,session,variant='B1',source_sha256=sha256(b'fallback').hexdigest())
    assert session.calls==2 and session.model_overrides=={}
    assert len(pack.facts)==1


def test_compact_extraction_does_not_replay_unknown_attempt():
    prepared=source("A decisão foi mantida.")
    class FakeSession:
        calls=0
        model_overrides={}
        attempt_records=[{'status':'unavailable','usage_known':False}]
        counter=TokenCounter()
        def preflight(self,*args,**kwargs): pass
        def call(self,*args,**kwargs):
            self.calls+=1
            raise ExpectedServiceError('groq_unavailable','unknown completion')
    session=FakeSession()
    with pytest.raises(ExpectedServiceError,match='unknown completion'):
        compact_extract(prepared,session,variant='B1',source_sha256=sha256(b'unknown').hexdigest())
    assert session.calls==1 and session.model_overrides=={}


def test_economy_profile_uses_120b_for_invalid_fact_repair_only_after_received_20b():
    prepared=source("A decisão foi mantida.");block=prepared.blocks[0]
    class FakeSession:
        calls=0
        profile='economy'
        model_overrides={}
        attempt_records=[]
        counter=TokenCounter()
        def preflight(self,*args,**kwargs): pass
        def call(self,*args,**kwargs):
            self.calls+=1
            data=args[2]
            if self.calls==1:
                return {'facts':[{'block_id':block.block_id,'tipo':'decision','texto_literal':prepared.excerpts[0].text,
                    'apoio_ids':['unknown-support']}]}
            assert self.model_overrides['extract']=='openai/gpt-oss-120b'
            assert 'invalid_items' in data
            return {'facts':[{'block_id':block.block_id,'tipo':'decision','texto_literal':prepared.excerpts[0].text,
                'apoio_ids':[block.excerpt_ids[0]]}]}
    session=FakeSession()
    result=compact_extract(prepared,session,variant='B1',source_sha256=sha256(b'repair').hexdigest())
    assert session.calls==2 and len(result.facts)==1


def test_compact_batches_separate_blocks_with_overlapping_support_catalogs():
    from suno_asset.domain.models import ParsedUnit
    first=ParsedUnit(unit_id='p1',document_id=uuid4(),page=1,start=0,end=len('Primeiro fato.'),text='Primeiro fato.',parser_version='test')
    second=ParsedUnit(unit_id='p2',document_id=first.document_id,page=2,start=0,end=len('Segundo fato.'),text='Segundo fato.',parser_version='test')
    prepared=prepare_source((first,second))
    assert len(prepared.blocks)==2
    a,b=prepared.blocks;excerpt_a,excerpt_b=prepared.excerpts
    overlapping=prepared.model_copy(update={'blocks':(
        a.model_copy(update={'context_ids':(excerpt_b.excerpt_id,),
            'required_context_ids':(excerpt_b.excerpt_id,)}),
        b.model_copy(update={'context_ids':(excerpt_a.excerpt_id,),
            'required_context_ids':(excerpt_a.excerpt_id,)}))})
    batches=compact_extraction_batches(overlapping,type('S',(),{'counter':TokenCounter()})())
    assert len(batches)==2 and all(len(batch)==1 for batch in batches)


def test_safe_rule_can_cover_only_an_entire_unambiguous_decision_block():
    prepared=source("O Comitê decidiu manter a Selic em 10,5%.")
    class FakeSession:
        def preflight(self,*args,**kwargs): pytest.fail("regra integral não exige chamada")
        def call(self,*args,**kwargs): pytest.fail("regra integral não exige chamada")
    pack=compact_extract(prepared,FakeSession(),variant="B2",source_sha256=sha256(b"source").hexdigest())
    assert len(pack.facts)==1
    assert pack.facts[0].provenance=="rule"
    assert pack.facts[0].texto_literal==prepared.excerpts[0].text.strip()


def test_compact_extraction_records_and_skips_structural_blocks():
    prepared=source("Cabeçalho institucional.")
    block=prepared.blocks[0]
    prepared=prepared.model_copy(update={'blocks':(block.model_copy(update={'kind':'header'}),)})
    class FakeSession:
        counter=TokenCounter()
        def preflight(self,*args,**kwargs): pytest.fail("bloco estrutural não exige chamada")
        def call(self,*args,**kwargs): pytest.fail("bloco estrutural não exige inferência")
    pack=compact_extract(prepared,FakeSession(),variant='B1',source_sha256=sha256(b"header").hexdigest())
    assert not pack.facts
    assert pack.verification['structural_blocks_excluded']==[block.block_id]


def test_context_only_fact_is_audited_and_kept_under_its_owner_block():
    doc=uuid4()
    text_a='Primeiro fato.';text_b='Segundo conteúdo.'
    prepared=prepare_source((ParsedUnit(unit_id='p1',document_id=doc,page=1,start=0,end=len(text_a),
        text=text_a,parser_version='test'),ParsedUnit(unit_id='p2',document_id=doc,page=2,
        start=0,end=len(text_b),text=text_b,parser_version='test')))
    first,second=prepared.blocks;a,b=prepared.excerpts
    prepared=prepared.model_copy(update={'blocks':(first.model_copy(update={
        'context_ids':(b.excerpt_id,),'required_context_ids':(b.excerpt_id,)}),second)})
    class FakeSession:
        counter=TokenCounter();model_overrides={};profile='economy'
        def preflight(self,*args,**kwargs):pass
        def call(self,stage,prompt,data,schema,cap):
            block_id=data['blocks'][0]['block_id']
            if block_id==first.block_id:
                return {'facts':[{'block_id':block_id,'tipo':'rationale','texto_literal':b.text.strip(),
                    'apoio_ids':[b.excerpt_id]}]}
            return {'facts':[{'block_id':block_id,'tipo':'rationale','texto_literal':b.text.strip(),
                'apoio_ids':[b.excerpt_id]}]}
    pack=compact_extract(prepared,FakeSession(),variant='B1',source_sha256=sha256(b'cross-block').hexdigest())
    assert [fact.owner_block_id for fact in pack.facts]==[second.block_id]
    assert pack.verification['cross_block_candidates'][0]['block_id']==first.block_id


def test_empty_narrative_block_gets_a_single_120b_recall_review():
    prepared=source('O cenário externo permanece incerto.')
    block=prepared.blocks[0]
    class FakeSession:
        counter=TokenCounter();model_overrides={};profile='economy';literal_artifacts=[];calls=0
        def preflight(self,*args,**kwargs):
            if self.calls:self.model_overrides['extract']='openai/gpt-oss-120b'
        def call(self,stage,prompt,data,schema,cap):
            self.calls+=1
            if self.calls==1:return {'facts':[]}
            assert self.model_overrides['extract']=='openai/gpt-oss-120b'
            return {'facts':[{'block_id':block.block_id,'tipo':'risk','texto_literal':prepared.excerpts[0].text.strip(),
                'apoio_ids':[block.excerpt_ids[0]]}]}
    session=FakeSession()
    pack=compact_extract(prepared,session,variant='B1',source_sha256=sha256(b'empty-review').hexdigest())
    assert session.calls==2 and session.model_overrides=={}
    assert len(pack.facts)==1 and pack.facts[0].tipo=='risk'


def test_empty_block_review_repairs_cross_excerpt_or_invalid_literals_without_failing_pack():
    prepared=source('O cenário externo permanece incerto.')
    block=prepared.blocks[0]
    class FakeSession:
        counter=TokenCounter();model_overrides={};profile='economy';literal_artifacts=[];calls=0
        def preflight(self,*args,**kwargs):pass
        def call(self,stage,prompt,data,schema,cap):
            self.calls+=1
            if self.calls==1:return {'facts':[]}
            if self.calls==2:
                assert 'um único trecho principal X' in prompt
                return {'facts':[{'block_id':block.block_id,'tipo':'risk','texto_literal':'texto que cruza a quebra de página',
                    'apoio_ids':[block.excerpt_ids[0]]}]}
            assert 'invalid_items' in data
            return {'facts':[{'block_id':block.block_id,'tipo':'risk','texto_literal':prepared.excerpts[0].text.strip(),
                'apoio_ids':[block.excerpt_ids[0]]}]}
    session=FakeSession()
    pack=compact_extract(prepared,session,variant='B1',source_sha256=sha256(b'empty-repair').hexdigest())
    assert session.calls==3 and session.model_overrides=={}
    assert len(pack.facts)==1 and pack.facts[0].texto_literal==prepared.excerpts[0].text.strip()


def test_empty_block_review_batches_two_blocks_with_one_schema_and_prompt():
    from suno_asset.domain.models import ParsedUnit
    from suno_asset.services.source_preparation import prepare_source
    document=uuid4();texts=('O cenário externo permanece incerto.','A inflação de serviços segue pressionada.')
    prepared=prepare_source(tuple(ParsedUnit(unit_id=f'p{i}',document_id=document,page=i,start=0,end=len(text),
        text=text,parser_version='test') for i,text in enumerate(texts,1)))
    class FakeSession:
        counter=TokenCounter();model_overrides={};profile='economy';literal_artifacts=[];calls=0
        def preflight(self,*args,**kwargs):pass
        def call(self,stage,prompt,data,schema,cap):
            self.calls+=1
            if self.calls==1:return {'facts':[]}
            assert len(data['blocks'])==2 and cap==1600
            excerpt_by_id={row['id']:row['text'] for row in data['source_catalog']}
            return {'facts':[{'block_id':block['block_id'],'tipo':'risk',
                'texto_literal':excerpt_by_id[block['owned_ids'][0]].strip(),
                'apoio_ids':[block['owned_ids'][0]]} for block in data['blocks']]}
    session=FakeSession()
    pack=compact_extract(prepared,session,variant='B1',source_sha256=sha256(b'grouped-empty').hexdigest())
    assert session.calls==2 and len(pack.facts)==2


def test_economy_core_requires_explicit_decision_and_guidance():
    from suno_asset.mvp_economy import select_anchors
    prepared=source('O Copom decidiu reduzir a Selic para 13,75%. Exige-se restrição maior por mais tempo. '
        'A magnitude do ciclo de calibração será ajustada à luz do cenário.')
    literals=[part.strip() for part in prepared.excerpts[0].text.split('.') if part.strip()]
    pack=pack_with(prepared,[('decision',literals[0]),('decision',literals[1]),('outlook',literals[2])])
    anchors=adapt_to_legacy(prepared,pack).anchors
    _,manifest=select_anchors(pack,anchors,'analytical_text')
    by_text={fact.texto_literal:fact.id for fact in pack.facts}
    assert set(manifest['mandatory_fact_ids'])=={by_text[literals[0]],by_text[literals[2]]}


def test_economy_analytical_selection_caps_facts_and_prioritizes_rationale_risk_projection():
    from suno_asset.mvp_economy import select_anchors
    text=('O Copom decidiu reduzir a Selic para 13,75%. '
        'O Comitê seguirá incorporando informações para manter a restrição adequada. '
        'Uma política fiscal contracíclica favorece a convergência da inflação. '
        'Há risco de desancoragem das expectativas. '
        'A projeção de inflação é 3,2% em 2028. '
        'O cenário externo permanece incerto. '
        'A taxa observada foi 4,1%. '
        'O PIB desacelerou no trimestre. '
        'A política monetária busca estabilidade de preços.')
    prepared=source(text)
    rows=[('decision','O Copom decidiu reduzir a Selic para 13,75%.'),
        ('outlook','O Comitê seguirá incorporando informações para manter a restrição adequada.'),
        ('rationale','Uma política fiscal contracíclica favorece a convergência da inflação.'),
        ('risk','Há risco de desancoragem das expectativas.'),
        ('outlook','A projeção de inflação é 3,2% em 2028.'),
        ('risk','O cenário externo permanece incerto.'),
        ('number','A taxa observada foi 4,1%.'),
        ('outlook','O PIB desacelerou no trimestre.'),
        ('rationale','A política monetária busca estabilidade de preços.')]
    pack=pack_with(prepared,rows);anchors=adapt_to_legacy(prepared,pack).anchors
    selected,manifest=select_anchors(pack,anchors,'analytical_text')
    selected_ids=set(manifest['selected_fact_ids'])
    by_id={fact.id:fact for fact in pack.facts}
    assert len(selected_ids)==6 and manifest['version']=='mvp-selection-v3'
    assert {fact.id for fact in pack.facts if is_explicit_decision(fact) or is_explicit_guidance(fact)} <= selected_ids
    assert any(by_id[fid].tipo=='rationale' for fid in selected_ids)
    assert any(by_id[fid].tipo=='risk' for fid in selected_ids)
    assert any(by_id[fid].texto_literal.startswith('A projeção') for fid in selected_ids)


def test_economy_selection_identity_includes_required_context_supports():
    from suno_asset.domain.models import ParsedUnit
    from suno_asset.mvp_economy import select_anchors
    from suno_asset.services.source_preparation import prepare_source
    document=uuid4();first='O Copom decidiu reduzir a Selic para 13,75%.';context='Orientação necessária para interpretar a decisão.'
    prepared=prepare_source((ParsedUnit(unit_id='p1',document_id=document,page=1,start=0,end=len(first),
        text=first,parser_version='test'),ParsedUnit(unit_id='p2',document_id=document,page=2,start=0,end=len(context),
        text=context,parser_version='test')))
    decision,followup=prepared.blocks;followup_excerpt=prepared.excerpts[1]
    prepared=prepared.model_copy(update={'blocks':(decision.model_copy(update={
        'context_ids':(followup_excerpt.excerpt_id,),
        'required_context_ids':(followup_excerpt.excerpt_id,)}),followup)})
    pack=pack_with(prepared,[('decision',first)])
    anchors=adapt_to_legacy(prepared,pack).anchors
    selected,manifest=select_anchors(pack,anchors,'analytical_text',prepared)
    assert selected==anchors and manifest['selected_fact_ids']==[pack.facts[0].id]
