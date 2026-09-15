"""Action phases keep publishing credentials out of provider processes."""

import argparse
import copy
import json
import os
from pathlib import Path
import re

from . import config as configuration, delivery, service, state
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
    if command.startswith('verify:'):
        prefix = command.split(':')[1]
        matches = [fid for fid in prior['findings'] if fid.startswith(prefix)]
        if len(matches) != 1:
            raise ReviewError('unknown_or_ambiguous_finding_id')
    packet = collect(repo, number, pr, config, prior, force_full=command == 'full' or command.startswith('verify:'))
    if all(lane['paths'] == [] for lane in packet['lanes'].values()):
        prior = state.reuse(prior, run_url)
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
    bundle = {'packet': packet, 'config': config, 'state': prior, 'default_branch': default_branch}
    if command == 'dry-run':
        print(json.dumps({'status': 'dry_run', 'files': len(packet['files']), 'context_files': len(packet['context']),
                          'omitted': len(packet['omitted']), 'packet_id': packet['packet_id'], 'lanes': packet['lanes']}))
        return
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
    output(run=True, publish=True, reason='reserved')
    print('Reserved review; immutable code packet prepared.')


def assert_no_credentials(result):
    encoded = json.dumps(result)
    for name in ('GROK_API_KEY', 'GEMINI_API_KEY', 'AGY_OAUTH_JSON', 'REVIEW_STATE_KEY', 'GH_TOKEN'):
        value = os.environ.get(name, '')
        if value and len(value) >= 16 and value in encoded:
            raise ReviewError('credential_in_review_output')


def review(args):
    bundle = load(Path(args.out) / 'bundle.json')
    # Fail closed if a caller accidentally broadens the inference environment.
    if os.environ.get('GH_TOKEN') or os.environ.get('REVIEW_STATE_KEY'):
        raise ReviewError('publishing_credentials_in_inference_environment')
    for name, value in bundle['config']['effective'].items():
        if os.environ.get(name, '') != value:
            raise ReviewError('provider_configuration_changed_after_reservation')
    result = service.run(bundle)
    assert_no_credentials(result)
    save(Path(args.out) / 'result.json', result)
    (Path(args.out) / 'result.md').write_text(delivery.report(result))
    print('Normalized review status:', result['status'])
    output(status=result['status'])


def publish(args):
    repo, event, default_branch = metadata()
    number, _ = target(os.environ['GITHUB_EVENT_NAME'], event, repo, args.mode)
    if not number:
        raise ReviewError('missing_publication_target')
    bundle = load(Path(args.out) / 'bundle.json')
    result = load(Path(args.out) / 'result.json')
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
    parser.add_argument('phase', choices=('prepare', 'review', 'publish', 'fail', 'validate-config'))
    parser.add_argument('--root', default='.')
    parser.add_argument('--config', default='.github/review.json')
    parser.add_argument('--out', required=True)
    parser.add_argument('--mode', default='review')
    args = parser.parse_args()
    try:
        if args.phase == 'validate-config':
            config = configuration.load(args.root, args.config)
            print('Trusted review configuration valid:', config['config_id'])
        else:
            {'prepare': prepare, 'review': review, 'publish': publish, 'fail': fail}[args.phase](args)
    except ReviewError as exc:
        print('Review stopped:', str(exc))
        raise SystemExit(1) from None
    except (KeyError, TypeError, ValueError, OSError, AttributeError):
        print('Review stopped: invalid_configuration_or_local_io')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
