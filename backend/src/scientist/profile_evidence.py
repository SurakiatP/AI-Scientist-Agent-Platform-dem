"""Reuse an independently accepted immutable image; requests cannot supply evidence."""
from contextlib import contextmanager
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path
import re
import stat

from scientist.profile_preparation import BuildFailure, BuildProof, Profile, _validate_proof

ROOT = Path(__file__).resolve().parents[3]
DOMAIN = b'scientist.accepted-profile.v1\0'
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


def accepted_image_builder(directory: Path, *, key: bytes, expected_image_digest: str, source_root: Path = ROOT):
    """Only the trusted host supplies the private receipt directory and authentication key.

    The operator creates accepted.json after independent build/security/isolation
    acceptance. It signs {proof,files,source_sha256}; this loader cannot issue that
    acceptance. The physical image build is reused, not launched again on refresh.
    """
    if not isinstance(key, bytes) or len(key) < 32:
        raise ValueError('invalid evidence authentication key')
    if not isinstance(expected_image_digest, str) or not re.fullmatch(r'sha256:[a-f0-9]{64}', expected_image_digest):
        raise ValueError('invalid immutable image digest')
    directory = Path(directory)
    source_root = Path(source_root)

    def verify(profile: Profile, job_id):
        try:
            receipt = directory / 'accepted.json'
            envelope = json.loads(b''.join(_chunks(receipt, 1024 * 1024, private=True)), object_pairs_hook=_object)
            if not isinstance(envelope, dict) or set(envelope) != {'proof', 'files', 'source_sha256', 'signature'}:
                raise ValueError('invalid acceptance receipt')
            payload = {k: v for k, v in envelope.items() if k != 'signature'}
            signature = envelope['signature']
            expected = hmac.digest(key, DOMAIN + _bytes(payload), 'sha256').hex()
            if not isinstance(signature, str) or not _SHA.fullmatch(signature) or not hmac.compare_digest(signature, expected):
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
            if not isinstance(sources, dict) or set(sources) != SOURCE_FILES:
                raise ValueError('incomplete source binding')
            for relative, digest in sources.items():
                path = source_root / relative
                if not isinstance(digest, str) or not _SHA.fullmatch(digest):
                    raise ValueError('source changed')
                try:
                    current = _digest(path, 16 * 1024 * 1024)
                except FileNotFoundError:
                    if relative != 'docs/skills/capability-registry.json':
                        raise
                    current = _digest(source_root / 'runtime/capability-registry.json', 16 * 1024 * 1024)
                if current != digest:
                    raise ValueError('source changed')
            proof = BuildProof.model_validate(payload['proof'])
            if proof.image_digest != expected_image_digest:
                raise ValueError('environment changed')
            # These raw evidence identities were independently accepted, and remain mandatory on reuse.
            if not {proof.source_manifest_sha256, proof.scan.report_sha256, proof.sbom_sha256,
                    proof.containment.isolation_test_sha256} <= set(files.values()):
                raise ValueError('proof has no matching actual files')
            raw = proof.model_copy(update={'job_id': job_id}).model_dump(mode='json')
            return _validate_proof(raw, profile, job_id).model_dump(mode='json')
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            raise BuildFailure('accepted_environment_unavailable') from exc

    return verify
