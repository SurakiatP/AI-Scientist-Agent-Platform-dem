"""Reuse an independently accepted immutable image; requests cannot supply evidence."""
from contextlib import contextmanager
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import stat
from typing import Mapping

from scientist.profile_preparation import BuildFailure, BuildProof, Profile, _validate_proof

ROOT = Path(__file__).resolve().parents[3]
DOMAIN = b'scientist.accepted-profile.v1\0'
COMPUTE_DOMAIN = b'scientist.accepted-compute.v1\0'
WORKER_PROFILE_ID = 'prof.worker-base@py3.14.7'
COMPUTE_PROFILE_ID = 'prof.csv-stdlib@py3.14.7'
_SHA = re.compile(r'^[a-f0-9]{64}$')
_FILE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,80}\.json$')
SOURCE_FILES = frozenset({
    *(f'backend/src/scientist/{name}.py' for name in (
        '__init__', 'contracts', 'runtime_contracts', 'model_payload', 'runtime_adapter',
        'capability_registry', 'instruction_loader', 'resource_recipe', 'private_worker_api',
        'profile_preparation', 'profile_evidence', 'scientific_authority')),
    'runtime/entrypoint.py', 'runtime/skills-manifest.json', 'runtime/requirements.lock',
    'runtime/profiles/worker-base.json', 'docs/skills/capability-registry.json',
})
COMPUTE_SOURCE_FILES = frozenset({
    'runtime/compute_entrypoint.py',
    'backend/src/scientist/cpu_recipes.py',
    'backend/src/scientist/scientific_render.py',
})


def _bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False).encode()


@contextmanager
def _directory(path: Path, *, private: bool):
    # Open each component relative to its pinned parent: ancestor swaps cannot
    # redirect reads through symlinks after a separate lstat check.
    path = path.absolute()
    if '..' in path.parts:
        raise ValueError('unsafe path')
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_NONBLOCK
    descriptor = os.open(path.anchor, flags)
    try:
        for component in path.parts[1:]:
            child = os.open(component, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        info = os.fstat(descriptor)
        if private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700):
            raise ValueError('unsafe receipt directory')
        yield descriptor
    finally:
        os.close(descriptor)


def _chunks(path: Path, maximum: int, *, private: bool = False):
    with _directory(path.parent, private=private) as parent:
        descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        try:
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_size > maximum
                    or private and (info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022)):
                raise ValueError('unsafe evidence')
            count = 0
            while chunk := os.read(descriptor, min(65536, maximum - count + 1)):
                count += len(chunk)
                if count > maximum:
                    raise ValueError('oversized evidence')
                yield chunk
        finally:
            os.close(descriptor)


def _digest(path: Path, maximum: int, *, private: bool = False) -> str:
    digest = sha256()
    for chunk in _chunks(path, maximum, private=private):
        digest.update(chunk)
    return digest.hexdigest()


def _object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError('duplicate receipt key')
        result[name] = value
    return result


def accepted_image_builder(
    directory: Path | Mapping[str, Path],
    *,
    key: bytes,
    expected_image_digest: str | None = None,
    expected_image_digests: Mapping[str, str] | None = None,
    source_root: Path = ROOT,
):
    """Reuse receipts only for exact profile IDs and independently pinned images."""
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError('invalid evidence authentication key')
    if expected_image_digest is not None and expected_image_digests is not None:
        raise ValueError('conflicting image pins')
    if expected_image_digests is None:
        if isinstance(directory, Mapping) or not isinstance(expected_image_digest, str):
            raise ValueError('worker receipt and image pin are required')
        directories = {WORKER_PROFILE_ID: Path(directory)}
        image_digests = {WORKER_PROFILE_ID: expected_image_digest}
    else:
        if not isinstance(directory, Mapping):
            raise ValueError('profile receipt directories are required')
        directories = dict(directory)
        image_digests = dict(expected_image_digests)
        allowed = {WORKER_PROFILE_ID, COMPUTE_PROFILE_ID}
        if (set(directories) != set(image_digests) or WORKER_PROFILE_ID not in image_digests
                or set(image_digests) - allowed):
            raise ValueError('profile receipt configuration is invalid')
        if len({str(Path(path).absolute()) for path in directories.values()}) != len(directories):
            raise ValueError('profile receipt directories must be distinct')
    if any(not isinstance(pin, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', pin)
           for pin in image_digests.values()):
        raise ValueError('invalid immutable image digest')
    if set(directories) - {WORKER_PROFILE_ID, COMPUTE_PROFILE_ID}:
        raise ValueError('unknown profile receipt directory')
    source_root = Path(source_root)

    def verify(profile: Profile, job_id):
        try:
            profile_id = profile.profile_id
            if profile_id not in directories or profile_id not in image_digests:
                raise ValueError('profile receipt is not configured')
            directory = Path(directories[profile_id])
            expected = image_digests[profile_id]
            compute = profile_id == COMPUTE_PROFILE_ID
            domain = COMPUTE_DOMAIN if compute else DOMAIN
            source_files = COMPUTE_SOURCE_FILES if compute else SOURCE_FILES
            receipt = directory / 'accepted.json'
            envelope = json.loads(
                b''.join(_chunks(receipt, 1024 * 1024, private=True)), object_pairs_hook=_object
            )
            if not isinstance(envelope, dict) or set(envelope) != {
                'proof', 'files', 'source_sha256', 'signature'
            }:
                raise ValueError('invalid acceptance receipt')
            payload = {k: v for k, v in envelope.items() if k != 'signature'}
            signature = envelope['signature']
            signed = hmac.digest(key, domain + _bytes(payload), 'sha256').hex()
            if not isinstance(signature, str) or not _SHA.fullmatch(signature) or not hmac.compare_digest(signature, signed):
                raise ValueError('unaccepted evidence')
            files = payload['files']
            if not isinstance(files, dict) or not 5 <= len(files) <= 20:
                raise ValueError('missing actual evidence')
            for name, digest in files.items():
                if (not isinstance(name, str) or not _FILE.fullmatch(name) or name == 'accepted.json'
                        or not isinstance(digest, str) or not _SHA.fullmatch(digest)
                        or _digest(directory / name, 64 * 1024 * 1024, private=True) != digest):
                    raise ValueError('evidence changed')
            sources = payload['source_sha256']
            if not isinstance(sources, dict) or set(sources) != source_files:
                raise ValueError('incomplete source binding')
            for relative, digest in sources.items():
                path = source_root / relative
                if not isinstance(digest, str) or not _SHA.fullmatch(digest):
                    raise ValueError('source changed')
                try:
                    current = _digest(path, 16 * 1024 * 1024)
                except FileNotFoundError:
                    if relative == 'docs/skills/capability-registry.json':
                        current = _digest(source_root / 'runtime/capability-registry.json', 16 * 1024 * 1024)
                    else:
                        raise
                if current != digest:
                    raise ValueError('source changed')
            proof = BuildProof.model_validate(payload['proof'])
            if proof.image_digest != expected:
                raise ValueError('environment changed')
            if compute:
                manifest_path = directory / 'recipe-manifest.json'
                manifest_raw = b''.join(_chunks(manifest_path, 1024 * 1024, private=True))
                manifest = json.loads(manifest_raw, object_pairs_hook=_object)
                expected_names = ('csv_describe.py', 'cpu_recipes.py', 'scientific_render.py')
                source_names = (
                    'runtime/compute_entrypoint.py',
                    'backend/src/scientist/cpu_recipes.py',
                    'backend/src/scientist/scientific_render.py',
                )
                if (not isinstance(manifest, dict) or set(manifest) != {'schema_version', 'files'}
                        or manifest['schema_version'] != 1 or not isinstance(manifest['files'], list)
                        or manifest_raw != _bytes(manifest) or len(manifest['files']) != 3
                        or sha256(manifest_raw).hexdigest() != proof.recipe_manifest_sha256):
                    raise ValueError('compute recipe manifest changed')
                for item, name, source in zip(manifest['files'], expected_names, source_names, strict=True):
                    if (not isinstance(item, dict) or set(item) != {'name', 'sha256'}
                            or item['name'] != name or item['sha256'] != sources[source]):
                        raise ValueError('compute recipe source differs from staged manifest')
                if files.get('recipe-manifest.json') != sha256(manifest_raw).hexdigest():
                    raise ValueError('compute recipe manifest missing from evidence')
            elif proof.recipe_manifest_sha256 is not None:
                raise ValueError('worker receipt contains a compute manifest')
            if not {
                proof.source_manifest_sha256, proof.scan.report_sha256, proof.sbom_sha256,
                proof.containment.isolation_test_sha256,
            } <= set(files.values()):
                raise ValueError('proof has no matching actual files')
            raw = proof.model_copy(update={'job_id': job_id}).model_dump(mode='json')
            return _validate_proof(raw, profile, job_id).model_dump(mode='json')
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise BuildFailure('accepted_environment_unavailable') from exc

    return verify
