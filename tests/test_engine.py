import base64
import copy
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from independent_review import anchors, cli, config, context, core, delivery, service, state


REPO = 'owner/project'
KEY = 'test-state-key-with-at-least-thirty-two-characters'
BASE, HEAD = 'b' * 40, 'a' * 40


def pr():
    return {'state': 'open', 'draft': False, 'title': 'Handle values', 'body': '', 'changed_files': 1,
            'head': {'sha': HEAD, 'repo': {'full_name': REPO}}, 'base': {'sha': BASE, 'ref': 'main'}}


def settings():
    return config.load(Path(__file__).resolve().parents[1], 'examples/review.json')


def packet():
    return {'repository': REPO, 'pr_number': 7, 'head_sha': HEAD, 'base_sha': BASE, 'packet_id': 'packet',
            'coverage': 'bounded_diff_and_related_source', 'omitted': [], 'rules': [], 'context': [],
            'lanes': {'Grok': {'paths': None, 'reason': 'full'}, 'Gemini': {'paths': None, 'reason': 'full'}},
            'files': [{'path': 'src/a.py', 'patch': '@@ -1,2 +1,2 @@\n def first(value):\n-    return None\n+    return value[0]',
                       'head_text': 'def first(value):\n    return value[0]\n', 'base_text': 'def first(value):\n    return None\n'}]}


def candidate():
    return anchors.bind({'path': 'src/a.py', 'line': 2, 'severity': 'P2', 'title': 'Empty list fails',
                         'body': 'An empty list raises IndexError for callers without a length guard.',
                         'evidence': 'return value[0]'}, packet()['files'][0])


def bundle():
    return {'packet': packet(), 'config': settings(), 'state': state.initial(REPO, 7), 'default_branch': 'main'}


def answer(findings=None):
    return json.dumps({'summary': 'Reviewed the supplied scope.', 'limitations': [], 'findings': findings or []})


class AnchorTests(unittest.TestCase):
    def test_maps_added_and_deleted_lines_in_multiple_hunks(self):
        lines = anchors.changed_lines('@@ -2,2 +2,2 @@\n-old value\n+new value\n context\n@@ -8 +8,2 @@\n-old second\n+new second\n+extra value')
        self.assertEqual([(l['side'], l['line']) for l in lines], [('LEFT', 2), ('RIGHT', 2), ('LEFT', 8), ('RIGHT', 8), ('RIGHT', 9)])

    def test_ambiguous_repeated_evidence_stays_summary_only(self):
        entry = {'patch': '@@ -0,0 +1,2 @@\n+return value[0]\n+return value[0]'}
        self.assertIsNone(anchors.locate(entry, 'return value[0]'))
        self.assertEqual(anchors.locate(entry, 'return value[0]', 2), {'side': 'RIGHT', 'line': 2})

    def test_context_line_is_never_used_as_an_inline_anchor(self):
        self.assertIsNone(anchors.locate(packet()['files'][0], 'def first(value):'))

    def test_identity_ignores_title_and_carries_rename(self):
        original = candidate()
        item = {**original, 'title': 'Different wording', 'path': 'src/renamed.py'}
        entry = {**packet()['files'][0], 'path': item['path'], 'previous_filename': original['path']}
        self.assertEqual(original['finding_id'], anchors.bind(item, entry)['finding_id'])


class StateTests(unittest.TestCase):
    def test_signature_binds_repository_and_pr(self):
        encoded = state.encode(state.initial(REPO, 7), KEY)
        self.assertEqual(state.decode(encoded, KEY, REPO, 7)['runs'], 0)
        for repo, number, key in [('other/project', 7, KEY), (REPO, 8, KEY), (REPO, 7, KEY + 'changed')]:
            with self.assertRaises(core.ReviewError):
                state.decode(encoded, key, repo, number)
        with self.assertRaises(core.ReviewError):
            state.decode(encoded.replace(':v1:', ':v1:A'), KEY, REPO, 7)

    def test_untrusted_comment_cannot_seed_or_reset_state(self):
        body = state.MARKER + state.encode(state.initial(REPO, 7), KEY)
        api = lambda *args: [{'id': 5, 'body': body, 'user': {'login': 'contributor'}}]
        value, comment_id = state.read(REPO, 7, KEY, api)
        self.assertIsNone(comment_id)
        api = lambda *args: [{'id': 5, 'body': state.MARKER, 'user': {'login': state.BOT}}]
        with self.assertRaisesRegex(core.ReviewError, 'signed_state'):
            state.read(REPO, 7, KEY, api)

    def test_interrupted_reservation_stays_charged_and_hard_run_cap_holds(self):
        data = bundle()
        data['config']['limits']['max_runs_per_pr'] = 1
        value = state.reserve(data['state'], data, '1:1', 'https://github.com/owner/project/actions/runs/1')
        self.assertEqual(value['runs'], 1)
        self.assertGreater(value['tokens'], 0)
        with self.assertRaisesRegex(core.ReviewError, 'run_budget_exhausted'):
            state.reserve(value, data, '1:2', 'url')

    def test_result_cannot_move_to_another_run_or_pr(self):
        data = bundle()
        reserved = state.reserve(data['state'], data, '1:1', 'url')
        result = service.run(data, {name: lambda *args: (answer(), 'model', {'total_tokens': 10}) for name in core.HARNESSES})
        for run_id, change in [('2:1', {}), ('1:1', {'pr_number': 8}), ('1:1', {'head_sha': 'c' * 40})]:
            with self.assertRaisesRegex(core.ReviewError, 'stale_or_unbound'):
                state.accept(reserved, {**result, **change}, run_id)
        accepted = state.accept(reserved, result, '1:1')
        self.assertEqual(accepted['tokens'], 20)
        self.assertEqual(len(accepted['lanes']), 2)
        with self.assertRaises(core.ReviewError):
            state.accept(accepted, result, '1:1')

    def test_oversized_state_fails_before_comment_publication(self):
        value = state.initial(REPO, 7)
        value['extra'] = 'x' * state.MAX_RAW
        with self.assertRaisesRegex(core.ReviewError, 'capacity'):
            state.encode(value, KEY)


class ContextTests(unittest.TestCase):
    def api(self, repo, path):
        if path == 'pulls/7':
            return pr()
        if path.startswith('pulls/7/files'):
            return [{'filename': 'src/a.py', 'status': 'modified', 'patch': packet()['files'][0]['patch']}]
        if path.startswith('compare/'):
            return {'merge_base_commit': {'sha': BASE}, 'status': 'ahead', 'commits': [{'sha': HEAD}], 'files': [{'filename': 'src/a.py'}]}
        if path.startswith('git/trees/'):
            return {'tree': [{'type': 'blob', 'path': p} for p in ('src/a.py', 'src/test_a.py', 'src/caller.py')]}
        if path.startswith('contents/'):
            text = packet()['files'][0]['head_text'] if 'src/a.py' in path else 'from a import first\nfirst([])\n'
            return {'type': 'file', 'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}
        raise AssertionError(path)

    def test_immutable_context_includes_related_test_and_sibling(self):
        data = context.collect(REPO, 7, pr(), settings(), state.initial(REPO, 7), api=self.api)
        self.assertEqual({entry['path'] for entry in data['context']}, {'src/test_a.py', 'src/caller.py'})
        self.assertLessEqual(data['context_reads'], settings()['limits']['max_api_reads'])
        self.assertFalse(data['full_repository_review'])

    def test_changed_snapshot_is_rejected(self):
        def api(repo, path):
            if path == 'pulls/7':
                value = pr()
                value['head']['sha'] = 'c' * 40
                return value
            return self.api(repo, path)
        with self.assertRaisesRegex(core.ReviewError, 'changed_during_collection'):
            context.collect(REPO, 7, pr(), settings(), state.initial(REPO, 7), api=api)

    def test_fork_draft_closed_and_non_default_targets_are_ineligible(self):
        cases = [pr() for _ in range(4)]
        cases[0]['head']['repo']['full_name'] = 'fork/project'
        cases[1]['draft'] = True
        cases[2]['state'] = 'closed'
        cases[3]['base']['ref'] = 'release'
        self.assertTrue(all(not context.eligible(value, REPO, 'main') for value in cases))

    def test_incremental_reuses_exact_success_and_falls_back_on_rebase(self):
        baseline = {'head_sha': HEAD, 'base_sha': BASE, 'config_id': 'config'}
        reader = context.Reader(REPO, 10, self.api)
        self.assertEqual(context.incremental(reader, baseline, pr(), 'config', False)[0], [])
        self.assertIsNone(context.incremental(reader, baseline, pr(), 'changed-config', False)[0])
        self.assertIsNone(context.incremental(reader, baseline, pr(), 'config', True)[0])
        baseline['head_sha'] = 'd' * 40
        reader = context.Reader(REPO, 10, lambda *args: {'status': 'diverged'})
        self.assertIsNone(context.incremental(reader, baseline, pr(), 'config', False)[0])

    def test_repository_metadata_route_has_no_trailing_slash(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'test-token'}), patch.object(core, 'request_json', return_value={}) as request:
            core.github(REPO, '')
        self.assertEqual(request.call_args.args[0], 'https://api.github.com/repos/' + REPO)

    def test_action_launcher_cannot_import_consumer_shadow_package(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            shadow = Path(directory) / 'independent_review'
            shadow.mkdir()
            (shadow / '__init__.py').write_text('raise RuntimeError("consumer code executed")')
            result = subprocess.run([sys.executable, '-I', str(root / 'scripts/action_phase.py'), 'cli', 'validate-config',
                                     '--root', str(root), '--config', 'examples/review.json', '--out', directory],
                                    cwd=directory, capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_policy_cannot_escape_trusted_checkout(self):
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(core.ReviewError):
                config.trusted_file(root, '../private.json')
            Path(root, 'rules.md').symlink_to('/etc/hosts')
            with self.assertRaises(core.ReviewError):
                config.trusted_file(root, 'rules.md')
            with self.assertRaises(core.ReviewError):
                config.trusted_file(root, None)


class VerificationTests(unittest.TestCase):
    def runners(self, status='confirmed', failure=False):
        calls = []
        def grok(backend, prompt):
            calls.append('grok')
            return answer([candidate()]), 'grok-test', {'total_tokens': 10}
        def gemini(backend, prompt):
            if prompt.startswith('Independently challenge'):
                calls.append('verification')
                if failure:
                    raise core.ReviewError('http_503')
                payload = json.loads(prompt[prompt.index('{"packet":'):])
                decisions = [{'finding_id': item['finding_id'], 'status': status, 'reason': 'The supplied caller passes an empty list without a guard.',
                              'evidence_path': 'src/a.py', 'evidence': 'return value[0]'} for item in payload['candidates']]
                return json.dumps({'decisions': decisions}), 'gemini-test', {'total_tokens': 10}
            calls.append('gemini')
            return answer(), 'gemini-test', {'total_tokens': 10}
        return {'compatible_packet': grok, 'antigravity_packet': gemini}, calls

    def test_single_model_discovery_is_cross_verified_without_consensus_requirement(self):
        runners, calls = self.runners()
        result = service.run(bundle(), runners)
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['findings'][0]['status'], 'open')
        self.assertEqual(result['findings'][0]['verification']['verifier'], 'Gemini')
        self.assertEqual(sorted(calls), ['gemini', 'grok', 'verification'])

    def test_counterevidence_withdraws_candidate_without_inline_publication(self):
        runners, _ = self.runners('dismissed')
        result = service.run(bundle(), runners)
        self.assertEqual(result['findings'][0]['status'], 'dismissed')

    def test_verifier_operational_failure_does_not_advance_baselines(self):
        data = bundle()
        runners, _ = self.runners(failure=True)
        result = service.run(data, runners)
        reserved = state.reserve(data['state'], data, '1:1', 'url')
        accepted = state.accept(reserved, result, '1:1')
        self.assertEqual(accepted['lanes'], {})
        self.assertEqual(accepted['status'], 'partial')
        self.assertEqual(result['findings'][0]['status'], 'uncertain')

    def test_absence_from_incremental_output_does_not_fix_old_issue(self):
        data = bundle()
        item = {**candidate(), 'status': 'open', 'sources': ['Grok']}
        data['state']['findings'][item['finding_id']] = item
        def run(backend, prompt):
            if prompt.startswith('Independently challenge'):
                return json.dumps({'decisions': [{'finding_id': item['finding_id'], 'status': 'uncertain', 'reason': 'Missing callers.'}]}), 'model', {}
            return answer(), 'model', {}
        result = service.run(data, {name: run for name in core.HARNESSES})
        self.assertEqual(result['findings'][0]['status'], 'uncertain')

    def test_identical_lanes_are_reused_without_any_model_call(self):
        data = bundle()
        for name in ('Grok', 'Gemini'):
            data['packet']['lanes'][name]['paths'] = []
            data['state']['lanes'][name] = {'review': {'slot': name, 'findings': [], 'summary': 'Cached'}}
        def unexpected(*args):
            self.fail('Unexpected provider invocation')
        result = service.run(data, {name: unexpected for name in core.HARNESSES})
        self.assertEqual([item['status'] for item in result['reviews']], ['reused', 'reused'])
        self.assertEqual(result['accounted_tokens'], 0)

    def test_fixed_requires_current_head_evidence_not_missing_old_text(self):
        item = {**candidate(), 'previous': True}
        decision = {'finding_id': item['finding_id'], 'status': 'fixed', 'reason': 'Changed.', 'evidence_path': 'src/a.py', 'evidence': 'return None'}
        with self.assertRaisesRegex(core.ReviewError, 'evidence_not_in_packet'):
            service.parse_decisions(json.dumps({'decisions': [decision]}), packet(), [item])


class CommandAndDeliveryTests(unittest.TestCase):
    def event(self, body, user_type='User'):
        return {'action': 'created', 'issue': {'number': 7, 'pull_request': {}},
                'comment': {'body': body, 'user': {'login': 'maintainer', 'type': user_type}}}

    def test_commands_check_current_write_permission_and_reject_shell_suffixes(self):
        event = self.event('/review full')
        event['issue']['pull_request'] = {'url': 'PR'}
        self.assertEqual(cli.target('issue_comment', event, REPO, 'review', lambda *args: {'permission': 'write'}), (7, 'full'))
        self.assertIsNone(cli.target('issue_comment', event, REPO, 'review', lambda *args: {'permission': 'read'})[0])
        for text in ('/review full; curl attacker', '/review\nfull', '/review @someone', '/review full now'):
            event['comment']['body'] = text
            self.assertIsNone(cli.target('issue_comment', event, REPO, 'review')[0])
        event['comment'].update(body='/review', user={'type': 'Bot', 'login': state.BOT})
        self.assertIsNone(cli.target('issue_comment', event, REPO, 'review')[0])

    def test_stale_sha_prevents_any_comment_write(self):
        value = {**state.initial(REPO, 7), 'head_sha': 'c' * 40, 'base_sha': BASE}
        with self.assertRaisesRegex(core.ReviewError, 'changed_before_publication'):
            delivery.current_pr(value, 'main', lambda *args: pr())

    def test_only_verified_anchored_findings_post_comment_event(self):
        value = {**state.initial(REPO, 7), 'head_sha': HEAD, 'base_sha': BASE}
        item = {**candidate(), 'status': 'open', 'verification': {'reason': 'Confirmed by the supplied caller.'}}
        value['findings'] = {item['finding_id']: item, 'uncertain': {**item, 'finding_id': 'uncertain', 'status': 'uncertain'}}
        writes = []
        def api(repo, path, data=None, method=None):
            if path == 'pulls/7':
                return pr()
            if method == 'POST':
                writes.append(data)
                return {'id': 15}
            if '/reviews/15/comments' in path:
                return [{'id': 22, 'user': {'login': state.BOT}, 'body': writes[0]['comments'][0]['body']}]
            return []
        delivery.publish_inline(value, settings(), 'main', api)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['event'], 'COMMENT')
        self.assertEqual(writes[0]['commit_id'], HEAD)
        self.assertEqual(len(writes[0]['comments']), 1)
        self.assertEqual(item['comment_id'], 22)
        delivery.publish_inline(value, settings(), 'main', api)
        self.assertEqual(len(writes), 1)

    def test_own_orphaned_post_is_recovered_without_reposting(self):
        item = {**candidate(), 'status': 'open'}
        value = {**state.initial(REPO, 7), 'head_sha': HEAD, 'base_sha': BASE, 'findings': {item['finding_id']: item}}
        def api(repo, path, data=None, method=None):
            self.assertIsNone(method)
            return [{'id': 31, 'user': {'login': state.BOT}, 'body': f"<!-- independent-pr-review-finding:{item['finding_id']} -->"}]
        delivery.publish_inline(value, settings(), 'main', api)
        self.assertEqual(item['comment_id'], 31)

    def test_only_owned_verified_fixed_threads_are_resolved(self):
        item = {**candidate(), 'status': 'fixed', 'comment_id': 31}
        value = {**state.initial(REPO, 7), 'head_sha': HEAD, 'base_sha': BASE, 'findings': {item['finding_id']: item}}
        mutations = []
        def gql(query, variables):
            if query.startswith('mutation'):
                mutations.append(variables['id'])
                return {}
            return {'repository': {'pullRequest': {'reviewThreads': {'pageInfo': {'hasNextPage': False}, 'nodes': [
                {'id': 'own', 'isResolved': False, 'comments': {'nodes': [{'databaseId': 31, 'author': {'login': state.BOT}}]}},
                {'id': 'human', 'isResolved': False, 'comments': {'nodes': [{'databaseId': 32, 'author': {'login': 'maintainer'}}]}}
            ]}}}}
        delivery.resolve_fixed(value, 'main', lambda *args: pr(), gql)
        self.assertEqual(mutations, ['own'])
        self.assertTrue(item['thread_resolved'])


if __name__ == '__main__':
    unittest.main()
