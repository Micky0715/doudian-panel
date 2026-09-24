"""Read real reference data and exercise local model-to-Excel generation only.

All generated batches/files live in a temporary directory. No browser or
remote submission component is imported or invoked.
"""

import json
import tempfile
from pathlib import Path

from app.gaia_listing.application.create_batch import sha256_file
from app.gaia_listing.application.generate_batch import prepare_gaia_template, generate_gaia_batch
from app.gaia_listing.application.model_resolution import resolve_operator_models
from app.gaia_listing.infrastructure.batch_repository import GaiaBatchRepository
from app.gaia_listing.infrastructure.excel_inspector import load_profiles, inspect_xlsx
from app.gaia_listing.infrastructure.model_mapping_repository import ModelMappingRepository
from app.gaia_listing.infrastructure.quality_mapping_repository import QualityMappingRepository


def main():
    root = Path(__file__).resolve().parents[1]
    mapping_repo = ModelMappingRepository(root / 'panel_data/gaia_listing/imports/model_mappings')
    metadata = mapping_repo.latest()
    if not metadata or not metadata.get('mainland_filter_applied'):
        raise RuntimeError('没有已通过国行筛选的映射版本')
    _, rows = mapping_repo.load_records(metadata['id'])
    models = [{'model': value} for value in ('Apple iPhone 17 Pro', 'OPPO Find X8', 'Redmi K80')]
    resolved = resolve_operator_models(rows, models)
    assert resolved['success'], resolved
    profiles = load_profiles(str(root / 'config/gaia_excel_profiles.json'))
    templates = root / 'panel_data/gaia_listing/imports/templates'
    candidates = sorted(templates.glob('*.xlsx'), key=lambda path: path.stat().st_mtime, reverse=True)
    source = next(path for path in candidates if any(
        item['id'] == 'gaia_publish_template' for item in inspect_xlsx(str(path), profiles)['profile_matches']))
    original_hash = sha256_file(str(source))
    prepared = prepare_gaia_template(
        source_path=str(source), mapping_version_id=metadata['id'], quality_label='99新', scene=2,
        selected_sku_ids=resolved['selected_sku_ids'], profiles=profiles,
        mapping_repository=mapping_repo,
        quality_repository=QualityMappingRepository(str(root / 'config/gaia_quality_mapping.json')),
    )
    with tempfile.TemporaryDirectory(prefix='gaia-workflow-canary-') as temporary:
        batch = generate_gaia_batch(
            repository=GaiaBatchRepository(temporary + '/batches'), outputs_dir=temporary + '/outputs',
            shop_id='7970406', scene=2, create_count=1, source_path=str(source), profiles=profiles,
            prepared=prepared,
        )
        assert batch.state == 'validation_passed'
        assert batch.summary['generated_rows'] == len(resolved['selected_sku_ids'])
        assert sha256_file(str(source)) == original_hash
        assert all(row.values()[1:3] == ('A', '1') for row in prepared.rows)
        assert [row.values()[3] for row in prepared.rows] == resolved['selected_sku_ids']
        mapping_repo.get(metadata['id'])  # Source and index still match their saved hashes.
        print(json.dumps({
            'success': True, 'remote_submission': False, 'temporary_outputs_only': True,
            'mapping': metadata['name'], 'mapping_version': metadata['id'], 'source_unchanged': True,
            'models': [{'input': item['model_input'], 'sku_count': item['candidate_count']}
                       for item in resolved['results']],
            'generated_rows': batch.summary['generated_rows'], 'quality_id': prepared.quality_id,
            'output_readback': 'passed', 'aggregate': 'A', 'inspection_link': '1',
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
