"""Apply reviewed, fixed-size UI patches to exactly the identified test engine.

Each module proposes byte ranges against the original engine. All proposals
are validated together before any range is changed; modules never write APKs.
"""
import hashlib
import importlib

SOURCE_SHA256 = '2cea0c85344d7d7f295aea1c2353621f02db78dbccd888a0a21a0508ac330659'
LIBRARY = 'lib/arm64-v8a/libUE4.so'
PATCH_MODULES = ('patch_native_skill_ui', 'patch_native_fps_ui')


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def apply_plan(source, proposals, *, expected_sha256=SOURCE_SHA256):
    """Validate a complete plan, then return patched bytes and an audit record."""
    if not isinstance(source, bytes) or sha256(source) != expected_sha256:
        raise ValueError('Native source SHA-256 does not match the reviewed engine')
    plan = list(proposals)
    if not plan:
        raise ValueError('No native UI patch was supplied')
    for patch in plan:
        if not isinstance(patch, dict):
            raise ValueError('Invalid native patch record')
        offset, before, after = patch.get('offset'), patch.get('before'), patch.get('after')
        if type(offset) is not int or offset < 0:
            raise ValueError('Invalid native patch offset')
        if not isinstance(before, bytes) or not isinstance(after, bytes):
            raise ValueError('Native patch bytes must be explicit')
        if not before or len(before) != len(after) or before == after:
            raise ValueError('Native patch must change a nonempty, fixed-size range')
        if offset + len(before) > len(source) or source[offset:offset + len(before)] != before:
            raise ValueError('Native patch original bytes do not match')
        if not isinstance(patch.get('name'), str) or not patch['name'].strip():
            raise ValueError('Native patch must have an audit name')
    plan.sort(key=lambda patch: patch['offset'])
    previous_end = 0
    for patch in plan:
        if patch['offset'] < previous_end:
            raise ValueError('Native patch ranges overlap')
        previous_end = patch['offset'] + len(patch['before'])
    result = bytearray(source)
    for patch in plan:
        offset = patch['offset']
        result[offset:offset + len(patch['after'])] = patch['after']
    result = bytes(result)
    # Verify the entire unchanged area as well as every replacement. Do not
    # exempt the whole library from the build's payload preservation checks.
    cursor = 0
    for patch in plan:
        offset, end = patch['offset'], patch['offset'] + len(patch['after'])
        if result[cursor:offset] != source[cursor:offset] or result[offset:end] != patch['after']:
            raise ValueError('Native patch footprint verification failed')
        cursor = end
    if len(result) != len(source) or result[cursor:] != source[cursor:]:
        raise ValueError('Native patch changed unreviewed bytes')
    report = {
        'library': LIBRARY, 'sourceSha256': expected_sha256, 'patchedSha256': sha256(result),
        'bytes': len(result), 'unmodifiedRangesVerified': True,
        'ranges': [dict(name=p['name'], offset=p['offset'], bytes=len(p['before']),
                        before=p['before'].hex(), after=p['after'].hex()) for p in plan],
    }
    return result, report


def patch_library(source):
    if sha256(source) != SOURCE_SHA256:
        raise ValueError('Native source SHA-256 does not match the reviewed engine')
    plan = []
    for name in PATCH_MODULES:
        plan.extend(importlib.import_module(name).patches(source))
    result, report = apply_plan(source, plan)
    report['modules'] = list(PATCH_MODULES)
    return result, report
