"""Load only trusted consumer policy and expose bounded portable settings."""

from fnmatch import fnmatch
import json
import os
from pathlib import Path
import re

from .core import ReviewError, digest, runtime_settings
from . import __version__


DEFAULTS = {
    'packet_chars': 4000000, 'context_chars': 2500000, 'max_context_files': 100,
    'max_api_reads': 300, 'max_runs_per_pr': 8, 'max_tokens_per_pr': 8000000,
    'max_inline_comments': 5, 'max_verification_candidates': 10,
}


def trusted_file(root, name):
    root = Path(root).resolve()
    if not isinstance(name, str) or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ReviewError('invalid_policy_path')
    candidate = root / name
    if candidate.is_symlink() or not candidate.resolve().is_relative_to(root) or not candidate.is_file():
        raise ReviewError('invalid_policy_path')
    if candidate.stat().st_size > 80000:
        raise ReviewError('policy_too_large')
    return candidate


def load(root, path):
    raw = json.loads(trusted_file(root, path).read_text())
    if not isinstance(raw, dict) or raw.get('version') != 1:
        raise ReviewError('unsupported_configuration')
    backend_path = raw.get('backends')
    backends = json.loads(trusted_file(root, backend_path).read_text()) if backend_path else json.loads(Path(__file__).with_name('backends.json').read_text())
    slots = backends.get('slots', [])
    if len(slots) != 2 or len({slot.get('id') for slot in slots}) != 2 or len({slot.get('opinion_family') for slot in slots}) != 2:
        raise ReviewError('two_independent_reviewers_required')
    effective = {}
    for slot in slots:
        if len(slot['backends']) != 1:
            raise ReviewError('automatic_provider_fallback_not_supported')
        backend = backends['backends'][slot['backends'][0]]
        if backend.get('opinion_family') != slot['opinion_family']:
            raise ReviewError('reviewer_family_mismatch')
        for key in ('model_env', 'base_url_env', 'key_env', 'oauth_env', 'binary_env', 'state_env', 'effort_env', 'context_window_env'):
            name = backend.get(key)
            if name and not re.fullmatch(r'[A-Z][A-Z0-9_]{0,80}', name):
                raise ReviewError('invalid_backend_environment_name')
        for key in ('model_env', 'base_url_env', 'effort_env', 'context_window_env'):
            if key in backend:
                effective[backend[key]] = os.environ.get(backend[key], '')
        if backend.get('harness') not in ('compatible_packet', 'gemini_packet', 'antigravity_packet'):
            raise ReviewError('unsupported_harness')
        if backend.get('api', 'chat_completions') not in ('chat_completions', 'responses'):
            raise ReviewError('unsupported_compatible_api')
        if not 10 <= backend.get('timeout_seconds', 0) <= 600:
            raise ReviewError('invalid_provider_timeout')
        for field in ('connect_timeout_seconds', 'idle_timeout_seconds'):
            if field in backend and (type(backend[field]) is not int or not 1 <= backend[field] <= backend['timeout_seconds']):
                raise ReviewError('invalid_provider_timeout')
    limits = {**DEFAULTS, **raw.get('limits', {})}
    ceilings = {'packet_chars': 8000000, 'context_chars': 6000000, 'max_context_files': 200,
                'max_api_reads': 600, 'max_runs_per_pr': 100, 'max_tokens_per_pr': 10000000,
                'max_inline_comments': 10, 'max_verification_candidates': 10}
    if set(limits) != set(DEFAULTS) or any(type(v) is not int or not 1 <= v <= ceilings[k] for k, v in limits.items()):
        raise ReviewError('invalid_review_limits')
    rules = []
    for file in raw.get('rules', []):
        rules.append({'id': file, 'paths': ['*'], 'text': trusted_file(root, file).read_text()})
    for policy in raw.get('policies', []):
        if not isinstance(policy.get('paths'), list) or not all(isinstance(p, str) for p in policy['paths']):
            raise ReviewError('invalid_policy_scope')
        rules.append({'id': policy['id'], 'paths': policy['paths'], 'text': trusted_file(root, policy['file']).read_text()})
    if sum(len(rule['text']) for rule in rules) > 50000:
        raise ReviewError('combined_policy_too_large')
    context = raw.get('context', {})
    for key in ('include', 'source_roots'):
        if key in context and (not isinstance(context[key], list) or not all(isinstance(p, str) for p in context[key])):
            raise ReviewError('invalid_context_scope')
    config = {'version': 1, 'backends': backends, 'effective': effective, 'limits': limits,
              'rules': rules, 'context': context,
              'runtime': {slot['id']: runtime_settings(backends['backends'][slot['backends'][0]]) for slot in slots}, 'inline_comments': raw.get('inline_comments', True),
              'verification': raw.get('verification', True), 'engine_version': __version__}
    if type(config['inline_comments']) is not bool or config['verification'] is not True:
        raise ReviewError('verified_publication_required')
    # Code and native release changes invalidate reuse even without a version bump.
    package = Path(__file__).parent
    config['engine_id'] = digest({p.name: p.read_text() for p in sorted(package.iterdir())
                                  if p.suffix in ('.py', '.md', '.json')})
    config['config_id'] = digest(config)
    return config


def selected_rules(config, paths):
    return [{'id': rule['id'], 'text': rule['text']} for rule in config['rules']
            if any(fnmatch(path, pattern) for path in paths for pattern in rule['paths'])]
