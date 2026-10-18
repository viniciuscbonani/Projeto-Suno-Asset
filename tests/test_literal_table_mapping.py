"""Unambiguous source-row mismatches are rejected, never rewritten."""
from copy import deepcopy
import pytest

from suno_asset.services.literal import (consolidate,extraction_payload,resolve_wire_extraction,
    validate_extraction,_validate_unambiguous_table_indicator)
from suno_asset.services.protocols import ExpectedServiceError
from test_literal_selectors import source,fact,row,parsed_wire


TABLE=('Variação do IPCA acumulada em quatro trimestres (%)\n'
       'Índice de preços 2026 2027 1º tri 2028\n'
       'IPCA 5,2 3,9 3,2\nIPCA livres 5,2 4,1 3,2\nIPCA administrados 5,3 3,5 3,2\n')


def candidate(value,indicator,text=TABLE):
    prepared=source(text);block=prepared.blocks[0];eid=block.excerpt_ids[0]
    data=extraction_payload(prepared,(block,))
    numeric=fact(data,eid,value,indicator=indicator)
    wire=parsed_wire({'blocks':{block.block_id:row(eid,[numeric])}})
    return prepared,resolve_wire_extraction(prepared,wire,(block,))


@pytest.mark.parametrize('value,indicator',[('4,1','IPCA'),('5,3','IPCA livres'),('3,5','IPCA livres')])
def test_unique_value_cannot_be_assigned_to_another_explicit_table_row(value,indicator):
    prepared,domain=candidate(value,indicator);before=deepcopy(domain.model_dump())
    with pytest.raises(ExpectedServiceError) as exc:consolidate(prepared,domain,prepared.blocks,'test')
    assert exc.value.code=='numeric_table_mapping'
    assert 'X0001' in str(exc.value) and 'offset' in str(exc.value)
    assert domain.model_dump()==before


@pytest.mark.parametrize('value,indicator',[('4,1','IPCA livres'),('5,3','IPCA administrados'),('3,9','IPCA')])
def test_correct_unique_association_preserves_original_value_and_label(value,indicator):
    prepared,domain=candidate(value,indicator)
    result=consolidate(prepared,domain,prepared.blocks,'test');validate_extraction(prepared,result)
    assert result.anchors[0].numeric_facts[0].original==value
    assert result.anchors[0].numeric_facts[0].indicator==indicator


def test_repeated_value_is_left_for_full_semantic_review_without_guessing():
    prepared,domain=candidate('5,2','IPCA administrados')
    result=consolidate(prepared,domain,prepared.blocks,'test')
    assert result.anchors[0].numeric_facts[0].indicator=='IPCA administrados'
    # This guard cannot approve it. Full semantic verification remains required.
    assert result.anchors[0].statement==TABLE


def test_persisted_extraction_cannot_bypass_unique_row_validation():
    prepared,domain=candidate('4,1','IPCA livres')
    result=consolidate(prepared,domain,prepared.blocks,'test');anchor=result.anchors[0]
    corrupt=anchor.numeric_facts[0].model_copy(update={'indicator':'IPCA'})
    result=result.model_copy(update={'anchors':(anchor.model_copy(update={'numeric_facts':(corrupt,)}),)})
    with pytest.raises(ExpectedServiceError) as exc:validate_extraction(prepared,result)
    assert exc.value.code=='numeric_table_mapping'


def test_one_repair_receives_all_proven_row_mismatches_without_dropping_values():
    prepared=source(TABLE);block=prepared.blocks[0];eid=block.excerpt_ids[0]
    data=extraction_payload(prepared,(block,))
    values=[fact(data,eid,value,indicator=indicator) for value,indicator in
        [('4,1','IPCA'),('5,3','IPCA livres'),('3,5','IPCA livres')]]
    wire=parsed_wire({'blocks':{block.block_id:row(eid,values)}})
    before=wire.model_dump_json();domain=resolve_wire_extraction(prepared,wire,(block,))
    with pytest.raises(ExpectedServiceError) as exc:consolidate(prepared,domain,(block,),'test')
    assert exc.value.code=='numeric_table_mapping'
    assert all(repr(value) in str(exc.value) for value in ('4,1','5,3','3,5'))
    assert wire.model_dump_json()==before


@pytest.mark.parametrize('change',['wrapped','header_missing','other_occurrence','generic_indicator'])
def test_uncertain_layout_or_generic_label_is_not_inferred(change):
    prepared,domain=candidate('4,1','IPCA')
    result=domain.blocks[0].anchors[0].numeric_facts[0]
    if change=='wrapped':text=TABLE.replace('IPCA livres 5,2 4,1 3,2','IPCA livres\n5,2 4,1 3,2')
    elif change=='header_missing':text=TABLE.replace('Índice de preços','Indicadores')
    elif change=='other_occurrence':text=TABLE+'Valor citado novamente: 4,1.\n'
    else:
        text=TABLE;result=result.model_copy(update={'indicator':'projeção de inflação'})
    excerpt=prepared.excerpts[0].model_copy(update={'text':text})
    _validate_unambiguous_table_indicator(result,(excerpt,))
