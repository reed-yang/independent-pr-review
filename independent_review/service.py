"""Independent lane opinions over a repository snapshot and cross-family verification.

Each phase is a pure function of the bundle, earlier phase outputs and the
snapshot, so separate jobs (one provider key each) derive identical plans.
"""

import copy
import json
import time

from .anchors import bind
from .core import ReviewError, digest, input_end_error, input_nonce, runtime_settings, safe_path
from .evidence import match_evidence, rejection
from .prompts import (OBSERVATION_KINDS, REVIEW_SCHEMA, SEVERITIES, VERIFY_SCHEMA, finding_body,
                      review_prompt, verification_prompt)


ACCESS = {'responses_tools': 'tools', 'codex_cli': 'shell'}
MAX_LISTED_PATHS = 500


def harness(name):
    """Import a harness lazily; each inference job loads only its own lane."""
    if name == 'responses_tools':
        from .tool_loop import run
    elif name == 'codex_cli':
        from .codex_runner import run
    else:
        raise ReviewError('harness_not_implemented')
    return run


def usage_tokens(usage, estimate):
    if not isinstance(usage, dict):
        return estimate
    for name in ('total_tokens', 'totalTokenCount'):
        if type(usage.get(name)) is int and usage[name] > 0:
            return usage[name]
    values = [usage[key] for key in ('input_tokens', 'output_tokens') if type(usage.get(key)) is int and usage[key] >= 0]
    return sum(values) if values and sum(values) else estimate


def lane_backend(config, slot_id):
    slot = next((slot for slot in config['backends']['slots'] if slot['id'] == slot_id), None)
    if slot is None:
        raise ReviewError('unknown_review_lane')
    return slot, slot['backends'][0], config['backends']['backends'][slot['backends'][0]]


def runner_for(backend, runners):
    return (runners or {}).get(backend['harness']) or harness(backend['harness'])


def parse_json(raw, code):
    if not isinstance(raw, str):
        raise ReviewError(code)
    raw = raw.strip()
    if raw.startswith('```json\n') and raw.endswith('```'):
        raw = raw[8:-3].strip()
    try:
        value = json.loads(raw)
    except ValueError:
        raise ReviewError(code) from None
    if not isinstance(value, dict):
        raise ReviewError(code)
    return value


class Sources:
    """Evidence sources for changed files (brief patch first) and any snapshot path."""

    def __init__(self, brief, workspace):
        self.changed = {entry['path']: entry for entry in brief['files']}
        self.workspace = workspace
        self.cache = {}

    def entry(self, path):
        if not safe_path(path):
            return None
        if path not in self.cache:
            changed = self.changed.get(path, {})
            entry = self.workspace.evidence_entry(path, changed.get('patch'), changed.get('previous_filename'))
            if changed.get('previous_filename'):
                entry['previous_filename'] = changed['previous_filename']
            self.cache[path] = entry
        return self.cache[path]


def text_list(values, limit, size, code):
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ReviewError(code)
    return [value[:size] for value in values[:limit]]


def parse_review(raw, brief, sources, nonce):
    obj = parse_json(raw, 'invalid_review_json')
    if not isinstance(obj.get('summary'), str) or not isinstance(obj.get('findings'), list) or len(obj['findings']) > 5:
        raise ReviewError('invalid_review_schema')
    limitations = text_list(obj.get('limitations', []), 20, 2000, 'invalid_limitations')
    examined = [path for path in text_list(obj.get('files_examined', []), MAX_LISTED_PATHS, 500, 'invalid_files_examined')
                if safe_path(path)]
    observations = []
    for item in obj.get('observations', [])[:20] if isinstance(obj.get('observations'), list) else []:
        if (isinstance(item, dict) and item.get('kind') in OBSERVATION_KINDS
                and isinstance(item.get('path'), str) and isinstance(item.get('text'), str)):
            observations.append({'path': item['path'][:500], 'kind': item['kind'], 'text': item['text'][:2000]})
    valid, rejected = [], []
    for index, finding in enumerate(obj['findings']):
        try:
            if not isinstance(finding, dict) or not isinstance(finding.get('path'), str) or finding['path'] not in sources.changed:
                raise ReviewError('invalid_finding_location_or_severity')
            if finding.get('severity') not in SEVERITIES:
                raise ReviewError('invalid_finding_location_or_severity')
            for key in ('title', 'mechanism', 'consequence', 'evidence'):
                if not isinstance(finding.get(key), str) or not 1 <= len(finding[key]) <= 4000:
                    raise ReviewError('invalid_finding_text')
            steps = finding.get('trigger_steps')
            if (not isinstance(steps, list) or not 1 <= len(steps) <= 12
                    or not all(isinstance(step, str) and 1 <= len(step) <= 1000 for step in steps)):
                raise ReviewError('invalid_finding_trigger')
            entry = sources.entry(finding['path'])
            source = match_evidence(entry, finding['evidence'], allow_base=True)
            if not source:
                code = 'finding_evidence_too_short' if len(finding['evidence'].strip()) < 8 else 'finding_evidence_not_in_snapshot'
                raise ReviewError(code)
            line = finding.get('line')
            if line is not None:
                lines = (entry.get('head_text') or '').splitlines()
                if type(line) is not int or line < 1:
                    raise ReviewError('invalid_finding_line')
                if line > len(lines) or not any(part.strip() and part.strip() in lines[line - 1]
                                                for part in finding['evidence'].splitlines()):
                    line = None
            normalized = {key: finding[key] for key in
                          ('path', 'severity', 'title', 'trigger_steps', 'mechanism', 'consequence', 'evidence')}
            normalized.update(body=finding_body(finding), line=line, evidence_source=source)
            valid.append(bind(normalized, entry))
        except ReviewError as exc:
            rejected.append(rejection(index, finding, str(exc), sources.changed))
    return {'summary': obj['summary'][:3000], 'limitations': limitations, 'observations': observations,
            'files_examined': examined, 'findings': valid, 'rejected_findings': rejected,
            'input_end_error': input_end_error(obj, nonce)}


def generate(bundle, slot_id, workspace, runners=None):
    brief, config, prior = bundle['packet'], bundle['config'], bundle['state']
    slot, backend_id, backend = lane_backend(config, slot_id)
    lane = brief['lanes'][slot_id]
    base = {'slot': slot_id, 'opinion_family': slot['opinion_family'], 'scope': lane['reason']}
    cached = prior['lanes'].get(slot_id, {}).get('review')
    if lane['paths'] == [] and cached:
        return {**copy.deepcopy(cached), 'status': 'reused', 'scope': lane['reason']}
    if not lane.get('generate', True):
        return {**base, 'status': 'skipped', 'findings': []}
    settings = runtime_settings(backend)
    identity = {'backend': backend_id, 'harness': backend['harness'], 'auth_mode': backend['auth_mode']}
    start = time.monotonic()
    stage, usage = 'provider', None
    try:
        nonce = input_nonce()
        task = {'kind': 'review', 'schema': REVIEW_SCHEMA, 'effort': settings['effort'],
                'prompt': review_prompt(brief, lane, ACCESS[backend['harness']], nonce)}
        raw, model, usage, trace = runner_for(backend, runners)(backend, task, workspace)
        stage = 'validation'
        review = parse_review(raw, brief, Sources(brief, workspace), nonce)
        incomplete = review.pop('input_end_error')
        status = 'partial' if review['rejected_findings'] or incomplete else 'completed'
        elapsed = round(time.monotonic() - start, 2)
        attempt = {'backend': backend_id, 'status': status, 'elapsed_seconds': elapsed,
                   **({'error': incomplete, 'stage': stage} if incomplete else {})}
        return {**base, **identity, **settings, 'status': status, 'model': model, 'usage': usage, 'trace': trace,
                'elapsed_seconds': elapsed, 'attempts': [attempt], **({'error': incomplete} if incomplete else {}),
                **review}
    except ReviewError as exc:
        elapsed = round(time.monotonic() - start, 2)
        return {**base, **identity, **settings, 'status': 'failed', 'error': str(exc), 'findings': [],
                'usage': usage, 'elapsed_seconds': elapsed,
                'attempts': [{'backend': backend_id, 'status': 'failed', 'error': str(exc), 'stage': stage,
                              'elapsed_seconds': elapsed, 'diagnostics': exc.diagnostics}]}


# Generation failures that a verification call in the same run would repeat. Transient
# provider errors and timeouts still allow the lane to verify the other family's candidates.
PERSISTENT_ERRORS = frozenset({
    'backend_not_configured', 'https_endpoint_required', 'invalid_context_window', 'invalid_tool_budget',
    'unsupported_reasoning_effort', 'http_400', 'http_401', 'http_403', 'http_404', 'provider_dns_error',
    'provider_tls_error', 'codex_not_found', 'codex_version_unknown', 'codex_version_mismatch',
    'invalid_codex_model', 'invalid_proxy_url', 'invalid_proxy_limits', 'credential_in_output'})


def plan(bundle, reviews, sources):
    """Deterministically select candidates and assign each to the other family."""
    brief, config, prior = bundle['packet'], bundle['config'], bundle['state']
    slots = config['backends']['slots']
    order = [slot['id'] for slot in slots]
    # Phases may list lane results in different orders; the plan must not depend on it.
    reviews = sorted(reviews, key=lambda review: order.index(review['slot']) if review['slot'] in order else len(order))
    candidates = {}
    for review in reviews:
        if review['status'] not in ('completed', 'partial'):
            continue
        for finding in review['findings']:
            fid = finding['finding_id']
            # Carry a signed identity across renames and wording changes.
            for old_id, old in prior['findings'].items():
                same_path = old['path'] == finding['path'] or any(
                    entry.get('previous_filename') == old['path'] and entry['path'] == finding['path']
                    for entry in brief['files'])
                if same_path and ''.join(old['evidence'].split()) == ''.join(finding['evidence'].split()) and old.get('scope', '') == finding.get('scope', ''):
                    fid = old_id
                    # A rediscovered published finding keeps its protection against dismissal.
                    finding = {**finding, 'finding_id': fid, 'published': bool(old.get('comment_id'))}
                    break
            if fid not in candidates:
                candidates[fid] = {**finding, 'sources': [], 'previous': False}
            if review['slot'] not in candidates[fid]['sources']:
                candidates[fid]['sources'].append(review['slot'])
    # Recheck unresolved findings after a new head; absence is never a fix signal.
    # A dismissed finding whose file changed since a lane baseline is rechecked too.
    if any(review['status'] not in ('reused', 'skipped') for review in reviews):
        renames = {entry['previous_filename']: entry['path'] for entry in brief['files'] if 'previous_filename' in entry}
        touched = {path for lane in brief['lanes'].values() for path in (lane.get('paths') or [])}
        for fid, old in sorted(prior['findings'].items()):
            if fid in candidates or not (old['status'] in ('open', 'uncertain') or
                                         (old['status'] == 'dismissed' and old['path'] in touched)):
                continue
            item = {**copy.deepcopy(old), 'previous': True, 'published': bool(old.get('comment_id'))}
            item['path'] = renames.get(item['path'], item['path'])
            entry = sources.entry(item['path']) if item['path'] in sources.changed else None
            item['anchor'] = bind(item, entry)['anchor'] if entry else None
            candidates[fid] = item
    limit = config['limits']['max_verification_candidates']
    ordered = sorted(candidates.values(), key=lambda item: (
        item['finding_id'] != bundle.get('verify_finding'), not item.get('previous'),
        item.get('status') == 'dismissed', item['severity'], item['finding_id']))
    chosen, overflow = ordered[:limit], ordered[limit:]
    batches = {slot['id']: [] for slot in slots}
    for candidate in chosen:
        sources_ = candidate.get('sources', [])
        verifier = next((slot for slot in slots if slot['id'] not in sources_), slots[-1])
        batches[verifier['id']].append(candidate)
    return ordered, overflow, batches


def parse_decisions(raw, candidates, sources, nonce):
    obj = parse_json(raw, 'invalid_verification_json')
    decisions = obj.get('decisions')
    ids = {candidate['finding_id']: candidate for candidate in candidates}
    if not isinstance(decisions, list) or len(decisions) != len(ids):
        raise ReviewError('incomplete_verification')
    valid, rejected = {}, []
    for index, decision in enumerate(decisions):
        if (not isinstance(decision, dict) or not isinstance(decision.get('finding_id'), str)
                or decision['finding_id'] not in ids or decision['finding_id'] in valid):
            raise ReviewError('invalid_verification_identity')
        status = decision.get('status')
        if status not in ('confirmed', 'dismissed', 'fixed', 'uncertain'):
            raise ReviewError('invalid_verification_status')
        fields = {key: decision.get(key, '') for key in ('reason', 'failing_step', 'evidence_path', 'evidence')}
        if not all(isinstance(value, str) for value in fields.values()) or not 1 <= len(fields['reason']) <= 3000:
            raise ReviewError('invalid_verification_text')
        fid = decision['finding_id']
        if status == 'fixed' and not ids[fid].get('previous'):
            raise ReviewError('new_candidate_cannot_be_fixed')
        if status != 'uncertain':
            entry = sources.entry(fields['evidence_path'])
            if not entry or not match_evidence(entry, fields['evidence'], allow_base=True, current_only=status == 'fixed'):
                rejected.append({**rejection(index, {'path': fields['evidence_path'], 'evidence': fields['evidence']},
                                             'verification_evidence_not_in_snapshot', sources.changed),
                                 'finding_id': fid})
                valid[fid] = {'finding_id': fid, 'status': 'uncertain', 'failing_step': '', 'evidence_path': '', 'evidence': '',
                              'reason': 'Verification evidence did not match the snapshot; the decision was rejected.'}
                continue
        valid[fid] = {'finding_id': fid, 'status': status, **{key: value[:3000] for key, value in fields.items()}}
    return {'decisions': valid, 'rejected_decisions': rejected, 'input_end_error': input_end_error(obj, nonce)}


def verify(bundle, slot_id, reviews, workspace, runners=None):
    brief, config = bundle['packet'], bundle['config']
    sources = Sources(brief, workspace)
    _, _, batches = plan(bundle, reviews, sources)
    batch = batches[slot_id]
    if not batch:
        return {'slot': slot_id, 'status': 'not_needed', 'decisions': {}, 'accounted_tokens': 0}
    _, backend_id, backend = lane_backend(config, slot_id)
    generation = next((review for review in reviews if review['slot'] == slot_id), {})
    if generation.get('status') == 'failed' and any(attempt.get('stage') == 'provider' and attempt.get('error') in PERSISTENT_ERRORS
                                                    for attempt in generation.get('attempts', [])):
        return {'slot': slot_id, 'status': 'failed', 'error': 'verification_skipped_after_provider_failure',
                'decisions': {}, 'accounted_tokens': 0, 'elapsed_seconds': 0}
    settings = runtime_settings(backend)
    effort = settings.get('verify_effort') or settings['effort']
    start = time.monotonic()
    usage = None
    estimate = backend.get('verification_reservation_tokens', 0)
    try:
        nonce = input_nonce()
        task = {'kind': 'verify', 'schema': VERIFY_SCHEMA, 'effort': effort,
                'prompt': verification_prompt(brief, batch, ACCESS[backend['harness']], nonce)}
        raw, model, usage, trace = runner_for(backend, runners)(backend, task, workspace)
        parsed = parse_decisions(raw, batch, sources, nonce)
        # Valid decisions are kept, but a verifier that may have seen only a
        # prefix of its input cannot complete the batch.
        error = parsed.pop('input_end_error') or ('verification_evidence_not_in_snapshot' if parsed['rejected_decisions'] else None)
        return {'slot': slot_id, 'backend': backend_id, 'status': 'partial' if error else 'completed', 'model': model,
                'effort': effort, **parsed, **({'error': error} if error else {}), 'usage': usage, 'trace': trace,
                'accounted_tokens': usage_tokens(usage, estimate), 'elapsed_seconds': round(time.monotonic() - start, 2)}
    except ReviewError as exc:
        return {'slot': slot_id, 'backend': backend_id, 'status': 'failed', 'error': str(exc), 'decisions': {},
                'effort': effort, 'accounted_tokens': usage_tokens(usage, estimate), 'usage': usage,
                'elapsed_seconds': round(time.monotonic() - start, 2), 'diagnostics': exc.diagnostics}


def combine(bundle, reviews, verifications, workspace):
    """Merge lane opinions and verification decisions into one normalized result."""
    brief, config = bundle['packet'], bundle['config']
    slots = config['backends']['slots']
    ordered, overflow, batches = plan(bundle, reviews, Sources(brief, workspace))
    by_slot = {item['slot']: item for item in verifications}
    for slot in slots:
        if batches[slot['id']] and slot['id'] not in by_slot:
            by_slot[slot['id']] = {'slot': slot['id'], 'status': 'failed', 'error': 'verification_result_missing',
                                   'decisions': {}, 'accounted_tokens': 0}
    verifications = [by_slot[slot['id']] for slot in slots if slot['id'] in by_slot]
    decisions = {fid: {**decision, 'verifier': result['slot']} for result in verifications
                 for fid, decision in result['decisions'].items()}
    findings = []
    for item in ordered:
        decision = decisions.get(item['finding_id'], {'status': 'uncertain', 'reason': 'Verification unavailable or candidate budget reached.'})
        status = {'confirmed': 'open', 'fixed': 'fixed', 'dismissed': 'dismissed', 'uncertain': 'uncertain'}[decision['status']]
        # A published issue needs a demonstrated fix to leave the PR; an
        # unpublished one may be dismissed like a new candidate.
        if item.get('published') and status == 'dismissed':
            status = 'uncertain'
        findings.append({**item, 'status': status, 'verification': decision})
    verification_failed = any(item['status'] not in ('completed', 'not_needed') for item in verifications) or bool(overflow)
    if verification_failed:
        for review in reviews:
            if review['status'] == 'completed':
                review.update(status='partial', error='verification_incomplete')
    complete = all(review['status'] in ('completed', 'reused', 'skipped') for review in reviews) and not verification_failed
    accounted = sum(usage_tokens(review.get('usage'), lane_backend(config, review['slot'])[2].get('reservation_tokens', 0))
                    for review in reviews if review['status'] in ('completed', 'partial', 'failed'))
    accounted += sum(item.get('accounted_tokens', 0) for item in verifications)
    return {'schema_version': 3, 'repository': brief['repository'], 'pr_number': brief['pr_number'],
            'base_sha': brief['base_sha'], 'merge_base_sha': brief['merge_base_sha'], 'head_sha': brief['head_sha'],
            'packet_id': brief['packet_id'], 'config_id': config['config_id'], 'bundle_id': digest(bundle),
            'engine_version': config.get('engine_version'), 'description_truncated': bool(brief.get('description_truncated')),
            'description_chars': brief.get('description_chars'),
            'status': 'completed' if complete else 'partial', 'coverage': brief['coverage'],
            'omitted': brief['omitted'], 'stats': brief.get('stats', {}), 'reviews': reviews,
            'verifications': verifications, 'findings': findings, 'accounted_tokens': accounted}
