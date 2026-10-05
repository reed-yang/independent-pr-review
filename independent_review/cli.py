"""Action phases keep publishing credentials out of provider processes.

prepare -> generate (per lane) -> verify (per lane) -> publish. Each inference
phase holds one provider key and no GitHub token or state key.
"""

import argparse
import copy
import json
import os
from pathlib import Path
import re

from . import config as configuration, delivery, service, snapshot, state
from .context import collect, eligible
from .core import ReviewError, digest, github, load, save


COMMAND = re.compile(r'^/review(?: (full|pause|resume)| verify ([0-9a-f]{8,24}))?$')


def output(**values):
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a') as handle:
            for key, value in values.items():
                handle.write(f'{key}={str(value).lower() if isinstance(value, bool) else value}\n')


def target(event_name, event, repo, mode, api=github):
    """Accept a closed command grammar only after checking current permission."""
    if event_name == 'issue_comment':
        if event.get('action') != 'created' or not event.get('issue', {}).get('pull_request'):
            return None, 'ignored'
        comment = event.get('comment', {})
        author = comment.get('user', {})
        match = COMMAND.fullmatch(comment.get('body', '').strip())
        if author.get('type') == 'Bot' or not match:
            return None, 'ignored'
        login = author.get('login', '')
        if not re.fullmatch(r'[A-Za-z0-9-]{1,39}', login):
            return None, 'ignored'
        permission = api(repo, f'collaborators/{login}/permission').get('permission')
        if permission not in ('admin', 'maintain', 'write'):
            return None, 'unauthorized'
        return event['issue']['number'], 'verify:' + match[2] if match[2] else match[1] or 'review'
    if event_name == 'pull_request_target':
        return event['pull_request']['number'], 'review'
    if event_name == 'workflow_dispatch':
        if mode not in ('dry-run', 'review', 'full'):
            raise ReviewError('invalid_review_mode')
        number = event.get('inputs', {}).get('pr_number') or os.environ.get('REVIEW_PR', '')
        try:
            number = int(number)
        except (ValueError, TypeError):
            raise ReviewError('invalid_pr_number') from None
        if number < 1:
            raise ReviewError('invalid_pr_number')
        return number, mode
    raise ReviewError('unsupported_review_event')


def trigger_context(event_name, event, command):
    """Describe what started this run for per-lane generation policies."""
    pr = event.get('pull_request') or {}
    labels = [label.get('name', '') for label in pr.get('labels', []) if isinstance(label, dict)]
    if event_name == 'pull_request_target' and event.get('action') == 'labeled':
        labels.append((event.get('label') or {}).get('name', ''))
    action = event.get('action') if event_name == 'pull_request_target' else None
    return {'command': command.split(':')[0], 'action': action, 'labels': sorted({label for label in labels if label})}


def metadata():
    repo = os.environ.get('GITHUB_REPOSITORY', '')
    event = load(os.environ['GITHUB_EVENT_PATH'])
    if event.get('repository', {}).get('full_name') != repo:
        raise ReviewError('event_repository_mismatch')
    default_branch = github(repo, '')['default_branch']
    if os.environ.get('GITHUB_REF') != 'refs/heads/' + default_branch:
        raise ReviewError('trusted_default_branch_required')
    return repo, event, default_branch


def run_identity(repo):
    run_id = os.environ['GITHUB_RUN_ID'] + ':' + os.environ.get('GITHUB_RUN_ATTEMPT', '1')
    if not re.fullmatch(r'[0-9]+:[0-9]+', run_id):
        raise ReviewError('invalid_run_identity')
    return run_id, f"https://github.com/{repo}/actions/runs/{os.environ['GITHUB_RUN_ID']}"


def prepare(args):
    repo, event, default_branch = metadata()
    number, command = target(os.environ['GITHUB_EVENT_NAME'], event, repo, args.mode)
    output(run=False, publish=False, reason=command)
    if number is None:
        print('Review command ignored:', command)
        return
    output(pr_number=number)
    config = configuration.load(args.root, args.config)
    key = os.environ.get('REVIEW_STATE_KEY', '')
    prior, comment_id = state.read(repo, number, key)
    pr = github(repo, f'pulls/{number}')
    run_id, run_url = run_identity(repo)
    publish = os.environ.get('REVIEW_PUBLISH', 'false') == 'true' and command != 'dry-run'
    if command in ('pause', 'resume'):
        prior['paused'] = command == 'pause'
        prior['status'] = 'paused' if prior['paused'] else 'ready'
        prior['last_run'] = run_url
        if publish:
            delivery.write_summary(prior, comment_id, key, config['limits'])
        if command == 'pause':
            return
    if not eligible(pr, repo, default_branch):
        if publish and comment_id:
            prior['status'] = 'ineligible'
            prior['reservation'] = None
            delivery.write_summary(prior, comment_id, key, config['limits'])
        print('Review skipped: PR is closed, draft, forked, or targets another branch.')
        return
    if prior['paused']:
        print('Review skipped: paused.')
        return
    verify_finding = None
    if command.startswith('verify:'):
        prefix = command.split(':')[1]
        matches = [fid for fid in prior['findings'] if fid.startswith(prefix)]
        if len(matches) != 1:
            raise ReviewError('unknown_or_ambiguous_finding_id')
        verify_finding = matches[0]
        if prior['findings'][verify_finding]['status'] not in ('open', 'uncertain'):
            raise ReviewError('finding_already_resolved')
    force_full = command == 'full' or command.startswith('verify:')
    # A successful identical snapshot needs neither source downloads nor inference.
    same_snapshot = not force_full and all(
        prior['lanes'].get(slot['id'], {}).get('head_sha') == pr['head']['sha'] and
        prior['lanes'][slot['id']].get('base_sha') == pr['base']['sha'] and
        prior['lanes'][slot['id']].get('config_id') == config['config_id']
        for slot in config['backends']['slots'])
    trigger = trigger_context(os.environ['GITHUB_EVENT_NAME'], event, command)
    if not trigger['labels']:
        trigger['labels'] = sorted({label['name'] for label in pr.get('labels', []) if isinstance(label, dict) and label.get('name')})
    packet = None if same_snapshot else collect(repo, number, pr, config, prior, trigger, force_full=force_full)
    # Nothing to generate (unchanged, or skipped by policy) means nothing new to verify.
    if same_snapshot or all(lane['paths'] == [] or not lane['generate'] for lane in packet['lanes'].values()):
        prior = state.reuse(prior, run_url, packet and packet['lanes'])
        if publish:
            delivery.publish_inline(prior, config, default_branch)
            delivery.write_summary(prior, comment_id, key, config['limits'])
        print('Identical successful snapshot reused; no provider calls or run reservation.')
        output(reason='reused')
        return
    if not packet['files']:
        prior['status'] = 'no_reviewable_text'
        prior['last_run'] = run_url
        if publish:
            delivery.write_summary(prior, comment_id, key, config['limits'])
        print('No reviewable text; no model calls were reserved.')
        return
    if command == 'dry-run':
        print(json.dumps({'status': 'dry_run', 'files': len(packet['files']), 'stats': packet['stats'],
                          'omitted': len(packet['omitted']), 'packet_id': packet['packet_id'], 'lanes': packet['lanes']}))
        return
    record = snapshot.create(f'https://github.com/{repo}.git', packet['head_sha'], packet['merge_base_sha'],
                             args.out, token=os.environ.get('GH_TOKEN'))
    bundle = {'packet': packet, 'config': config, 'state': prior, 'default_branch': default_branch,
              'verify_finding': verify_finding, 'snapshot': record}
    if not publish:
        raise ReviewError('publication_required_for_durable_budget_reservation')
    try:
        reserved = state.reserve(prior, bundle, run_id, run_url)
    except ReviewError as exc:
        if str(exc) not in ('review_run_budget_exhausted', 'review_token_budget_exhausted'):
            raise
        prior['status'] = str(exc)
        prior['last_run'] = run_url
        delivery.write_summary(prior, comment_id, key, config['limits'])
        output(reason=str(exc))
        print(str(exc))
        return
    delivery.current_pr(reserved, default_branch)
    delivery.write_summary(reserved, comment_id, key, config['limits'])
    save(Path(args.out) / 'bundle.json', bundle)
    lanes = [slot['id'] for slot in config['backends']['slots']]
    output(run=True, publish=True, reason='reserved', lanes=json.dumps(lanes, separators=(',', ':')))
    print('Reserved review; brief and repository snapshot prepared.')


CREDENTIAL_NAMES = ('GROK_API_KEY', 'GPT_API_KEY', 'GEMINI_API_KEY', 'AGY_OAUTH_JSON', 'REVIEW_STATE_KEY', 'GH_TOKEN')


def assert_no_credentials(result, secrets=()):
    encoded = json.dumps(result)
    for value in [os.environ.get(name, '') for name in CREDENTIAL_NAMES] + list(secrets):
        if value and len(value) >= 16 and value in encoded:
            raise ReviewError('credential_in_review_output')


def inference_context(args):
    """Load one lane's inputs after checking the job holds no publishing credentials."""
    bundle = load(Path(args.out) / 'bundle.json')
    # Fail closed if a caller accidentally broadens the inference environment.
    if os.environ.get('GH_TOKEN') or os.environ.get('REVIEW_STATE_KEY'):
        raise ReviewError('publishing_credentials_in_inference_environment')
    _, _, backend = service.lane_backend(bundle['config'], args.lane)
    for key in ('model_env', 'base_url_env', 'effort_env', 'verify_effort_env', 'context_window_env'):
        name = backend.get(key)
        if name and os.environ.get(name, '') != bundle['config']['effective'].get(name, ''):
            raise ReviewError('provider_configuration_changed_after_reservation')
    from .hardening import harden_process
    harden_process()
    workspace = snapshot.Snapshot.open(args.out, bundle['snapshot'], Path(args.work) / args.lane)
    return bundle, workspace


def lane_results(out, prefix, slots):
    results = []
    for slot in slots:
        path = Path(out) / f"{prefix}-{slot['id']}.json"
        if path.exists():
            value = load(path)
            if value.get('slot') != slot['id']:
                raise ReviewError('lane_result_identity_mismatch')
            results.append(value)
    return results


def generate(args):
    bundle, workspace = inference_context(args)
    review = service.generate(bundle, args.lane, workspace)
    assert_no_credentials(review)
    save(Path(args.out) / f'lane-{args.lane}.json', review)
    print(f'{args.lane} opinion status:', review['status'])


def verify(args):
    bundle, workspace = inference_context(args)
    slots = bundle['config']['backends']['slots']
    reviews = lane_results(args.out, 'lane', slots)
    for slot in slots:
        if not any(review['slot'] == slot['id'] for review in reviews):
            reviews.append(missing_lane(slot))
    result = service.verify(bundle, args.lane, reviews, workspace)
    assert_no_credentials(result)
    save(Path(args.out) / f'verify-{args.lane}.json', result)
    print(f'{args.lane} verification status:', result['status'])


def missing_lane(slot):
    return {'slot': slot['id'], 'opinion_family': slot['opinion_family'], 'status': 'failed',
            'error': 'lane_result_missing', 'scope': 'lane_result_missing', 'findings': [],
            'attempts': [{'status': 'failed', 'error': 'lane_result_missing', 'stage': 'job'}]}


def combine(args, bundle):
    """Merge per-lane artifacts in the publishing job; no model is called here."""
    slots = bundle['config']['backends']['slots']
    reviews = lane_results(args.out, 'lane', slots)
    for slot in slots:
        if not any(review['slot'] == slot['id'] for review in reviews):
            reviews.append(missing_lane(slot))
    reviews.sort(key=lambda review: [slot['id'] for slot in slots].index(review['slot']))
    workspace = snapshot.Snapshot.open(args.out, bundle['snapshot'], Path(args.work) / 'publish')
    result = service.combine(bundle, reviews, lane_results(args.out, 'verify', slots), workspace)
    save(Path(args.out) / 'result.json', result)
    (Path(args.out) / 'result.md').write_text(delivery.report(result))
    print('Normalized review status:', result['status'])
    return result


def publish(args):
    repo, event, default_branch = metadata()
    number, _ = target(os.environ['GITHUB_EVENT_NAME'], event, repo, args.mode)
    if not number:
        raise ReviewError('missing_publication_target')
    bundle = load(Path(args.out) / 'bundle.json')
    result = combine(args, bundle)
    if digest(bundle) != result.get('bundle_id') or bundle['default_branch'] != default_branch:
        raise ReviewError('review_artifact_identity_mismatch')
    key = os.environ.get('REVIEW_STATE_KEY', '')
    current, comment_id = state.read(repo, number, key)
    run_id, _ = run_identity(repo)
    delivery.current_pr(current, default_branch)
    updated = state.accept(current, result, run_id)
    # Persist verified results before network-dependent inline delivery. The
    # owned comment markers allow recovery after an interrupted POST response.
    comment_id = delivery.write_summary(updated, comment_id, key, bundle['config']['limits'])
    try:
        delivery.publish_inline(updated, bundle['config'], default_branch)
    except ReviewError:
        updated['status'] = 'publication_partial'
        delivery.write_summary(updated, comment_id, key, bundle['config']['limits'])
        raise
    delivery.current_pr(updated, default_branch)
    delivery.write_summary(updated, comment_id, key, bundle['config']['limits'])
    print('Updated English PR summary and eligible verified inline findings.')
    if result['status'] != 'completed':
        raise ReviewError('independent_review_incomplete')


def fail(args):
    repo, event, _ = metadata()
    number, _ = target(os.environ['GITHUB_EVENT_NAME'], event, repo, args.mode)
    if not number:
        raise ReviewError('missing_publication_target')
    key = os.environ.get('REVIEW_STATE_KEY', '')
    current, comment_id = state.read(repo, number, key)
    reservation = current.get('reservation') or {}
    if reservation.get('run_id') != run_identity(repo)[0]:
        raise ReviewError('stale_or_unbound_review_failure')
    bundle = load(Path(args.out) / 'bundle.json')
    if digest(bundle) != reservation.get('bundle_id'):
        raise ReviewError('review_artifact_identity_mismatch')
    current.update(status='failed', head_sha=reservation['head_sha'], base_sha=reservation['base_sha'], reservation=None)
    delivery.write_summary(current, comment_id, key, bundle['config']['limits'])
    raise ReviewError('review_failed_reservation_retained')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('phase', choices=('prepare', 'generate', 'verify', 'publish', 'fail', 'validate-config'))
    parser.add_argument('--root', default='.')
    parser.add_argument('--config', default='.github/review.json')
    parser.add_argument('--out', required=True)
    parser.add_argument('--mode', default='review')
    parser.add_argument('--lane', default='')
    parser.add_argument('--work', default='')
    args = parser.parse_args()
    try:
        if args.phase == 'validate-config':
            config = configuration.load(args.root, args.config)
            print('Trusted review configuration valid:', config['config_id'])
        else:
            if args.phase in ('generate', 'verify', 'publish') and not args.work:
                raise ReviewError('work_directory_required')
            {'prepare': prepare, 'generate': generate, 'verify': verify, 'publish': publish, 'fail': fail}[args.phase](args)
    except ReviewError as exc:
        print('Review stopped:', str(exc))
        raise SystemExit(1) from None
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        print('Review stopped: invalid_configuration_or_local_io')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
