import contextlib
import copy
import html
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import patch

from independent_review import cli, core, delivery, service, snapshot, state
from support import (KEY, NUMBER, REPO, ROOT, FakeGitHub, Repository, Scripted, clean_env, decision, finding,
                     make_bundle, result_block, settings)


class SnapshotCase(unittest.TestCase):
    """One repository and snapshot per class; every test gets fresh bundles and state."""

    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temporary.name)
        cls.repo = Repository(cls.root)
        cls.out = cls.root / 'out'
        cls.base_bundle = make_bundle(cls.repo, cls.out)
        cls.workspace = snapshot.Snapshot.open(cls.out, cls.base_bundle['snapshot'], cls.root / 'work')

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def setUp(self):
        self.env = patch.dict(os.environ, clean_env(), clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def bundle(self, **changes):
        value = copy.deepcopy(self.base_bundle)
        value.update(changes)
        return value

    def run_all(self, data, runner, slots=('Grok', 'GPT')):
        reviews = [service.generate(data, slot, self.workspace, runner.runners()) for slot in slots]
        verifications = [service.verify(data, slot, reviews, self.workspace, runner.runners()) for slot in slots]
        return reviews, verifications, service.combine(data, reviews, verifications, self.workspace)


class GenerationTests(SnapshotCase):
    def test_evidence_must_come_from_the_snapshot_head_base_or_diff(self):
        quotes = {'head': 'head = value[0]', 'base': 'def first(value):\n    if not value:',
                  'diff': '-    if not value:\n-        return None'}
        findings = [finding(evidence=quote, title=name) for name, quote in quotes.items()]
        findings += [finding(evidence='invented_guard(value)'), finding(path='src/caller.py', evidence='return first([])')]
        runner = Scripted(findings={'grok': findings})
        review = service.generate(self.bundle(), 'Grok', self.workspace, runner.runners())
        self.assertEqual(review['status'], 'partial')
        sources = {item['title']: item['evidence_source'] for item in review['findings']}
        self.assertEqual(sources, {'head': 'head_text', 'base': 'base_text', 'diff': 'patch'})
        self.assertEqual([item['error'] for item in review['rejected_findings']],
                         ['finding_evidence_not_in_snapshot', 'invalid_finding_location_or_severity'])
        # Only a demonstrated added line becomes an inline anchor.
        anchored = {item['title']: item['anchor'] for item in review['findings']}
        self.assertEqual(anchored['head'], {'line': 2, 'side': 'RIGHT'})
        self.assertEqual(review['usage']['total_tokens'], 120)
        self.assertEqual(review['trace']['tool_calls'], 3)
        self.assertEqual((review['model'], review['effort']), ('responses_tools-model', 'xhigh'))
        self.assertEqual(runner.tasks[0]['effort'], 'xhigh')
        self.assertEqual(runner.tasks[0]['schema'], service.REVIEW_SCHEMA)
        # Short quotes and malformed candidate fields are rejected individually, never crash the lane.
        runner = Scripted(findings={'grok': [finding(evidence='return'), finding(path=['src/a.py']), finding(severity='P3'),
                                             finding(trigger_steps=[]), finding()]})
        review = service.generate(self.bundle(), 'Grok', self.workspace, runner.runners())
        self.assertEqual([item['error'] for item in review['rejected_findings']],
                         ['finding_evidence_too_short', 'invalid_finding_location_or_severity',
                          'invalid_finding_location_or_severity', 'invalid_finding_trigger'])
        self.assertEqual((review['status'], len(review['findings'])), ('partial', 1))

    def test_invalid_json_or_schema_fails_the_lane_and_keeps_usage(self):
        for raw in ('not json', '[]', json.dumps({'summary': 'x', 'findings': [{}] * 6})):
            def runner(backend, task, workspace, raw=raw):
                return raw, 'model', {'total_tokens': 321}, {}
            with self.subTest(raw=raw[:20]):
                review = service.generate(self.bundle(), 'Grok', self.workspace, {'responses_tools': runner})
                self.assertEqual(review['status'], 'failed')
                self.assertEqual(review['attempts'][0]['stage'], 'validation')
                self.assertEqual(review['usage']['total_tokens'], 321)

    def test_missing_or_mismatched_nonce_keeps_findings_but_is_partial(self):
        for value, error in (('absent', 'input_end_nonce_missing'), ('', 'input_end_nonce_missing'),
                             ('f' * 16, 'input_end_nonce_mismatch')):
            with self.subTest(value=value):
                runner = Scripted(findings={'gpt': [finding()]}, nonce=value)
                review = service.generate(self.bundle(), 'GPT', self.workspace, runner.runners())
                self.assertEqual((review['status'], review['error'], review['attempts'][0]['error']), ('partial', error, error))
                self.assertEqual(len(review['findings']), 1)
                self.assertNotIn('f' * 16, json.dumps(review))

    def test_every_call_ends_with_a_fresh_nonce_outside_bundle_identity(self):
        runner = Scripted(findings={'grok': [finding()]})
        first = self.run_all(self.bundle(), runner)[2]
        second = self.run_all(self.bundle(), runner)[2]
        prompts = [task['prompt'] for task in runner.tasks]
        nonces = [prompt.rsplit('\nEND_OF_INPUT_NONCE=', 1)[1] for prompt in prompts]
        self.assertEqual(len(prompts), 6)
        self.assertTrue(all(len(nonce) == 16 for nonce in nonces))
        self.assertEqual(len(set(nonces)), 6)
        self.assertEqual((first['packet_id'], first['bundle_id']), (second['packet_id'], second['bundle_id']))
        self.assertFalse(any(nonce in json.dumps([first, second]) for nonce in nonces))

    def test_unchanged_lane_reuses_its_baseline_and_skipped_lane_calls_nothing(self):
        data = self.bundle()
        data['packet']['lanes']['Grok'] = {'paths': [], 'reason': 'identical_successful_snapshot', 'generate': True}
        data['packet']['lanes']['GPT'] = {'paths': None, 'reason': 'below_generation_threshold', 'generate': False}
        data['state']['lanes']['Grok'] = {'review': {'slot': 'Grok', 'status': 'completed', 'summary': 'Cached', 'findings': [],
                                                     'access': {'files_examined': 4, 'tool_calls': 7, 'files_read': 3}}}
        runner = Scripted()
        reviews, verifications, result = self.run_all(data, runner)
        self.assertEqual(runner.calls, [])
        self.assertEqual([(item['status'], item['scope']) for item in reviews],
                         [('reused', 'identical_successful_snapshot'), ('skipped', 'below_generation_threshold')])
        self.assertEqual({item['status'] for item in verifications}, {'not_needed'})
        self.assertEqual((result['status'], result['accounted_tokens']), ('completed', 0))


class VerificationTests(SnapshotCase):
    def test_grok_candidate_is_routed_to_gpt_and_confirmed(self):
        runner = Scripted(findings={'grok': [finding()]})
        reviews, verifications, result = self.run_all(self.bundle(), runner)
        self.assertEqual(runner.calls, [('grok', 'review'), ('gpt', 'review'), ('gpt', 'verify')])
        self.assertEqual([item['status'] for item in verifications], ['not_needed', 'completed'])
        self.assertEqual(runner.tasks[-1]['effort'], 'xhigh')
        self.assertEqual(runner.tasks[-1]['schema'], service.VERIFY_SCHEMA)
        self.assertEqual(result['status'], 'completed')
        (item,) = result['findings']
        self.assertEqual((item['status'], item['verification']['verifier'], item['sources']), ('open', 'GPT', ['Grok']))
        self.assertEqual(result['accounted_tokens'], 360)
        self.assertEqual((result['engine_version'], result['description_truncated']), ('0.4.0', False))

    def test_skipped_gpt_lane_still_verifies_grok_candidates(self):
        cfg = settings(generation={'GPT': {'min_changed_lines': 1000}})
        with tempfile.TemporaryDirectory() as out:
            data = make_bundle(self.repo, out, cfg=cfg)
        self.assertEqual(data['packet']['lanes']['GPT'], {'paths': None, 'reason': 'below_generation_threshold', 'generate': False})
        data['snapshot'] = self.base_bundle['snapshot']
        runner = Scripted(findings={'grok': [finding()]})
        reviews, _, result = self.run_all(data, runner)
        self.assertEqual(runner.calls, [('grok', 'review'), ('gpt', 'verify')])
        self.assertEqual(reviews[1]['status'], 'skipped')
        self.assertEqual((result['status'], result['findings'][0]['status']), ('completed', 'open'))
        reserved = state.reserve(data['state'], data, '1:1', 'url')
        accepted = state.accept(reserved, result, '1:1')
        self.assertEqual(set(accepted['lanes']), {'Grok'})
        text = delivery.summary(accepted, cfg['limits'])
        self.assertIn("Skipped: below the generation threshold for this lane; it still verifies the other lane's candidates",
                      html.unescape(text))

    def test_transient_generation_failure_still_verifies_the_other_lane(self):
        failure = core.ReviewError('http_502', {'stage': 'headers'})
        runner = Scripted(findings={'grok': [finding()]}, fail={('gpt', 'review'): failure})
        reviews, verifications, result = self.run_all(self.bundle(), runner)
        self.assertEqual(runner.calls, [('grok', 'review'), ('gpt', 'review'), ('gpt', 'verify')])
        self.assertEqual((reviews[1]['status'], verifications[1]['status']), ('failed', 'completed'))
        self.assertEqual((result['status'], result['findings'][0]['status']), ('partial', 'open'))

    def test_verification_is_skipped_after_a_persistent_provider_failure(self):
        failure = core.ReviewError('http_401', {'stage': 'headers'})
        runner = Scripted(findings={'grok': [finding()]}, fail={('gpt', 'review'): failure})
        reviews, verifications, result = self.run_all(self.bundle(), runner)
        self.assertEqual(runner.calls, [('grok', 'review'), ('gpt', 'review')])
        self.assertEqual((reviews[1]['status'], reviews[1]['attempts'][0]['stage']), ('failed', 'provider'))
        self.assertEqual(reviews[1]['attempts'][0]['diagnostics'], {'stage': 'headers'})
        self.assertEqual(verifications[1]['error'], 'verification_skipped_after_provider_failure')
        self.assertEqual((result['status'], result['findings'][0]['status']), ('partial', 'uncertain'))
        self.assertEqual((reviews[0]['status'], reviews[0]['error']), ('partial', 'verification_incomplete'))
        data = self.bundle()
        accepted = state.accept(state.reserve(data['state'], data, '1', 'url'), result, '1')
        self.assertEqual(accepted['lanes'], {})
        self.assertIn('Do not interpret this status as a clean review', delivery.summary(accepted, data['config']['limits']))

    def test_rejected_verification_quote_keeps_valid_sibling_and_stays_partial(self):
        findings = [finding(), finding(evidence='return head', title='Second')]
        def decide(candidate, family):
            if candidate['title'] == 'Second':
                return decision(candidate, 'dismissed', evidence='invented_guard()', evidence_path='src/a.py')
            return decision(candidate)
        runner = Scripted(findings={'grok': findings}, decide=decide)
        _, verifications, result = self.run_all(self.bundle(), runner)
        statuses = {item['title']: item['status'] for item in result['findings']}
        self.assertEqual(statuses, {'Empty list raises IndexError': 'open', 'Second': 'uncertain'})
        self.assertEqual((verifications[1]['status'], verifications[1]['error']), ('partial', 'verification_evidence_not_in_snapshot'))
        self.assertEqual(result['status'], 'partial')
        self.assertIn('retained as uncertain', delivery.report(result))

    def test_truncated_verification_keeps_decisions_but_is_partial(self):
        runner = Scripted(findings={'grok': [finding()]})
        original = runner.__call__
        def call(backend, task, workspace):
            raw, model, usage, trace = original(backend, task, workspace)
            if task['kind'] == 'verify':
                raw = json.dumps({**json.loads(raw), 'input_end_nonce': 'f' * 16})
            return raw, model, usage, trace
        runners = {'responses_tools': call, 'codex_cli': call}
        data = self.bundle()
        reviews = [service.generate(data, slot, self.workspace, runners) for slot in ('Grok', 'GPT')]
        verifications = [service.verify(data, slot, reviews, self.workspace, runners) for slot in ('Grok', 'GPT')]
        result = service.combine(data, reviews, verifications, self.workspace)
        self.assertEqual((verifications[1]['status'], verifications[1]['error']), ('partial', 'input_end_nonce_mismatch'))
        self.assertEqual((result['status'], result['findings'][0]['status']), ('partial', 'open'))
        self.assertIn('GPT verification: partial', delivery.report(result))

    def test_decision_parser_rejects_bad_identity_and_unsupported_fixes(self):
        sources = service.Sources(self.base_bundle['packet'], self.workspace)
        bound = service.parse_review(json.dumps({'summary': 's', 'findings': [finding()], 'input_end_nonce': 'n'}),
                                     self.base_bundle['packet'], sources, 'n')['findings'][0]
        other = {**bound, 'finding_id': 'a' * 24}
        good = decision(bound)
        for duplicate in (good, {**good, 'finding_id': []}):
            with self.subTest(duplicate=duplicate['finding_id']), self.assertRaisesRegex(core.ReviewError, 'invalid_verification_identity'):
                service.parse_decisions(json.dumps({'decisions': [good, duplicate]}), [bound, other], sources, 'n')
        with self.assertRaisesRegex(core.ReviewError, 'new_candidate_cannot_be_fixed'):
            service.parse_decisions(json.dumps({'decisions': [decision(bound, 'fixed')]}), [bound], sources, 'n')
        with self.assertRaisesRegex(core.ReviewError, 'incomplete_verification'):
            service.parse_decisions(json.dumps({'decisions': []}), [bound], sources, 'n')
        previous = {**bound, 'previous': True}
        # A fix needs current head evidence; removed merge-base text cannot demonstrate it.
        removed = decision(previous, 'fixed', evidence_path='src/a.py', evidence='if not value:')
        with patch.dict(os.environ, {'GROK_API_KEY': 'fixture-secret-key'}):
            parsed = service.parse_decisions(json.dumps({'decisions': [removed], 'input_end_nonce': 'n'}), [previous], sources, 'n')
        self.assertEqual(parsed['decisions'][bound['finding_id']]['status'], 'uncertain')
        self.assertEqual(parsed['rejected_decisions'][0]['error'], 'verification_evidence_not_in_snapshot')
        current = decision(previous, 'fixed', evidence_path='src/a.py', evidence='return head')
        parsed = service.parse_decisions(json.dumps({'decisions': [current], 'input_end_nonce': 'n'}), [previous], sources, 'n')
        self.assertEqual(parsed['decisions'][bound['finding_id']]['status'], 'fixed')
        secret = decision(bound, 'dismissed', evidence_path='src/a.py', evidence='fixture-secret-key ' + 'x' * 5000)
        with patch.dict(os.environ, {'GROK_API_KEY': 'fixture-secret-key'}):
            parsed = service.parse_decisions(json.dumps({'decisions': [secret], 'input_end_nonce': 'n'}), [bound], sources, 'n')
        self.assertNotIn('fixture-secret-key', json.dumps(parsed))
        self.assertLessEqual(len(parsed['rejected_decisions'][0]['evidence_preview']), 1000)


class PreviousFindingTests(SnapshotCase):
    def previous(self, **changes):
        sources = service.Sources(self.base_bundle['packet'], self.workspace)
        bound = service.parse_review(json.dumps({'summary': 's', 'findings': [finding()], 'input_end_nonce': 'n'}),
                                     self.base_bundle['packet'], sources, 'n')['findings'][0]
        return {**bound, 'status': 'open', 'sources': ['Grok'], 'verification': {'status': 'confirmed', 'reason': 'Earlier.'},
                **changes}

    def dismiss(self, old):
        data = self.bundle()
        data['state']['findings'] = {old['finding_id']: old}
        runner = Scripted(decide=lambda candidate, family: decision(candidate, 'dismissed', failing_step='Step 2',
                                                                      evidence_path='src/a.py', evidence='return head'))
        return self.run_all(data, runner)[2], runner

    def test_previous_unpublished_finding_may_be_dismissed(self):
        result, runner = self.dismiss(self.previous())
        self.assertEqual(runner.calls[-1], ('gpt', 'verify'))
        (item,) = result['findings']
        self.assertEqual((item['status'], item['previous'], item['published']), ('dismissed', True, False))
        self.assertEqual(item['verification']['failing_step'], 'Step 2')

    def test_previous_published_finding_cannot_be_dismissed(self):
        result, _ = self.dismiss(self.previous(comment_id=31))
        self.assertEqual(result['findings'][0]['status'], 'uncertain')

    def test_rediscovered_published_finding_keeps_its_protection(self):
        old = self.previous(comment_id=31)
        data = self.bundle()
        data['state']['findings'] = {old['finding_id']: old}
        runner = Scripted(findings={'grok': [finding(title='Reworded')]},
                          decide=lambda candidate, family: decision(candidate, 'dismissed', evidence_path='src/a.py',
                                                                    evidence='return head'))
        result = self.run_all(data, runner)[2]
        (item,) = result['findings']
        self.assertEqual((item['finding_id'], item['published'], item['status']), (old['finding_id'], True, 'uncertain'))

    def test_dismissed_finding_is_rechecked_only_when_its_path_changed(self):
        data = self.bundle()
        old = self.previous(status='dismissed')
        data['state']['findings'] = {old['finding_id']: old}
        reviews = [{'slot': 'Grok', 'status': 'completed', 'findings': []}, {'slot': 'GPT', 'status': 'completed', 'findings': []}]
        sources = service.Sources(data['packet'], self.workspace)
        self.assertEqual(service.plan(data, reviews, sources)[0], [])
        data['packet']['lanes']['Grok'] = {'paths': ['src/a.py'], 'reason': 'incremental', 'generate': True, 'baseline_head': 'd' * 40}
        ordered, _, batches = service.plan(data, reviews, sources)
        self.assertEqual([item['finding_id'] for item in ordered], [old['finding_id']])
        self.assertEqual([item['finding_id'] for item in batches['GPT']], [old['finding_id']])
        # Reused or skipped lanes alone never trigger rechecks.
        idle = [{'slot': 'Grok', 'status': 'reused', 'findings': []}, {'slot': 'GPT', 'status': 'skipped', 'findings': []}]
        self.assertEqual(service.plan(data, idle, sources)[0], [])

    def test_plan_is_deterministic_across_calls_and_result_order(self):
        data = self.bundle()
        old = self.previous(finding_id='e' * 24, severity='P1', evidence='an earlier quote')
        data['state']['findings'] = {old['finding_id']: old}
        runner = Scripted(findings={'grok': [finding()], 'gpt': [finding(evidence='return head', title='GPT finding')]})
        reviews = [service.generate(data, slot, self.workspace, runner.runners()) for slot in ('Grok', 'GPT')]
        sources = service.Sources(data['packet'], self.workspace)
        def shape(plan):
            return ([item['finding_id'] for item in plan[0]],
                    {slot: [item['finding_id'] for item in batch] for slot, batch in plan[2].items()})
        first = shape(service.plan(data, reviews, sources))
        self.assertEqual(first, shape(service.plan(copy.deepcopy(data), list(reversed(reviews)), service.Sources(data['packet'], self.workspace))))
        ordered, batches = first
        self.assertEqual(ordered[0], 'e' * 24)
        self.assertEqual(len(batches['GPT']), 2)
        self.assertEqual(len(batches['Grok']), 1)
        data['config']['limits']['max_verification_candidates'] = 1
        data['verify_finding'] = ordered[-1]
        ordered, overflow, batches = service.plan(data, reviews, sources)
        self.assertEqual(ordered[0]['finding_id'], data['verify_finding'])
        self.assertEqual(len(overflow), 2)


class PhaseTests(SnapshotCase):
    """Run the Action phases in one process with a fake GitHub and stub hardening."""

    def setUp(self):
        super().setUp()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.out = Path(self.directory.name) / 'out'
        self.github = FakeGitHub(self.repo)
        event = Path(self.directory.name) / 'event.json'
        event.write_text(json.dumps({'repository': {'full_name': REPO}, 'inputs': {'pr_number': str(NUMBER)}}))
        self.outputs = Path(self.directory.name) / 'outputs'
        self.publishing = {'GITHUB_REPOSITORY': REPO, 'GITHUB_EVENT_PATH': str(event), 'GITHUB_EVENT_NAME': 'workflow_dispatch',
                           'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ID': '11', 'GITHUB_RUN_ATTEMPT': '1',
                           'GITHUB_OUTPUT': str(self.outputs), 'GH_TOKEN': 'gh-token-fixture-value-0001',
                           'REVIEW_STATE_KEY': KEY, 'REVIEW_PUBLISH': 'true'}
        self.hardened = []
        stub = ModuleType('independent_review.hardening')
        stub.harden_process = lambda: self.hardened.append(True)
        create = snapshot.create
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for manager in (patch.object(core, 'request_json', self.github), patch.object(delivery, 'request_json', self.github),
                        patch.dict(sys.modules, {'independent_review.hardening': stub}),
                        patch.object(snapshot, 'create', lambda remote, *args, **kwargs: create(self.repo.remote, *args)),
                        contextlib.redirect_stdout(io.StringIO())):
            stack.enter_context(manager)

    def args(self, phase, lane=''):
        return SimpleNamespace(phase=phase, root=str(ROOT), config='examples/review.json', out=str(self.out), mode='review',
                               lane=lane, work=str(Path(self.directory.name) / f'work-{phase}'))

    def phase(self, name, lane='', publishing=False, runner=None):
        env = self.publishing if publishing else {}
        with patch.dict(os.environ, env), patch.object(service, 'harness', lambda name: runner):
            return getattr(cli, name)(self.args(name, lane))

    def test_phases_publish_a_verified_review_end_to_end(self):
        runner = Scripted(findings={'grok': [finding()]})
        self.phase('prepare', publishing=True)
        self.assertIn('run=true', self.outputs.read_text())
        self.assertTrue((self.out / 'bundle.json').exists())
        reserved = state.decode(self.github.summary(), KEY, REPO, NUMBER)
        self.assertEqual((reserved['status'], reserved['runs']), ('in_progress', 1))
        for lane in ('Grok', 'GPT'):
            self.phase('generate', lane, runner=runner)
        for lane in ('Grok', 'GPT'):
            self.phase('verify', lane, runner=runner)
        self.assertEqual(len(self.hardened), 4)
        self.assertEqual(runner.calls, [('grok', 'review'), ('gpt', 'review'), ('gpt', 'verify')])
        self.phase('publish', publishing=True)
        body = self.github.summary()
        accepted = state.decode(body, KEY, REPO, NUMBER)
        (item,) = accepted['findings'].values()
        self.assertEqual((accepted['status'], item['status'], item['comment_id']), ('completed', 'open', 9000))
        self.assertEqual(set(accepted['lanes']), {'Grok', 'GPT'})
        block = result_block(body)
        self.assertEqual((block['status'], block['head_sha'], block['merge_base_sha']), ('completed', self.repo.head, self.repo.base))
        self.assertEqual(block['findings'][0]['finding_id'], item['finding_id'])
        self.assertEqual([lane['access']['tool_calls'] for lane in block['lanes']], [3, 3])
        review = [data for method, path, data in self.github.writes if path == f'pulls/{NUMBER}/reviews']
        self.assertEqual(review[0]['commit_id'], self.repo.head)
        self.assertIn('head = value［0］', (self.out / 'result.md').read_text())
        self.assertNotIn('gh-token-fixture-value-0001', (self.out / 'result.json').read_text())

    def test_inference_phases_refuse_publishing_credentials_before_hardening(self):
        self.phase('prepare', publishing=True)
        for name in ('GH_TOKEN', 'REVIEW_STATE_KEY'):
            with self.subTest(name=name), patch.dict(os.environ, {name: 'x' * 40}):
                with self.assertRaisesRegex(core.ReviewError, 'publishing_credentials_in_inference_environment'):
                    self.phase('generate', 'Grok', runner=Scripted())
        with patch.dict(os.environ, {'GROK_MODEL': 'grok-4.7'}), \
                self.assertRaisesRegex(core.ReviewError, 'provider_configuration_changed_after_reservation'):
            self.phase('generate', 'Grok', runner=Scripted())
        self.assertEqual(self.hardened, [])

    def test_missing_lane_artifact_is_reported_and_keeps_the_run_partial(self):
        runner = Scripted(findings={'grok': [finding()]})
        self.phase('prepare', publishing=True)
        self.phase('generate', 'Grok', runner=runner)
        self.phase('verify', 'GPT', runner=runner)
        with self.assertRaisesRegex(core.ReviewError, 'independent_review_incomplete'):
            self.phase('publish', publishing=True)
        body = self.github.summary()
        accepted = state.decode(body, KEY, REPO, NUMBER)
        gpt = accepted['last_reviews'][1]
        self.assertEqual((gpt['status'], gpt['scope'], gpt['errors']), ('failed', 'lane_result_missing', ['lane_result_missing']))
        self.assertIn('No result: the lane job produced no output', body)
        self.assertEqual(accepted['status'], 'partial')
        # Grok's generation and the verification of its candidates completed; only it advances.
        self.assertEqual(set(accepted['lanes']), {'Grok'})


class ReplayTests(unittest.TestCase):
    def test_fake_replay_runs_the_pipeline_without_providers(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = Repository(directory)
            out = Path(directory) / 'replay'
            command = [sys.executable, str(ROOT / 'scripts/replay.py'), '--repo', str(repo.path), '--repository', REPO,
                       '--pr', '7', '--base', repo.base, '--head', repo.head, '--root', str(ROOT),
                       '--config', 'examples/review.json', '--out', str(out), '--fake', '--lanes', 'Grok']
            done = subprocess.run(command, capture_output=True, text=True, timeout=60, env=clean_env())
            self.assertEqual(done.returncode, 0, done.stderr[-2000:])
            summary = json.loads(done.stdout)
            self.assertEqual(summary['lanes'], {'Grok': 'completed', 'GPT': 'skipped'})
            result = json.loads((out / 'result.json').read_text())
            self.assertEqual(result['reviews'][1]['scope'], 'replay_lane_not_selected')
            self.assertIn('Not run: lane not selected for this replay', (out / 'result.md').read_text())


if __name__ == '__main__':
    unittest.main()
