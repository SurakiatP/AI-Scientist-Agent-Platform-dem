"""Pinned inert bundle and packaging checks, not image/runtime acceptance."""
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest
from runtime import prepare_build as build

ROOT = Path(__file__).resolve().parents[2]


def test_full_catalog_manifest_preserves_all_instruction_hashes_without_enablement():
    manifest = json.loads((ROOT / 'runtime/skills-manifest.json').read_bytes())
    registry = json.loads((ROOT / 'docs/skills/capability-registry.json').read_bytes())
    assert len(manifest['skills']) == 177
    assert {s['name'] for s in manifest['skills']} == set(registry['skills'])
    for item in manifest['skills']:
        instruction = next(f for f in item['files'] if f['path'] == 'SKILL.md')
        assert instruction['sha256'] == registry['skills'][item['name']]['audit_source']['skill_sha256']
    payload = {k: v for k, v in manifest.items() if k != 'manifest_sha256'}
    assert manifest['manifest_sha256'] == hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    assert registry['skills']['get-available-resources']['runtime']['image'] == 'prof.worker-base@py3.14.7'
    assert not any(s['statuses']['enabled']['value'] for s in registry['skills'].values())
    assert {'instruction_loader.py', 'capability_registry.py', 'resource_recipe.py',
            'scientific_render.py'} <= set(build.MODULES)


def test_catalog_extra_private_file_or_changed_bytes_fail_before_source_manifest(tmp_path, monkeypatch):
    root = tmp_path / 'source'
    (root / 'runtime').mkdir(parents=True)
    raw = b'fixed instruction'
    data = {'schema_version': 1, 'catalog_commit': build.CATALOG_COMMIT, 'skills': [{
        'name': 'get-available-resources', 'files': [{'path': 'SKILL.md', 'sha256': hashlib.sha256(raw).hexdigest(), 'size': len(raw)}],
    }]}
    data['manifest_sha256'] = hashlib.sha256(json.dumps(data, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    (root / 'runtime/skills-manifest.json').write_text(json.dumps(data))
    def archive(content, extra=False):
        out = io.BytesIO()
        with tarfile.open(fileobj=out, mode='w') as tar:
            entries = [('skills/get-available-resources/SKILL.md', content)]
            if extra:
                entries.append(('skills/get-available-resources/.env', b'synthetic private marker'))
            for name, body in entries:
                item = tarfile.TarInfo(name); item.size = len(body)
                tar.addfile(item, io.BytesIO(body))
        return out.getvalue()
    for i, blob in enumerate((archive(raw, extra=True), archive(b'changed instruction'))):
        monkeypatch.setattr(build, 'archive', lambda *args, blob=blob: blob)
        output = tmp_path / str(i); output.mkdir()
        with pytest.raises(ValueError):
            build._catalog(root, tmp_path, output)
        assert not (output / 'source-hashes.json').exists()
