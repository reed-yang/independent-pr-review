"""Provider-free fixtures: a temporary git repository, its snapshot, scripted runners and a fake GitHub."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))

import replay  # noqa: E402
from independent_review import config, context, snapshot, state  # noqa: E402

REPO = 'owner/project'
NUMBER = 7
KEY = 'test-state-key-with-at-least-thirty-two-characters'
BASE_FILES = {'src/a.py': 'def first(value):\n    if not value:\n        return None\n    return value[0]\n',
              'src/caller.py': 'from a import first\n\n\ndef use():\n    return first([])\n',
              'README.md': '# Fixture\n'}
HEAD_FILES = {'src/a.py': 'def first(value):\n    head = value[0]\n    return head\n',
              'src/b.py': 'def second(value):\n    return value[1]\n'}


def clean_env(**extra):
    """The test process environment without provider, publishing or user git configuration."""
    prefixes = ('GROK_', 'GPT_', 'GEMINI_', 'AGY_', 'CODEX_', 'GITHUB_', 'REVIEW_', 'GIT_')
    env = {name: value for name, value in os.environ.items() if not name.startswith(prefixes) and name != 'GH_TOKEN'}
    env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, **extra)
    return env


def git(path, *args):
    env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(path), 'GIT_CONFIG_NOSYSTEM': '1',
           'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
           'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}
    return subprocess.run(['git', '-C', str(path), *args], env=env, check=True, capture_output=True, text=True).stdout.strip()


class Repository:
    """A local origin with a base commit and a PR head commit."""

    def __init__(self, root, base_files=BASE_FILES, head_files=HEAD_FILES):
        self.path = Path(root) / 'origin'
        self.path.mkdir(parents=True)
        git(self.path, 'init', '-q')
        self.base = self.commit(base_files)
        self.head = self.commit(head_files)
        self.remote = 'file://' + str(self.path)

    def commit(self, files):
        for name, text in files.items():
            target = self.path / name
            if text is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
        git(self.path, 'add', '-A')
        git(self.path, 'commit', '-q', '-m', 'fixture')
        return git(self.path, 'rev-parse', 'HEAD')

    def items(self):
        from unittest.mock import patch
        with patch.dict(os.environ, clean_env(), clear=True):
            return replay.pr_files(str(self.path), self.base, self.head)

    def pr(self, body='', title='Handle values', labels=()):
        return {'state': 'open', 'draft': False, 'title': title, 'body': body, 'changed_files': len(self.items()),
                'labels': [{'name': name} for name in labels],
                'head': {'sha': self.head, 'ref': 'feature', 'repo': {'full_name': REPO}},
                'base': {'sha': self.base, 'ref': 'main', 'repo': {'full_name': REPO}}}


def settings(**changes):
    """Load the example configuration with an environment free of provider variables."""
    from unittest.mock import patch
    with patch.dict(os.environ, clean_env(), clear=True):
        value = config.load(ROOT, 'examples/review.json')
    for key, item in changes.items():
        value[key] = item
    return value


def make_bundle(repo, out, prior=None, cfg=None, trigger=None, body='', reader=None, force_full=False):
    cfg = cfg or settings()
    prior = prior or state.initial(REPO, NUMBER)
    trigger = trigger or {'command': 'review', 'action': 'opened', 'labels': []}
    brief = context.build(REPO, NUMBER, repo.pr(body), repo.items(), repo.base, cfg, prior, trigger,
                          force_full=force_full, reader=reader)
    record = snapshot.create(repo.remote, repo.head, repo.base, out)
    return {'packet': brief, 'config': cfg, 'state': prior, 'default_branch': 'main', 'verify_finding': None,
            'snapshot': record}


def finding(**changes):
    value = {'path': 'src/a.py', 'line': 2, 'severity': 'P2', 'title': 'Empty list raises IndexError',
             'trigger_steps': ['Call use() in src/caller.py.', 'first([]) evaluates value[0].'],
             'mechanism': 'The head removed the empty-input guard before indexing.',
             'consequence': 'Callers that pass an empty list now crash with IndexError.',
             'evidence': 'head = value[0]'}
    value.update(changes)
    return value


def decision(candidate, status='confirmed', **changes):
    value = {'finding_id': candidate['finding_id'], 'status': status, 'reason': 'use() passes an empty list to first().',
             'failing_step': '', 'evidence_path': 'src/caller.py', 'evidence': 'return first([])'}
    if status == 'uncertain':
        value.update(evidence_path='', evidence='')
    value.update(changes)
    return value


def nonce_of(prompt):
    return prompt.rsplit('\nEND_OF_INPUT_NONCE=', 1)[1]


def candidates_of(prompt):
    return json.loads(prompt.rsplit('Candidates JSON: ', 1)[1].rsplit('\nEND_OF_INPUT_NONCE=', 1)[0])


class Scripted:
    """A harness stand-in: per-family findings, a decision function and optional failures."""

    def __init__(self, findings=None, decide=None, fail=None, nonce=None):
        self.findings = findings or {}
        self.decide = decide or (lambda candidate, family: decision(candidate))
        self.fail = fail or {}
        self.nonce = nonce
        self.calls = []
        self.tasks = []

    def __call__(self, backend, task, workspace):
        family = backend['opinion_family']
        self.calls.append((family, task['kind']))
        self.tasks.append(task)
        if (family, task['kind']) in self.fail:
            raise self.fail[(family, task['kind'])]
        echo = nonce_of(task['prompt']) if self.nonce is None else self.nonce
        if task['kind'] == 'review':
            answer = {'summary': f'{family} read the change and its callers.', 'files_examined': ['src/a.py', 'src/caller.py'],
                      'limitations': [f'{family} could not run the tests.'],
                      'observations': [{'path': 'src/a.py', 'kind': 'contract_change', 'text': 'first() no longer accepts [].'}],
                      'findings': self.findings.get(family, [])}
        else:
            answer = {'decisions': [self.decide(item, family) for item in candidates_of(task['prompt'])]}
        if echo != 'absent':
            answer['input_end_nonce'] = echo
        usage = {'input_tokens': 100, 'cached_input_tokens': 10, 'output_tokens': 20, 'reasoning_tokens': 5,
                 'total_tokens': 120, 'requests': 2, 'transport': {}}
        trace = {'tool_calls': 3, 'files_read': ['src/a.py', 'src/caller.py'], 'commands': ['rg -n first src'],
                 'truncated_outputs': 0, 'stopped_reason': 'final_answer'}
        return json.dumps(answer), backend['harness'] + '-model', usage, trace

    def runners(self):
        return {'responses_tools': self, 'codex_cli': self}


class FakeGitHub:
    """In-process REST and GraphQL responses for one PR; records every write."""

    def __init__(self, repo, items=None, pr=None):
        self.pr = pr or repo.pr()
        self.items = items if items is not None else repo.items()
        self.repo = repo
        self.comments = []
        self.review_comments = []
        self.writes = []
        self.threads = []
        self.graphql_errors = None
        self.resolved = []

    def __call__(self, url, token, data=None, method=None, timeout=90):
        parts = urlsplit(url)
        if parts.path == '/graphql':
            return self.graphql(data)
        path = parts.path.removeprefix(f'/repos/{REPO}').lstrip('/') + ('?' + parts.query if parts.query else '')
        if method:
            self.writes.append((method, path, data))
        if path == '':
            return {'default_branch': 'main', 'full_name': REPO}
        if path == f'pulls/{NUMBER}':
            return self.pr
        if path.startswith(f'pulls/{NUMBER}/files'):
            return self.items if path.endswith('page=1') else []
        if path.startswith('compare/'):
            return {'merge_base_commit': {'sha': self.repo.base}}
        if path.startswith(f'issues/{NUMBER}/comments?'):
            return self.comments
        if path == f'issues/{NUMBER}/comments' and method == 'POST':
            comment = {'id': 500 + len(self.comments), 'body': data['body'], 'user': {'login': state.BOT}}
            self.comments.append(comment)
            return comment
        if (match := re.fullmatch(r'issues/comments/(\d+)', path)) and method == 'PATCH':
            comment = next(item for item in self.comments if item['id'] == int(match[1]))
            comment['body'] = data['body']
            return comment
        if path.startswith(f'pulls/{NUMBER}/comments?'):
            return self.review_comments
        if path == f'pulls/{NUMBER}/reviews' and method == 'POST':
            for comment in data['comments']:
                self.review_comments.append({'id': 9000 + len(self.review_comments), 'body': comment['body'],
                                             'user': {'login': state.BOT}, 'review_id': 15})
            return {'id': 15}
        if path.startswith(f'pulls/{NUMBER}/reviews/15/comments'):
            return [item for item in self.review_comments if item.get('review_id') == 15]
        raise AssertionError('Unexpected GitHub request: ' + path)

    def graphql(self, data):
        if self.graphql_errors:
            return {'errors': self.graphql_errors}
        if data['query'].startswith('mutation'):
            self.resolved.append(data['variables']['id'])
            return {'data': {'resolveReviewThread': {'thread': {'id': data['variables']['id'], 'isResolved': True}}}}
        return {'data': {'repository': {'pullRequest': {'reviewThreads': {
            'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': self.threads}}}}}

    def summary(self):
        owned = [item for item in self.comments if state.MARKER in item['body']]
        assert len(owned) == 1, len(owned)
        return owned[0]['body']


def result_block(body):
    match = re.search(r'<!-- independent-pr-review-result:v1 (.*?) -->', body, re.S)
    return json.loads(match[1]) if match else None

