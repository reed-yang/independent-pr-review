"""Authenticate bounded PR state; a reservation is recorded before model work."""

import base64
import copy
import hashlib
import hmac
import json
import re
import zlib

from .core import ReviewError, digest, github, failure_description


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
    # A conservative per-lane estimate stays charged after an interrupted run. The
    # token cap is an accounting guard, not a provider-side hard quota.
    lanes = bundle['packet']['lanes']
    estimate = 0
    for slot in bundle['config']['backends']['slots']:
        backend = bundle['config']['backends']['backends'][slot['backends'][0]]
        lane = lanes[slot['id']]
        if lane.get('generate', True) and lane['paths'] != []:
            estimate += backend.get('reservation_tokens', 0)
        estimate += backend.get('verification_reservation_tokens', 0)
    if state['tokens'] + estimate > limits['max_tokens_per_pr']:
        raise ReviewError('review_token_budget_exhausted')
    value['runs'] += 1
    value['tokens'] += estimate
    value['reservation'] = {'run_id': run_id, 'bundle_id': digest(bundle), 'estimated_tokens': estimate,
                            'base_sha': bundle['packet']['base_sha'], 'head_sha': bundle['packet']['head_sha']}
    value['status'] = 'in_progress'
    value['last_run'] = run_url
    return value


def baseline_review(lane):
    """Keep what reuse and the summary need; traces and full findings stay in the run artifacts."""
    keep = ('slot', 'status', 'opinion_family', 'backend', 'harness', 'auth_mode', 'model', 'effort',
            'context_window_tokens', 'scope', 'summary', 'elapsed_seconds')
    value = {key: copy.deepcopy(lane[key]) for key in keep if key in lane}
    # A reused lane's findings are not candidates again; the full records live in state findings.
    value['findings'] = [{key: finding.get(key) for key in ('finding_id', 'severity', 'path', 'line')}
                         | {'title': str(finding.get('title', ''))[:300]} for finding in lane.get('findings', [])]
    value.update(lane_access(lane), **lane_notes(lane))
    return value


def lane_notes(lane):
    """Bounded reviewer limitations and observations shown in the summary."""
    limitations = lane.get('limitations') if isinstance(lane.get('limitations'), list) else []
    observations = lane.get('observations') if isinstance(lane.get('observations'), list) else []
    return {'limitations': [text[:300] for text in limitations if isinstance(text, str)][:3],
            'observations': [{key: str(item.get(key, ''))[:size] for key, size in (('kind', 40), ('path', 300), ('text', 500))}
                             for item in observations if isinstance(item, dict)][:5]}


def lane_access(lane):
    """Bounded counts that show how much of the repository a reviewer actually read."""
    if 'access' in lane:
        # A reused baseline already carries its counts.
        return {'access': lane['access']}
    trace = lane.get('trace') or {}
    return {'access': {'files_examined': len(lane.get('files_examined') or []),
                       'tool_calls': trace.get('tool_calls') or 0, 'files_read': len(trace.get('files_read') or [])}}


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
                                          'config_id': result['config_id'], 'review': baseline_review(lane)}
    for finding in result['findings']:
        old = value['findings'].get(finding['finding_id'], {})
        value['findings'][finding['finding_id']] = {**old, **finding, 'last_checked_sha': result['head_sha']}
    value['status'] = result['status']
    value['head_sha'], value['base_sha'] = result['head_sha'], result['base_sha']
    value['last_reviews'] = [{**{key: lane.get(key) for key in
                               ('slot', 'status', 'model', 'scope', 'error', 'effort', 'context_window_tokens', 'elapsed_seconds')},
                              **lane_access(lane), **lane_notes(lane),
                              'errors': [item['error'] for item in lane.get('attempts', []) if item.get('error')],
                              'failure_notes': [note for item in lane.get('attempts', []) if (note := failure_description(item))],
                              'rejected_count': len(lane.get('rejected_findings', []))} for lane in result['reviews']]
    value.update({key: result.get(key) for key in ('merge_base_sha', 'config_id', 'engine_version')})
    value['description_truncated'] = bool(result.get('description_truncated'))
    value['description_chars'] = result['description_chars'] if type(result.get('description_chars')) is int else None
    value['coverage'] = result['coverage']
    value['omitted_count'] = len(result['omitted'])
    value['reservation'] = None
    return value


def reuse(state, run_url, lanes=None):
    """Mark an unchanged run; a lane its generation policy skipped is shown as skipped."""
    value = copy.deepcopy(state)
    value['status'] = 'completed'
    value['last_run'] = run_url
    value['reservation'] = None
    reviews = []
    for lane in value.get('last_reviews', []):
        plan = (lanes or {}).get(lane.get('slot')) or {}
        if plan and plan.get('paths') != [] and not plan.get('generate', True):
            lane = {'slot': lane.get('slot'), 'status': 'skipped', 'scope': plan.get('reason'),
                    'access': {'files_examined': 0, 'tool_calls': 0, 'files_read': 0}, 'limitations': [], 'observations': []}
        else:
            lane = {**lane, 'status': 'reused', 'scope': 'identical_successful_snapshot'}
        reviews.append({**lane, 'errors': [], 'failure_notes': [], 'rejected_count': 0})
    value['last_reviews'] = reviews
    return value
