"""Hash-checked activation artifact for the revised functional M7 MVP."""
from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path

MVP_SOURCE_SHA256 = 'f03423dc25625aa14ae29c10fdacf7b4665b6714ab0667c0dc86a6f77c43665c'


def digest(path: Path) -> str:
    return sha256(path.read_bytes()).hexdigest()


def load_mvp_release(path: str | Path) -> dict:
    artifact_path = Path(path).resolve()
    release = json.loads(artifact_path.read_text(encoding="utf-8"))
    if release.get('version') == 'm7-functional-mvp-release-v2':
        return _load_hybrid_release(artifact_path,release)
    if (release.get("version") != "m7-functional-mvp-release-v1"
        or release.get("status") != "accepted"
        or release.get("configuration") != {"architecture":"literal-v1","model_profile":"economy"}
        or release.get("quality_review") != "approved"):
        raise ValueError("artefato de aceite MVP ausente ou inválido")
    artifacts = release.get("outputs")
    if not isinstance(artifacts, dict) or set(artifacts) != {"analytical_text","informational_carousel","short_video_script"}:
        raise ValueError("aceite MVP exige os três formatos")
    for output_format, row in artifacts.items():
        output = (artifact_path.parent / row["path"]).resolve()
        if artifact_path.parent not in output.parents or not output.is_file() or digest(output) != row.get("sha256"):
            raise ValueError(f"evidência MVP ausente/alterada: {output_format}")
    return release


def _load_hybrid_release(path,release):
    from suno_asset.domain.models import ExecutionResult, PreparedSource
    from suno_asset.services.artifact_lifecycle import runtime_fingerprint
    from suno_asset.services.hybrid_facts import POLICY, CONTROLS
    from suno_asset.services.gemini import configuration
    from suno_asset.experiments.literal_efficiency.contracts import CompactFactPack
    if (release.get('status')!='accepted' or release.get('quality_review')!='approved'
        or release.get('runtime_fingerprint_sha256')!=runtime_fingerprint()
        or release.get('configuration')!={'architecture':'literal-v1','model_profile':'economy',
            'provider':'gemini','models':configuration()['models'],'policy_version':POLICY.version}):
        raise ValueError('aceite híbrido diverge do código/provedor/modelo/política atuais')
    def artifact(row):
        target=(path.parent/row['path']).resolve()
        if path.parent not in target.parents or not target.is_file() or digest(target)!=row.get('sha256'):
            raise ValueError('artefato híbrido ausente/alterado')
        return target
    source=artifact(release['source'])
    if digest(source)!=release.get('source_sha256') or release.get('source_sha256')!=MVP_SOURCE_SHA256:
        raise ValueError('fonte do aceite híbrido divergente')
    pack=CompactFactPack.model_validate_json(artifact(release['factpack']).read_text())
    verdict=pack.verification.get('verdict',{})
    if (pack.source_sha256!=digest(source) or pack.pending_material_checks or not pack.verification.get('approved')
        or set(verdict.get('controls',{}))!=set(CONTROLS) or any(v!='passed' for v in verdict['controls'].values())
        or verdict.get('pending_excerpt_ids') or verdict.get('issues')):
        raise ValueError('ficha híbrida incompleta')
    expected={'analytical_text':'beginner','informational_carousel':'intermediate','short_video_script':'advanced'}
    outputs=release.get('outputs',{})
    if set(outputs)!=set(expected):
        raise ValueError('aceite híbrido exige três formatos')
    cache_keys=set()
    for fmt,audience in expected.items():
        row=outputs[fmt]
        result=ExecutionResult.model_validate_json(artifact(row).read_text())
        review=json.loads(artifact(row['source_first_review']).read_text())
        manifest=result.manifest
        hybrid=manifest.get('hybrid',{})
        if (result.status.value!='approved' or result.content.output_format.value!=fmt
            or result.content.audience.value!=audience or manifest.get('provider')!='gemini'
            or manifest.get('models')!=configuration()['models'] or result.policy_version!=POLICY.version
            or manifest.get('resolved_configuration',{}).get('execution_code_sha256')!=runtime_fingerprint()
            or hybrid.get('source_sha256')!=pack.source_sha256
            or result.errors or not result.fact_verification.approved or not result.evaluation.approved):
            raise ValueError('saída híbrida não qualificada')
        cache_keys.add(hybrid['cache']['key'])
        if (review.get('version')!='hybrid-source-first-review-v1' or review.get('status')!='approved'
            or review.get('source_sha256')!=pack.source_sha256 or review.get('request_id')!=str(result.request_id)
            or review.get('output_sha256')!=row['sha256'] or review.get('issues')
            or review.get('reviewed_before_automatic_verdict') is not True or review.get('factpack_review')!='approved'):
            raise ValueError('revisão source-first ausente ou divergente')
    if len(cache_keys)!=1:
        raise ValueError('formatos não compartilham a mesma preparação')
    operational=json.loads(artifact(release['operational_validation']).read_text())
    if (operational.get('runtime_fingerprint_sha256')!=runtime_fingerprint()
        or not all(operational.get(name) is True for name in
            ('idempotency_after_restart','frontend_passed','regression_280_approved','offline_suite_passed'))):
        raise ValueError('validação operacional híbrida incompleta')
    return release
