"""Offline stage preflights do not imply a complete run fits its global cap."""
import pytest

from suno_asset.m7_budget import aggregate_plan_budget, aggregate_round_budget


@pytest.fixture
def official_source():
    import json
    import os
    from hashlib import sha256
    from pathlib import Path

    source=next(row for row in json.loads(Path('tests/fixtures/evaluation/corpus.json').read_text())['sources']
        if row['id']=='copom281-20260916')
    supplied=os.environ.get('SUNO_COPOM281_PDF')
    path=Path(supplied or source['local_pdf'])
    if not path.is_file() and not supplied:
        pytest.skip('defina SUNO_COPOM281_PDF para o planejamento com a ata oficial')
    assert path.is_file(), 'SUNO_COPOM281_PDF deve apontar para um PDF existente'
    assert sha256(path.read_bytes()).hexdigest()==source['sha256'], 'PDF diferente da fonte de referência'
    return {**source,'local_pdf':str(path.resolve())}


def stage(name,tokens,retry,requests=1):
    return {'stage':name,'reserved_stage_tokens':tokens,'retry_reserve':retry,'requests':requests,
        'estimated_stage_seconds':(tokens+retry)*60/8000+requests*2,'model':'openai/gpt-oss-120b'}


MANIFEST={'max_tokens':250000,'max_calls':64,'deadline_seconds':3600,'tpm_limit':8000}


def test_aggregate_stage_sum_and_re_evaluation_branches_are_explicit():
    plans=[stage('extract',40000,5000,10),stage('verify',40000,5000,10),stage('generate',50000,5000,10),
        stage('evaluate',45000,5000,10),stage('refine',55000,5000,10)]
    budget=aggregate_plan_budget(plans,MANIFEST)
    assert budget['observed_planned_stage_sum_with_retries']==255000
    assert budget['observed_planned_stages_fit_token_cap'] is False
    assert budget['branches']['0']['reserved_tokens_with_retries']==195000
    assert budget['branches']['1']['reserved_tokens_with_retries']==305000
    assert budget['branches']['2']['reserved_tokens_with_retries']==415000
    assert budget['branches']['1']['evaluations']==2 and budget['branches']['2']['evaluations']==3
    assert budget['branches']['0']['proxy_fits'] is True
    assert budget['branches']['1']['proxy_fits'] is False
    assert budget['actual_output_feasibility']=='unknown'


def test_missing_stages_never_mean_zero_or_complete_fit():
    budget=aggregate_plan_budget([stage('extract',1000,500)],MANIFEST)
    assert budget['branches']['0']['proxy_fits'] is None
    assert budget['branches']['2']['complete_proxy_plan'] is False
    assert budget['branches']['2']['missing_stage_plans']==['evaluate','generate','refine','verify']
    result=aggregate_round_budget([{'aggregate_budget':budget}],10,2)
    assert result['proxy_unknown_runs']==1 and result['proxy_cost_fits_round_cap'] is None


def test_transport_worst_and_corrections_do_not_claim_retry_guarantee():
    plans=[stage(name,10000,1000,3) for name in ('extract','verify','generate','evaluate','refine')]
    budget=aggregate_plan_budget(plans,MANIFEST)
    branch=budget['branches']['0']
    assert branch['reserved_tokens_with_retries']==44000
    assert branch['transport_worst_proxy']['reserved_tokens']==120000
    assert branch['transport_worst_proxy']['reserved_calls']==36
    assert branch['transport_and_extraction_correction_proxy']['reserved_tokens']==150000
    assert branch['transport_and_extraction_correction_proxy']['reserved_calls']==45
    assert branch['transport_and_extraction_correction_proxy']['correction_prompt_overhead']=='unknown'
    assert budget['retry_completion_guaranteed'] is False


def test_round_reports_infeasible_branches_and_cost_too_large():
    plans=[stage(name,60000,1000,8) for name in ('extract','verify','generate','evaluate','refine')]
    budget=aggregate_plan_budget(plans,MANIFEST)
    result=aggregate_round_budget([{'aggregate_budget':budget}]*18,.01,2)
    assert result['proxy_infeasible_runs']==18
    assert result['proxy_cost_fits_round_cap'] is False
    assert result['actual_output_feasibility']=='unknown'


@pytest.mark.parametrize('configuration', ['current-quality','literal-quality','literal-hybrid'])
def test_offline_plan_attempts_every_stage_without_transport(tmp_path, configuration, official_source):
    from suno_asset.m7_literal_eval import plan_case
    source=official_source
    row=plan_case(source,configuration,tmp_path/configuration,max_refinements=2)
    assert row['paid'] is False
    assert row['attempted_stages']==['extract','verify','generate','evaluate','refine']
    assert {'extract','verify','generate','evaluate','refine'} <= set(
        row['planned_stages'] + row['unplannable_stages'])
    assert row['aggregate_budget']['branches']['2']['proxy_fits'] is not True
    assert row['aggregate_budget']['actual_output_feasibility']=='unknown'


def test_literal_281_reports_proxy_caps_and_public_daily_limit_without_claiming_real_fit(tmp_path, official_source):
    from suno_asset.m7_literal_eval import plan_case

    source=official_source
    row=plan_case(source,'literal-quality',tmp_path/'literal-quality',max_refinements=0)
    branch=row['aggregate_budget']['branches']['0']

    assert branch['complete_proxy_plan'] is True
    # Wire ownership and prompt changes alter the proxy size. Assert the
    # reported admission decision, rather than freezing an old token estimate.
    assert branch['caps']=={'tokens':250000,'calls':64,'seconds':2400}
    assert branch['reserved_tokens_with_retries']>200000
    fits_tokens=branch['reserved_tokens_with_retries']<=250000
    assert branch['fits_caps']=={'tokens':fits_tokens,'calls':True,'seconds':True}
    if not fits_tokens:
        assert branch['proxy_fits'] is False
        assert any(error['code']=='offline_proxy_budget_exceeded' for error in row['errors'])
    assert branch['reserved_calls_with_retries']<=64
    assert branch['estimated_seconds_with_retries']<=2400
    assert row['aggregate_budget']['actual_output_feasibility']=='unknown'


def test_historical_281_evaluation_stages_are_planned_with_compact_global_checks(tmp_path, official_source):
    from suno_asset.m7_literal_eval import plan_case

    source=official_source
    row=plan_case(source,'current-quality',tmp_path/'current-quality',max_refinements=0)

    assert 'evaluate' in row['planned_stages']
    assert 'evaluate' not in row['unplannable_stages']
    assert not any(error['stage']=='evaluate' for error in row['errors'])


@pytest.mark.parametrize('configuration',['literal-quality','literal-hybrid'])
def test_full_ata_refinement_and_two_evaluations_fit_a_separate_execution(tmp_path,configuration,official_source):
    from suno_asset.m7_literal_refinement import plan_refinement
    source=official_source

    plan=plan_refinement(source,configuration,tmp_path/configuration)

    assert plan['paid'] is False and plan['actual_output_feasibility']=='unknown'
    assert plan['complete_proxy_plan'] and plan['proxy_fits']
    assert {'evaluate','refine'} <= set(plan['stages'])
    assert plan['stages']['evaluate']['requests'] >= 2
    assert plan['totals']['reserved_tokens']<=250000
    assert plan['totals']['reserved_calls']<=64
    assert plan['totals']['estimated_seconds']<2400
    assert all(item['fits'] for stage in plan['stages'].values() for item in stage['per_request'])
