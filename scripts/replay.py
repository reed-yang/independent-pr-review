"""Replay one historical PR through the review pipeline locally, without GitHub.

The brief and snapshot come from a local clone; generation, verification and
combination run exactly as in Actions. Nothing is published. With --fake, a
provider-free scripted runner exercises the pipeline end to end.

On macOS a sandboxed Codex command can read the launch environment of any
same-user process, so provider keys must not be exported to this script when
the Codex lane runs; --keychain-service reads them into this process instead.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from independent_review import config as configuration, context, delivery, service, snapshot, state  # noqa: E402
from independent_review.core import save  # noqa: E402


def git(repo, *args):
    return subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True, text=True).stdout


def pr_files(repo, merge_base, head):
    """Mimic GitHub's PR file list: status, counts and per-file hunks."""
    statuses = {}
    for row in git(repo, 'diff', '--name-status', '-M', merge_base, head).splitlines():
        parts = row.split('\t')
        kind = parts[0][0]
        path = parts[-1]
        statuses[path] = ({'A': 'added', 'D': 'removed', 'R': 'renamed', 'M': 'modified'}.get(kind, 'modified'),
                          parts[1] if kind == 'R' else None)
    items = []
    for path, (status, previous) in statuses.items():
        body = git(repo, 'diff', '-M', merge_base, head, '--', *([previous, path] if previous else [path]))
        patch = body[body.find('\n@@') + 1:] if '\n@@' in body else None
        rows = (patch or '').splitlines()
        added = sum(1 for row in rows if row.startswith('+'))
        deleted = sum(1 for row in rows if row.startswith('-'))
        item = {'filename': path, 'status': status, 'additions': added, 'deletions': deleted, 'patch': patch}
        if previous:
            item['previous_filename'] = previous
        items.append(item)
    return items


def load_keys(config, wanted, service_name):
    """Read each selected lane's provider key from the macOS Keychain into this process only."""
    backends = [config['backends']['backends'][slot['backends'][0]]
                for slot in config['backends']['slots'] if slot['id'] in wanted]
    names = [backend['key_env'] for backend in backends if backend.get('key_env')]
    exposed = [name for name in names if os.environ.get(name)]
    if sys.platform == 'darwin' and exposed and any(backend['harness'] == 'codex_cli' for backend in backends):
        sys.exit(f"unset {', '.join(exposed)}: sandboxed Codex commands can read this process's launch environment")
    keys = {}
    for name in names if service_name else ():
        account = name.lower().replace('_', '-')
        value = subprocess.run(['security', 'find-generic-password', '-s', service_name, '-a', account, '-w'],
                               capture_output=True, text=True).stdout.strip()
        if not value:
            sys.exit(f'no Keychain item for service {service_name} account {account}')
        keys[name] = value
    return keys


def fake_runner(backend, task, workspace):
    """Return a schema-valid answer echoing the nonce; candidates are verified as uncertain."""
    nonce = task['prompt'].rsplit('END_OF_INPUT_NONCE=', 1)[1].strip()
    if task['kind'] == 'review':
        answer = {'summary': 'Scripted replay opinion.', 'files_examined': [], 'limitations': ['fake runner'],
                  'observations': [], 'findings': [], 'input_end_nonce': nonce}
    else:
        ids = json.loads(task['prompt'].split('Candidates JSON: ', 1)[1].rsplit('\nEND_OF_INPUT_NONCE=', 1)[0])
        answer = {'decisions': [{'finding_id': item['finding_id'], 'status': 'uncertain', 'reason': 'fake runner',
                                 'failing_step': '', 'evidence_path': '', 'evidence': ''} for item in ids],
                  'input_end_nonce': nonce}
    return json.dumps(answer), 'fake', {'total_tokens': 1, 'requests': 1}, {'tool_calls': 0, 'stopped_reason': 'fake'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--repo', required=True, help='local clone containing both commits')
    parser.add_argument('--repository', required=True, help='owner/name recorded in the brief')
    parser.add_argument('--pr', type=int, required=True)
    parser.add_argument('--base', required=True, help='PR base commit (merge base is derived)')
    parser.add_argument('--head', required=True)
    parser.add_argument('--title', default='')
    parser.add_argument('--body-file')
    parser.add_argument('--root', required=True, help='trusted consumer checkout with the review config')
    parser.add_argument('--config', default='.github/review.json')
    parser.add_argument('--out', required=True)
    parser.add_argument('--lanes', default='', help='comma-separated lane ids; default all')
    parser.add_argument('--fake', action='store_true')
    parser.add_argument('--resume', action='store_true', help='reuse lane-*.json opinions already in --out')
    parser.add_argument('--keychain-service', help='read provider keys from this Keychain service '
                        '(account = key variable in lower case with dashes)')
    args = parser.parse_args()
    repo = str(Path(args.repo).resolve())
    head = git(repo, 'rev-parse', args.head).strip()
    base = git(repo, 'rev-parse', args.base).strip()
    merge_base = git(repo, 'merge-base', base, head).strip()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    config = configuration.load(args.root, args.config)
    items = pr_files(repo, merge_base, head)
    body = Path(args.body_file).read_text() if args.body_file else ''
    pr = {'title': args.title, 'body': body, 'changed_files': len(items), 'state': 'open',
          'base': {'sha': base}, 'head': {'sha': head}}
    prior = state.initial(args.repository, args.pr)
    trigger = {'command': 'full', 'action': None, 'labels': []}
    brief = context.build(args.repository, args.pr, pr, items, merge_base, config, prior, trigger, force_full=True)
    record = snapshot.create('file://' + repo, head, merge_base, out)
    bundle = {'packet': brief, 'config': config, 'state': prior, 'default_branch': 'main',
              'verify_finding': None, 'snapshot': record}
    save(out / 'bundle.json', bundle)
    slots = config['backends']['slots']
    wanted = set(filter(None, args.lanes.split(','))) or {slot['id'] for slot in slots}
    keys = {} if args.fake else load_keys(config, wanted, args.keychain_service)
    runners = {name: fake_runner for name in service.ACCESS} if args.fake else None
    # Snapshot.open needs a fresh directory per workspace.
    work = out / f'work-{time.strftime("%Y%m%d-%H%M%S")}'

    def open_workspace(name):
        return snapshot.Snapshot.open(out, record, work / name)

    timings = {}

    def generate(slot):
        start = time.monotonic()
        if slot['id'] not in wanted:
            return {'slot': slot['id'], 'opinion_family': slot['opinion_family'], 'status': 'skipped',
                    'scope': 'replay_lane_not_selected', 'findings': []}
        previous = out / f"lane-{slot['id']}.json"
        if args.resume and previous.exists():
            return json.loads(previous.read_text())
        review = service.generate(bundle, slot['id'], open_workspace('generate-' + slot['id']), runners)
        timings['generate-' + slot['id']] = round(time.monotonic() - start, 1)
        save(out / f"lane-{slot['id']}.json", review)
        return review

    # The Codex lane takes its key out of the environment on each run, as one Actions job would.
    os.environ.update(keys)
    with ThreadPoolExecutor(max_workers=len(slots)) as pool:
        reviews = list(pool.map(generate, slots))

    def verify(slot):
        if slot['id'] not in wanted:
            # A lane that was not selected is never invoked; its candidates stay unverified.
            return {'slot': slot['id'], 'status': 'not_needed'}
        start = time.monotonic()
        result = service.verify(bundle, slot['id'], reviews, open_workspace('verify-' + slot['id']), runners)
        timings['verify-' + slot['id']] = round(time.monotonic() - start, 1)
        save(out / f"verify-{slot['id']}.json", result)
        return result

    os.environ.update(keys)
    with ThreadPoolExecutor(max_workers=len(slots)) as pool:
        verifications = [item for item in pool.map(verify, slots) if item['status'] != 'not_needed']
    result = service.combine(bundle, reviews, verifications, open_workspace('combine'))
    save(out / 'result.json', result)
    (out / 'result.md').write_text(delivery.report(result))
    print(json.dumps({'status': result['status'], 'timings': timings,
                      'lanes': {review['slot']: review['status'] for review in reviews},
                      'findings': [{key: item.get(key) for key in ('finding_id', 'status', 'severity', 'path', 'title')}
                                   for item in result['findings']]}, indent=1))


if __name__ == '__main__':
    main()
