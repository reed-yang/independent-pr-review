"""Authenticate bounded PR state; a reservation is recorded before model work."""

import base64
import copy
import hashlib
import hmac
import json
import re
import zlib

from .core import ReviewError, digest, github


MARKER = '<!-- independent-pr-review:v2 -->'
LEGACY_MARKER = '<!-- independent-pr-review:v1 -->'
STATE_RE = re.compile(r'<!-- independent-pr-review-state:v1:([A-Za-z0-9_=-]+):([0-9a-f]{64}) -->')
MAX_RAW = 160000
MAX_ENCODED = 40000
BOT = 'github-actions[bot]'


def key_bytes(key):
    if not isinstance(key, str) or len(key) < 32:
        raise ReviewError('missing_or_short_review_state_key')
    return key.encode()


def encode(value, key):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
    if len(raw) > MAX_RAW:
        raise ReviewError('review_state_capacity')
    payload = base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode()
    if len(payload) > MAX_ENCODED:
        raise ReviewError('review_state_capacity')
    signature = hmac.new(key_bytes(key), payload.encode(), hashlib.sha256).hexdigest()
    return f'<!-- independent-pr-review-state:v1:{payload}:{signature} -->'


def decode(body, key, repo, number):
    matches = STATE_RE.findall(body)
    if len(matches) != 1:
        raise ReviewError('missing_or_invalid_signed_state')
    payload, signature = matches[0]
    if len(payload) > MAX_ENCODED or not hmac.compare_digest(signature, hmac.new(key_bytes(key), payload.encode(), hashlib.sha256).hexdigest()):
        raise ReviewError('invalid_review_state_signature')
    try:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(base64.urlsafe_b64decode(payload), MAX_RAW + 1)
        if len(raw) > MAX_RAW or not decoder.eof or decoder.unused_data:
            raise ValueError()
        value = json.loads(raw)
    except (ValueError, zlib.error, UnicodeError):
        raise ReviewError('invalid_review_state_payload') from None
    if (not isinstance(value, dict) or value.get('version') != 1
            or value.get('repository') != repo or value.get('pr_number') != number):
        raise ReviewError('review_state_identity_mismatch')
    return value


def initial(repo, number):
    return {'version': 1, 'repository': repo, 'pr_number': number, 'paused': False,
            'runs': 0, 'tokens': 0, 'lanes': {}, 'findings': {}, 'reservation': None,
            'status': 'ready', 'last_run': None}


def read(repo, number, key, api=github):
    key_bytes(key)
    found = []
    legacy = []
    for page in range(1, 21):
        comments = api(repo, f'issues/{number}/comments?per_page=100&page={page}')
        for comment in comments:
            if comment['user']['login'] != BOT:
                continue
            if MARKER in comment.get('body', ''):
                found.append(comment)
            elif LEGACY_MARKER in comment.get('body', ''):
                legacy.append(comment)
        if len(comments) < 100:
            break
    else:
        raise ReviewError('comment_history_incomplete')
    if len(found) > 1:
        raise ReviewError('multiple_owned_review_summaries')
    if found:
        return decode(found[0]['body'], key, repo, number), found[0]['id']
    return initial(repo, number), legacy[0]['id'] if len(legacy) == 1 else None


def reserve(state, bundle, run_id, run_url):
    value = copy.deepcopy(state)
    limits = bundle['config']['limits']
    if state['paused']:
        raise ReviewError('review_paused')
    if state['runs'] >= limits['max_runs_per_pr']:
        raise ReviewError('review_run_budget_exhausted')
    # A conservative estimate stays charged after an interrupted run. The token
    # cap is an accounting guard, not a provider-side hard quota.
    prompt_estimate = len(json.dumps(bundle['packet']).encode()) // 3 + 6500
    estimate = prompt_estimate * 4
    if state['tokens'] + estimate > limits['max_tokens_per_pr']:
        raise ReviewError('review_token_budget_exhausted')
    value['runs'] += 1
    value['tokens'] += estimate
    value['reservation'] = {'run_id': run_id, 'bundle_id': digest(bundle), 'estimated_tokens': estimate,
                            'base_sha': bundle['packet']['base_sha'], 'head_sha': bundle['packet']['head_sha']}
    value['status'] = 'in_progress'
    value['last_run'] = run_url
    return value


def accept(state, result, run_id):
    value = copy.deepcopy(state)
    reservation = state.get('reservation') or {}
    if (reservation.get('run_id') != run_id or reservation.get('bundle_id') != result.get('bundle_id')
            or reservation.get('head_sha') != result.get('head_sha') or reservation.get('base_sha') != result.get('base_sha')
            or result.get('repository') != state['repository'] or result.get('pr_number') != state['pr_number']):
        raise ReviewError('stale_or_unbound_review_result')
    value['tokens'] = max(0, state['tokens'] - reservation['estimated_tokens']) + result['accounted_tokens']
    for lane in result['reviews']:
        if lane['status'] == 'completed':
            value['lanes'][lane['slot']] = {'head_sha': result['head_sha'], 'base_sha': result['base_sha'],
                                          'config_id': result['config_id'], 'review': lane}
    for finding in result['findings']:
        old = value['findings'].get(finding['finding_id'], {})
        value['findings'][finding['finding_id']] = {**old, **finding, 'last_checked_sha': result['head_sha']}
    value['status'] = result['status']
    value['head_sha'], value['base_sha'] = result['head_sha'], result['base_sha']
    value['last_reviews'] = [{key: lane.get(key) for key in ('slot', 'status', 'model', 'scope', 'error')} for lane in result['reviews']]
    value['coverage'] = result['coverage']
    value['omitted_count'] = len(result['omitted'])
    value['reservation'] = None
    return value
