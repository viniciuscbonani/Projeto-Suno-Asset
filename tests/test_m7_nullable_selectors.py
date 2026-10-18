"""Equivalent nullable scoped selectors; no transport or external schema engine."""
import json
from pathlib import Path

from suno_asset.services.literal import _schema_for,EXTRACTION_SCHEMA,_numeric_metadata_catalog
from test_literal_selectors import source


def test_nullable_selectors_remain_scoped_and_null_without_nested_anyof():
    prepared=source('1. A pesquisa Focus projeta IPCA de 5,2% em 2026.')
    schema=_schema_for(EXTRACTION_SCHEMA,prepared,prepared.blocks)
    for block in prepared.blocks:
        fields=schema['properties']['blocks']['properties'][block.block_id]['anyOf'][0]['properties']['anchors']['items']['properties']['numeric_facts']['items']['properties']
        catalog=_numeric_metadata_catalog(prepared,(block,))
        for field in ('indicator','unit','period','attribution'):
            selected=[item['id'] for item in catalog if item['field']==field]
            assert fields[field+'_id']==({'type':['string','null'],'enum':[*selected,None]} if selected else {'type':'null'})


def test_empty_selector_catalog_still_accepts_only_null():
    prepared=source('1. A atividade desacelerou.')
    fields=_schema_for(EXTRACTION_SCHEMA,prepared,prepared.blocks)['properties']['blocks']['properties'][prepared.blocks[0].block_id]['anyOf'][0]['properties']['anchors']['items']['properties']['numeric_facts']['items']['properties']
    assert all(fields[field+'_id']=={'type':'null'} for field in ('indicator','unit','period','attribution'))


def test_old_prompt_and_wire_snapshot_is_preserved():
    # Snapshot is local to this candidate; historical strings remain available.
    snapshot=json.loads((Path(__file__).parent/'fixtures/literal-v1.11-prompts.json').read_text())
    assert snapshot['version']=='literal-v1.11'
    assert snapshot['wire_fact_schema']['properties']['unit_id']=={'anyOf':[{'type':'string'},{'type':'null'}]}
