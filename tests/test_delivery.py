import html
import os
import unittest
from unittest.mock import patch

from independent_review import core, delivery, state
from support import KEY, REPO, result_block, settings
from test_engine import BASE, HEAD, candidate, pr


LIMITS = settings()['limits']
HOSTILE = 'Close --> <!-- reopen --!> -- > [link](https://x) @team `code`'


def item(fid, status, **changes):
    value = {'finding_id': fid * 24, 'status': status, 'severity': 'P2', 'path': 'src/a.py', 'line': 2,
             'title': f'{status} finding', 'body': 'Trigger: (1) call it.', 'evidence': 'return value[0]',
             'verification': {'status': {'open': 'confirmed', 'dismissed': 'dismissed', 'fixed': 'fixed'}.get(status, 'uncertain'),
                              'reason': f'{status} reason', 'verifier': 'GPT'}}
    value.update(changes)
    return value


def reviewed(**changes):
    value = state.initial(REPO, 7)
    value.update(status='completed', head_sha=HEAD, base_sha=BASE, merge_base_sha='c' * 40, config_id='d' * 64,
                 engine_version='0.4.0', last_run='https://github.com/owner/project/actions/runs/1', runs=1, tokens=500,
                 description_truncated=True, description_chars=70000, omitted_count=1)
    value['findings'] = {entry['finding_id']: entry for entry in (
        item('a', 'open'), item('b', 'uncertain'), item('c', 'fixed', comment_id=31, thread_resolve_error='FORBIDDEN'),
        item('d', 'dismissed', verification={'status': 'dismissed', 'reason': 'A guard already rejects empty input.',
                                             'verifier': 'Grok', 'failing_step': 'Step 2'}))}
    value['last_reviews'] = [
        {'slot': 'Grok', 'status': 'completed', 'model': 'grok-4.7', 'effort': 'xhigh', 'scope': 'incremental',
         'access': {'files_examined': 12, 'tool_calls': 30, 'files_read': 18}, 'errors': [], 'failure_notes': [],
         'rejected_count': 1, 'limitations': ['Could not run the integration tests.'],
         'observations': [{'kind': 'contract_change', 'path': 'src/a.py', 'text': 'first() now rejects empty input.'}]},
        {'slot': 'GPT', 'status': 'skipped', 'scope': 'below_generation_threshold', 'errors': [], 'failure_notes': [],
         'rejected_count': 0, 'access': {'files_examined': 0, 'tool_calls': 0, 'files_read': 0}}]
    value.update(changes)
    return value


class SummaryTests(unittest.TestCase):
    def test_summary_reports_counts_lanes_notes_and_warnings(self):
        text = delivery.summary(reviewed(), LIMITS)
        plain = html.unescape(text)
        self.assertTrue(text.startswith(state.MARKER))
        self.assertIn('**Completed** · `aaaaaaaaaaaa` · [Workflow run](https://github.com/owner/project/actions/runs/1)', text)
        self.assertIn('1 verified open · 1 uncertain · 1 verified fixed · 1 dismissed', text)
        self.assertIn('- **P2** `src/a.py` — open finding (`aaaaaaaa`)', text)
        self.assertIn('| Grok | grok-4.7 | xhigh | Completed: 1 rejected candidate(s) | Changes since previous successful review | '
                      '12 examined · 30 tool calls · 18 files read |', text)
        self.assertIn("| GPT | — | — | Skipped | Skipped: below the generation threshold for this lane; it still verifies "
                      "the other lane's candidates | — |", plain)
        self.assertIn('the PR description has 70,000 characters; reviewers received only its first 60,000 characters', plain)
        self.assertIn('**Publication warning:** 1 verified-fixed review thread(s) could not be resolved (FORBIDDEN); a later run retries.', text)
        self.assertIn('1 candidate(s) dismissed by cross-family verification', text)
        self.assertIn('They are not reported as bugs', text)
        self.assertIn('- `dddddddd` dismissed finding — dismissed by Grok: A guard already rejects empty input.', text)
        self.assertIn('- Observation (contract_change) `src/a.py`: first() now rejects empty input.', text)
        self.assertIn('- Limitation: Could not run the integration tests.', text)
        self.assertIn('read-only sandboxed CLI', text)
        self.assertIn("may execute only inside the GPT lane's read-only, no-network sandbox", plain)
        self.assertNotIn('bounded diff and related-source context', text)
        # Dismissed and fixed findings are never listed as open bugs.
        self.assertNotIn('dismissed finding (`', text)

    def test_resolved_or_unresolved_states_render_without_warnings(self):
        value = reviewed()
        value['findings']['c' * 24].update(thread_resolved=True)
        self.assertNotIn('Publication warning', delivery.summary(value, LIMITS))
        value = reviewed(description_truncated=False, status='in_progress', reservation={'head_sha': 'f' * 40})
        text = delivery.summary(value, LIMITS)
        self.assertNotIn('PR description has', text)
        self.assertIn('`ffffffffffff`', text)
        self.assertIn('Do not interpret this status as a clean review', text)
        self.assertEqual(result_block(text)['status'], 'in_progress')
        empty = delivery.summary(state.initial(REPO, 7), LIMITS)
        self.assertEqual(result_block(empty)['counts'], {'open': 0, 'uncertain': 0, 'fixed': 0, 'dismissed': 0})

    def test_failed_lane_explains_a_reasoning_timeout_without_provider_text(self):
        value = reviewed(status='partial')
        attempt = {'error': 'provider_deadline_exceeded', 'diagnostics': {
            'stage': 'read', 'http_status': 200, 'reasoning_events': 30, 'content_events': 0}}
        value['last_reviews'][1] = {'slot': 'GPT', 'status': 'failed', 'errors': ['provider_deadline_exceeded'],
                                    'error': 'provider_deadline_exceeded', 'failure_notes': [core.failure_description(attempt)]}
        text = delivery.summary(value, LIMITS)
        self.assertIn('no final review text arrived before the configured time limit', text)
        self.assertIn('Reasoning updates were received', text)
        self.assertIn('Failed: provider_deadline_exceeded |', text)
        self.assertEqual(text.count('provider_deadline_exceeded'), 1)

    def test_reused_state_hides_an_earlier_failed_attempt(self):
        value = reviewed(status='partial')
        value['last_reviews'][0].update(status='failed', errors=['provider_deadline_exceeded'],
                                        failure_notes=['The failed force-review attempt timed out.'])
        rendered = delivery.summary(state.reuse(value, 'https://github.com/owner/project/actions/runs/2'), LIMITS)
        self.assertNotIn('timed out', rendered)
        self.assertNotIn('provider_deadline_exceeded', rendered)
        self.assertNotIn('rejected candidate', rendered)

    def test_machine_readable_block_is_bound_to_identity_and_cannot_close_the_comment(self):
        value = reviewed()
        value['findings']['a' * 24].update(title=HOSTILE + 'x' * 500, path='src/-->.py')
        value['last_reviews'][0]['model'] = 'model-->injected'
        text = delivery.summary(value, LIMITS)
        start = text.index('<!-- independent-pr-review-result:v1 ')
        block = text[start:text.index('\n', start)]
        # The only comment terminator is the block's own end; no nested opener survives.
        self.assertEqual(block.count('-->'), 1)
        self.assertTrue(block.endswith(' -->'))
        self.assertEqual(block.count('<!--'), 1)
        self.assertNotIn('--', block[len('<!-- independent-pr-review-result:v1 '):-len(' -->')])
        self.assertNotRegex(block[5:-4], r'[<>]')
        data = result_block(text)
        self.assertEqual({key: data[key] for key in ('repository', 'pr_number', 'base_sha', 'merge_base_sha', 'head_sha',
                                                       'config_id', 'engine_version', 'run_url', 'status')},
                         {'repository': REPO, 'pr_number': 7, 'base_sha': BASE, 'merge_base_sha': 'c' * 40, 'head_sha': HEAD,
                          'config_id': 'd' * 64, 'engine_version': '0.4.0',
                          'run_url': 'https://github.com/owner/project/actions/runs/1', 'status': 'completed'})
        self.assertEqual(data['counts'], {'open': 1, 'uncertain': 1, 'fixed': 1, 'dismissed': 1})
        first = data['findings'][0]
        self.assertEqual((first['finding_id'], first['status'], first['path'], first['line']), ('a' * 24, 'open', 'src/-->.py', 2))
        self.assertEqual(first['title'], (HOSTILE + 'x' * 500)[:199] + '…')
        self.assertEqual([entry['status'] for entry in data['findings']], ['open', 'uncertain', 'fixed', 'dismissed'])
        self.assertEqual(data['lanes'][0], {'slot': 'Grok', 'status': 'completed', 'scope': 'incremental', 'model': 'model-->injected',
                                            'effort': 'xhigh', 'access': {'files_examined': 12, 'tool_calls': 30, 'files_read': 18}})
        self.assertEqual(data['lanes'][1]['model'], None)
        # The visible summary escapes the same text as inert markup.
        self.assertNotIn(HOSTILE, text)
        self.assertNotIn('@team', text)

    def test_large_state_shrinks_lists_before_failing_and_fits_with_the_signed_state(self):
        value = reviewed()
        for index in range(60):
            for status in ('open', 'uncertain', 'dismissed'):
                entry = item(f'{index:02x}', status, title='t' * 4000, path='p/' * 200,
                             verification={'status': 'uncertain', 'reason': 'r' * 3000, 'verifier': 'GPT'})
                entry['finding_id'] = f'{status[0]}{index:023x}'
                value['findings'][entry['finding_id']] = entry
        for lane in value['last_reviews']:
            lane.update(limitations=['l' * 300] * 3, observations=[{'kind': 'risk', 'path': 'p' * 300, 'text': 'o' * 500}] * 5)
        full = delivery.render(value, LIMITS, delivery.DETAIL[0])
        self.assertGreater(len(full), 30000)
        text = delivery.summary(value, LIMITS, 30000)
        self.assertLessEqual(len(text), 30000)
        self.assertIn('more verified open findings are listed in the workflow report', text)
        self.assertEqual(result_block(text)['findings_total'], 184)
        with self.assertRaisesRegex(core.ReviewError, 'summary_capacity_exceeded'):
            delivery.summary(value, LIMITS, 2000)
        writes = []
        def api(repo, path, data=None, method=None):
            writes.append(data['body'])
            return {'id': 5}
        # Keep the signed payload small enough for this fixture; the summary takes the rest.
        compact = {key: entry for key, entry in list(value['findings'].items())[:40]}
        value['findings'] = {key: {**entry, 'title': entry['title'][:200], 'verification': {**entry['verification'], 'reason': 'r'}}
                             for key, entry in compact.items()}
        self.assertEqual(delivery.write_summary(value, None, KEY, LIMITS, api), 5)
        self.assertLessEqual(len(writes[0]), delivery.SUMMARY_LIMIT)
        self.assertEqual(state.decode(writes[0], KEY, REPO, 7)['findings'].keys(), value['findings'].keys())
        self.assertIsNotNone(result_block(writes[0]))


class ReportTests(unittest.TestCase):
    def result(self):
        lane = {'slot': 'Grok', 'status': 'partial', 'model': 'grok-4.7', 'effort': 'xhigh', 'scope': 'incremental',
                'elapsed_seconds': 12.5, 'summary': 'Read the parser and its callers.',
                'usage': {'input_tokens': 1000, 'cached_input_tokens': 200, 'output_tokens': 300, 'reasoning_tokens': 250,
                          'total_tokens': 1300, 'requests': 4, 'transport': {}},
                'trace': {'tool_calls': 9, 'files_read': ['src/a.py', 'src/b.py'], 'commands': [f'rg pattern{index}' for index in range(8)],
                          'stopped_reason': 'final_answer'},
                'files_examined': ['src/a.py'], 'attempts': [{'status': 'partial', 'error': 'input_end_nonce_missing', 'stage': 'validation'}],
                'error': 'input_end_nonce_missing',
                'findings': [{**candidate(), 'trigger_steps': ['Call first([]).', 'Index zero is read.'],
                              'mechanism': 'No guard.', 'consequence': 'IndexError.', 'evidence_source': 'patch'}],
                'rejected_findings': [{'index': 1, 'error': 'finding_evidence_not_in_snapshot', 'path': 'src/a.py'}],
                'observations': [{'kind': 'coverage_gap', 'path': 'tests/test_a.py', 'text': 'No empty-list test.'}],
                'limitations': ['Did not run tests.']}
        skipped = {'slot': 'GPT', 'status': 'skipped', 'scope': 'below_generation_threshold', 'findings': []}
        decisions = {'a' * 24: {'finding_id': 'a' * 24, 'status': 'dismissed', 'failing_step': 'Step 2', 'reason': 'A guard exists.'}}
        verifications = [{'slot': 'GPT', 'status': 'partial', 'model': 'gpt-6.1-sol', 'effort': 'xhigh', 'elapsed_seconds': 3,
                          'decisions': decisions, 'error': 'verification_evidence_not_in_snapshot',
                          'rejected_decisions': [{'finding_id': 'b' * 24, 'error': 'verification_evidence_not_in_snapshot'}]},
                         {'slot': 'Grok', 'status': 'not_needed', 'decisions': {}}]
        findings = [item('a', 'dismissed', verification={**decisions['a' * 24], 'verifier': 'GPT'}), item('b', 'open')]
        return {'status': 'partial', 'repository': REPO, 'pr_number': 7, 'base_sha': BASE, 'merge_base_sha': 'c' * 40,
                'head_sha': HEAD, 'config_id': 'd' * 64, 'engine_version': '0.4.0', 'description_truncated': True,
                'description_chars': 70000, 'coverage': 'repository_snapshot_with_read_tools', 'omitted': [],
                'reviews': [lane, skipped], 'verifications': verifications, 'findings': findings}

    def test_report_lists_lane_usage_access_findings_and_decisions(self):
        text = html.unescape(delivery.report(self.result()))
        for expected in (
                '# Independent review: partial', 'Engine 0.4.0', "GPT lane's read-only, no-network sandbox",
                'The PR description (70,000 characters) exceeded the brief limit',
                '- model grok-4.7 · effort xhigh · scope: Changes since previous successful review · elapsed 12.5 s',
                '- Usage: input 1,000 · cached input 200 · output 300 · reasoning 250 · total 1,300 · requests 4.',
                '- Access: 1 files examined · 9 tool calls · 2 files read · stopped: final_answer.',
                '`rg pattern4`', 'may have been truncated', '  - Trigger: (1) Call first(［］). (2) Index zero is read.',
                '  - Mechanism: No guard.', '  - Consequence: IndexError.', '  - Evidence (patch): return value［0］',
                '- Rejected candidate 2: finding_evidence_not_in_snapshot; path: src/a.py.',
                '- Observation (coverage_gap) `tests/test_a.py`: No empty-list test.', '- Limitation: Did not run tests.',
                "scope: Skipped: below the generation threshold for this lane; it still verifies the other lane's candidates",
                '### GPT verification: partial', 'error verification_evidence_not_in_snapshot',
                f"- `{'a' * 24}` **dismissed** by GPT; failing step: Step 2; reason: A guard exists.",
                f"- Rejected decision `{'b' * 24}` (verification_evidence_not_in_snapshot); retained as uncertain.",
                '### Grok verification: not_needed', 'No candidates were routed to this verifier.',
                f"- `{'a' * 24}` **dismissed** P2 `src/a.py`: dismissed finding. Decision by GPT: A guard exists. "
                'Dismissed by cross-family verification; not reported as a bug.',
                f"- `{'b' * 24}` **open** P2"):
            self.assertIn(expected, text)
        self.assertNotIn('rg pattern5', text)
        self.assertEqual(text.count('input_end_nonce_missing'), 1)

    def test_report_is_bounded(self):
        value = self.result()
        value['reviews'][0]['summary'] = 'x' * (delivery.REPORT_LIMIT + 10)
        text = delivery.report(value)
        self.assertLess(len(text), delivery.REPORT_LIMIT + 100)
        self.assertTrue(text.endswith('result.json contains every record.\n'))


class InlineTests(unittest.TestCase):
    def test_stale_sha_prevents_any_comment_write(self):
        value = {**state.initial(REPO, 7), 'head_sha': 'c' * 40, 'base_sha': BASE}
        with self.assertRaisesRegex(core.ReviewError, 'changed_before_publication'):
            delivery.current_pr(value, 'main', lambda *args: pr())

    def test_only_verified_anchored_findings_post_comment_event(self):
        value = {**state.initial(REPO, 7), 'head_sha': HEAD, 'base_sha': BASE}
        finding = {**candidate(), 'status': 'open', 'verification': {'reason': 'Confirmed by the supplied caller.'}}
        value['findings'] = {finding['finding_id']: finding,
                             'uncertain': {**finding, 'finding_id': 'uncertain', 'status': 'uncertain'},
                             'dismissed': {**finding, 'finding_id': 'dismissed', 'status': 'dismissed'}}
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
        self.assertEqual((writes[0]['event'], writes[0]['commit_id'], len(writes[0]['comments'])), ('COMMENT', HEAD, 1))
        self.assertEqual(finding['comment_id'], 22)
        delivery.publish_inline(value, settings(), 'main', api)
        self.assertEqual(len(writes), 1)

    def test_own_orphaned_post_is_recovered_without_reposting(self):
        finding = {**candidate(), 'status': 'open'}
        value = {**state.initial(REPO, 7), 'head_sha': HEAD, 'base_sha': BASE, 'findings': {finding['finding_id']: finding}}
        def api(repo, path, data=None, method=None):
            self.assertIsNone(method)
            return [{'id': 31, 'user': {'login': state.BOT}, 'body': f"<!-- independent-pr-review-finding:{finding['finding_id']} -->"}]
        delivery.publish_inline(value, settings(), 'main', api)
        self.assertEqual(finding['comment_id'], 31)


class ThreadResolutionTests(unittest.TestCase):
    def fixed_state(self):
        finding = {**candidate(), 'status': 'fixed', 'comment_id': 9000000031}
        return {**state.initial(REPO, 7), 'head_sha': HEAD, 'base_sha': BASE, 'findings': {finding['finding_id']: finding}}, finding

    def threads(self):
        return {'repository': {'pullRequest': {'reviewThreads': {'pageInfo': {'hasNextPage': False}, 'nodes': [
            {'id': 'own', 'isResolved': False, 'comments': {'nodes': [{'fullDatabaseId': '9000000031', 'author': {'login': 'github-actions', '__typename': 'Bot'}}]}},
            {'id': 'human', 'isResolved': False, 'comments': {'nodes': [{'fullDatabaseId': '32', 'author': None}]}}]}}}}

    def test_only_owned_verified_fixed_threads_are_resolved(self):
        value, finding = self.fixed_state()
        mutations = []
        def gql(query, variables):
            self.assertNotIn('databaseId', query)
            if query.startswith('mutation'):
                mutations.append(variables['id'])
                return {}
            return self.threads()
        delivery.resolve_fixed(value, 'main', lambda *args: pr(), gql)
        self.assertEqual(mutations, ['own'])
        self.assertTrue(finding['thread_resolved'])

    def test_failed_resolution_is_recorded_retried_and_does_not_stop_publication(self):
        value, finding = self.fixed_state()
        failure = core.ReviewError('github_graphql_failed', {'error_type': 'FORBIDDEN', 'path': 'resolveReviewThread'})
        def failing(query, variables):
            if query.startswith('mutation'):
                raise failure
            return self.threads()
        delivery.resolve_fixed(value, 'main', lambda *args: pr(), failing)
        self.assertEqual(finding['thread_resolve_error'], 'FORBIDDEN')
        self.assertNotIn('thread_resolved', finding)
        self.assertIn('could not be resolved (FORBIDDEN)', delivery.summary({**value, 'status': 'completed'}, LIMITS))
        # A thread query failure marks every pending target.
        delivery.resolve_fixed(value, 'main', lambda *args: pr(), lambda *args: (_ for _ in ()).throw(core.ReviewError('http_502')))
        self.assertEqual(finding['thread_resolve_error'], 'http_502')
        delivery.resolve_fixed(value, 'main', lambda *args: pr(), lambda *args: {'repository': None})
        self.assertEqual(finding['thread_resolve_error'], 'invalid_response')
        # A later run retries and clears the warning.
        delivery.resolve_fixed(value, 'main', lambda *args: pr(), lambda query, variables: {} if query.startswith('mutation') else self.threads())
        self.assertTrue(finding['thread_resolved'])
        self.assertNotIn('thread_resolve_error', finding)
        self.assertNotIn('Publication warning', delivery.summary({**value, 'status': 'completed'}, LIMITS))

    def test_inline_publication_continues_when_thread_resolution_fails(self):
        value, fixed = self.fixed_state()
        anchored = {**candidate(), 'finding_id': 'e' * 24, 'status': 'open', 'verification': {'reason': 'Confirmed.'}}
        value['findings'][anchored['finding_id']] = anchored
        writes = []
        def api(repo, path, data=None, method=None):
            if path == 'pulls/7':
                return pr()
            if method == 'POST':
                writes.append(data)
                return {'id': 15}
            return []
        def gql(query, variables):
            raise core.ReviewError('github_graphql_failed', {'error_type': 'RATE_LIMITED'})
        delivery.publish_inline(value, settings(), 'main', api, gql)
        self.assertEqual(len(writes), 1)
        self.assertEqual(fixed['thread_resolve_error'], 'RATE_LIMITED')

    def test_pr_change_still_stops_resolution(self):
        value, _ = self.fixed_state()
        moved = pr(head={'sha': 'c' * 40, 'repo': {'full_name': REPO}})
        with self.assertRaisesRegex(core.ReviewError, 'pr_changed_before_publication'):
            delivery.resolve_fixed(value, 'main', lambda *args: moved, lambda *args: self.threads())

    def test_graphql_errors_keep_only_a_redacted_classification(self):
        response = {'errors': [{'type': 'FORBIDDEN', 'path': ['resolveReviewThread', 0], 'message': 'private message for token ghp_x'}]}
        with patch.dict(os.environ, {'GH_TOKEN': 'fixture'}), patch.object(delivery, 'request_json', return_value=response):
            with self.assertRaises(core.ReviewError) as caught:
                delivery.graphql('query', {})
        self.assertEqual((str(caught.exception), caught.exception.diagnostics),
                         ('github_graphql_failed', {'error_type': 'FORBIDDEN', 'path': 'resolveReviewThread'}))
        self.assertNotIn('private', repr(caught.exception.diagnostics))
        cases = [({'errors': [{'type': 'lowercase <script>', 'path': ['x' * 80]}]}, {'error_type': 'unclassified'}),
                 ({'errors': [{'message': 'only text'}]}, {'error_type': 'unclassified'}),
                 ({'data': None}, {'error_type': 'missing_data'}), ([], {'error_type': 'missing_data'})]
        for value, expected in cases:
            with self.subTest(value=value), patch.dict(os.environ, {'GH_TOKEN': 'fixture'}), \
                    patch.object(delivery, 'request_json', return_value=value), self.assertRaises(core.ReviewError) as caught:
                delivery.graphql('query', {})
            self.assertEqual(caught.exception.diagnostics, expected)
        with patch.dict(os.environ, {'GH_TOKEN': 'fixture'}), patch.object(delivery, 'request_json', return_value={'data': {'ok': 1}}):
            self.assertEqual(delivery.graphql('query', {}), {'ok': 1})


if __name__ == '__main__':
    unittest.main()
