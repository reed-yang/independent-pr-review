import contextlib
import copy
import io
import json
import os
import subprocess
import sys
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from independent_review import anchors, cli, config, context, core, state
from support import KEY, NUMBER, REPO, ROOT, clean_env, settings


BASE, HEAD = 'b' * 40, 'a' * 40
PATCH = '@@ -1,2 +1,2 @@\n def first(value):\n-    return None\n+    return value[0]'


def pr(**changes):
    value = {'state': 'open', 'draft': False, 'title': 'Handle values', 'body': '', 'changed_files': 1,
             'head': {'sha': HEAD, 'repo': {'full_name': REPO}}, 'base': {'sha': BASE, 'ref': 'main'}}
    value.update(changes)
    return value


def entry():
    return {'path': 'src/a.py', 'patch': PATCH, 'head_text': 'def first(value):\n    return value[0]\n',
            'base_text': 'def first(value):\n    return None\n'}


def candidate():
    return anchors.bind({'path': 'src/a.py', 'line': 2, 'severity': 'P2', 'title': 'Empty list fails',
                         'body': 'An empty list raises IndexError for callers without a length guard.',
                         'evidence': 'return value[0]'}, entry())


def item(name, additions=1, deletions=1, patch=None, **extra):
    return {'filename': name, 'status': 'modified', 'additions': additions, 'deletions': deletions,
            'patch': patch if patch is not None else '@@ -1 +1 @@\n-old\n+new', **extra}


def bundle(lanes=None):
    cfg = settings()
    lanes = lanes or {slot['id']: {'paths': None, 'reason': 'explicit_full_review', 'generate': True}
                      for slot in cfg['backends']['slots']}
    return {'packet': {'repository': REPO, 'pr_number': NUMBER, 'base_sha': BASE, 'head_sha': HEAD, 'lanes': lanes},
            'config': cfg, 'state': state.initial(REPO, NUMBER), 'default_branch': 'main'}


def result(**changes):
    value = {'repository': REPO, 'pr_number': NUMBER, 'base_sha': BASE, 'merge_base_sha': 'c' * 40, 'head_sha': HEAD,
             'config_id': 'config', 'engine_version': '0.4.0', 'bundle_id': None, 'status': 'completed',
             'coverage': 'repository_snapshot_with_read_tools', 'omitted': [], 'findings': [], 'accounted_tokens': 20,
             'description_truncated': True, 'description_chars': 70000,
             'reviews': [{'slot': 'Grok', 'status': 'completed', 'model': 'grok-4.7', 'effort': 'xhigh', 'scope': 'incremental',
                          'summary': 'Read it.', 'findings': [], 'files_examined': ['a', 'b'],
                          'trace': {'tool_calls': 4, 'files_read': ['a']},
                          'limitations': ['x' * 900] * 6,
                          'observations': [{'kind': 'risk', 'path': 'p' * 900, 'text': 't' * 3000}] * 8},
                         {'slot': 'GPT', 'status': 'skipped', 'scope': 'below_generation_threshold', 'findings': []}]}
    value.update(changes)
    return value


class AnchorTests(unittest.TestCase):
    def test_maps_added_and_deleted_lines_in_multiple_hunks(self):
        lines = anchors.changed_lines('@@ -2,2 +2,2 @@\n-old value\n+new value\n context\n@@ -8 +8,2 @@\n-old second\n+new second\n+extra value')
        self.assertEqual([(l['side'], l['line']) for l in lines], [('LEFT', 2), ('RIGHT', 2), ('LEFT', 8), ('RIGHT', 8), ('RIGHT', 9)])

    def test_ambiguous_repeated_evidence_stays_summary_only(self):
        value = {'patch': '@@ -0,0 +1,2 @@\n+return value[0]\n+return value[0]'}
        self.assertIsNone(anchors.locate(value, 'return value[0]'))
        self.assertEqual(anchors.locate(value, 'return value[0]', 2), {'side': 'RIGHT', 'line': 2})

    def test_context_line_is_never_used_as_an_inline_anchor(self):
        self.assertIsNone(anchors.locate(entry(), 'def first(value):'))

    def test_identity_ignores_title_and_carries_rename(self):
        original = candidate()
        renamed = {**original, 'title': 'Different wording', 'path': 'src/renamed.py'}
        moved = {**entry(), 'path': renamed['path'], 'previous_filename': original['path']}
        self.assertEqual(original['finding_id'], anchors.bind(renamed, moved)['finding_id'])


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

    def test_reservation_charges_generation_only_for_lanes_that_generate(self):
        backends = settings()['backends']['backends']
        grok, gpt = backends['grok-tools'], backends['gpt-codex']
        full = {'paths': None, 'reason': 'explicit_full_review', 'generate': True}
        cases = [({'Grok': full, 'GPT': full}, grok['reservation_tokens'] + gpt['reservation_tokens']),
                 ({'Grok': full, 'GPT': {**full, 'generate': False, 'reason': 'below_generation_threshold'}}, grok['reservation_tokens']),
                 ({'Grok': {**full, 'paths': []}, 'GPT': {**full, 'paths': ['src/a.py']}}, gpt['reservation_tokens'])]
        verification = grok['verification_reservation_tokens'] + gpt['verification_reservation_tokens']
        for lanes, generation in cases:
            with self.subTest(lanes=lanes):
                data = bundle(lanes)
                reserved = state.reserve(data['state'], data, '1:1', 'url')
                # A skipped or unchanged lane still verifies the other family's candidates.
                self.assertEqual(reserved['tokens'], generation + verification)
                self.assertEqual(reserved['reservation']['estimated_tokens'], generation + verification)
        data = bundle()
        data['config']['limits']['max_tokens_per_pr'] = grok['reservation_tokens']
        with self.assertRaisesRegex(core.ReviewError, 'token_budget_exhausted'):
            state.reserve(data['state'], data, '1:1', 'url')

    def test_result_cannot_move_to_another_run_or_pr(self):
        data = bundle()
        reserved = state.reserve(data['state'], data, '1:1', 'url')
        value = result(bundle_id=reserved['reservation']['bundle_id'])
        for run_id, change in [('2:1', {}), ('1:1', {'pr_number': 8}), ('1:1', {'head_sha': 'c' * 40}),
                               ('1:1', {'bundle_id': 'other'})]:
            with self.assertRaisesRegex(core.ReviewError, 'stale_or_unbound'):
                state.accept(reserved, {**value, **change}, run_id)
        accepted = state.accept(reserved, value, '1:1')
        self.assertEqual(accepted['tokens'], 20)
        self.assertEqual(set(accepted['lanes']), {'Grok'})
        with self.assertRaises(core.ReviewError):
            state.accept(accepted, value, '1:1')

    def test_accept_keeps_bounded_notes_identity_and_description_flag(self):
        data = bundle()
        reserved = state.reserve(data['state'], data, '1:1', 'url')
        accepted = state.accept(reserved, result(bundle_id=reserved['reservation']['bundle_id']), '1:1')
        grok, gpt = accepted['last_reviews']
        self.assertEqual(grok['access'], {'files_examined': 2, 'tool_calls': 4, 'files_read': 1})
        self.assertEqual(len(grok['limitations']), 3)
        self.assertTrue(all(len(text) == 300 for text in grok['limitations']))
        self.assertEqual(len(grok['observations']), 5)
        self.assertEqual({len(value) for value in grok['observations'][0].values()}, {4, 300, 500})
        self.assertEqual((gpt['status'], gpt['scope'], gpt['limitations']), ('skipped', 'below_generation_threshold', []))
        self.assertEqual((accepted['merge_base_sha'], accepted['config_id'], accepted['engine_version']), ('c' * 40, 'config', '0.4.0'))
        self.assertEqual((accepted['description_truncated'], accepted['description_chars']), (True, 70000))
        # The baseline review keeps the same bounded notes for a later reuse.
        baseline = accepted['lanes']['Grok']['review']
        self.assertEqual((len(baseline['limitations']), len(baseline['observations'])), (3, 5))
        self.assertNotIn('trace', baseline)
        reused = state.reuse(accepted, 'https://github.com/owner/project/actions/runs/2')
        self.assertEqual(reused['last_reviews'][0]['limitations'], grok['limitations'])
        self.assertEqual(reused['last_reviews'][0]['observations'], grok['observations'])

    def test_bounded_notes_keep_a_full_state_under_capacity(self):
        value = state.initial(REPO, 7)
        lane = result()['reviews'][0]
        value['last_reviews'] = [{**state.lane_access(lane), **state.lane_notes(lane), 'slot': name} for name in ('Grok', 'GPT')]
        value['lanes'] = {name: {'review': state.baseline_review({**lane, 'findings': [
            {'finding_id': 'f' * 24, 'severity': 'P1', 'path': 'p' * 500, 'line': 3, 'title': 't' * 4000,
             'mechanism': 'm' * 4000, 'body': 'b' * 20000}] * 5})} for name in ('Grok', 'GPT')}
        raw = len(json.dumps(value, sort_keys=True, separators=(',', ':')))
        self.assertLess(raw, state.MAX_RAW // 4)
        self.assertNotIn('mechanism', json.dumps(value['lanes']))
        state.encode(value, KEY)

    def test_resume_reuse_restores_completed_state_without_refunding_budget(self):
        value = state.initial(REPO, 7)
        value.update(status='ready', runs=3, tokens=1234, reservation={'run_id': 'interrupted'},
                     last_reviews=[{'slot': 'Grok', 'status': 'completed', 'scope': 'full'}])
        reused = state.reuse(value, 'https://github.com/owner/project/actions/runs/2')
        self.assertEqual(reused['status'], 'completed')
        self.assertEqual((reused['runs'], reused['tokens']), (3, 1234))
        self.assertIsNone(reused['reservation'])
        self.assertEqual(reused['last_reviews'][0]['scope'], 'identical_successful_snapshot')
        self.assertEqual(value['status'], 'ready')

    def test_reuse_reports_a_lane_skipped_by_policy_as_skipped(self):
        value = state.initial(REPO, 7)
        value['last_reviews'] = [{'slot': 'Grok', 'status': 'completed', 'scope': 'incremental', 'limitations': ['old']},
                                 {'slot': 'GPT', 'status': 'completed', 'model': 'gpt-6.1-sol', 'limitations': ['old'],
                                  'access': {'files_examined': 9, 'tool_calls': 9, 'files_read': 9}}]
        lanes = {'Grok': {'paths': [], 'reason': 'identical_successful_snapshot', 'generate': True},
                 'GPT': {'paths': ['src/a.py'], 'reason': 'below_generation_threshold', 'generate': False}}
        grok, gpt = state.reuse(value, 'url', lanes)['last_reviews']
        self.assertEqual((grok['status'], grok['scope'], grok['limitations']), ('reused', 'identical_successful_snapshot', ['old']))
        self.assertEqual((gpt['status'], gpt['scope'], gpt['limitations']), ('skipped', 'below_generation_threshold', []))
        self.assertEqual(gpt['access'], {'files_examined': 0, 'tool_calls': 0, 'files_read': 0})

    def test_oversized_state_fails_before_comment_publication(self):
        value = state.initial(REPO, 7)
        value['extra'] = 'x' * state.MAX_RAW
        with self.assertRaisesRegex(core.ReviewError, 'capacity'):
            state.encode(value, KEY)


class BriefTests(unittest.TestCase):
    trigger = {'command': 'review', 'action': 'synchronize', 'labels': []}

    def build(self, items, cfg=None, prior=None, trigger=None, body='', force_full=False, api=None, changed=None):
        cfg = cfg or settings()
        value = pr(body=body, changed_files=len(items) if changed is None else changed)
        reader = context.Reader(REPO, 10, api or (lambda *args: self.fail('Unexpected API read')))
        return context.build(REPO, NUMBER, value, items, 'c' * 40, cfg, prior or state.initial(REPO, NUMBER),
                             trigger or self.trigger, force_full, reader)

    def test_patches_are_admitted_largest_change_first_within_the_brief_budget(self):
        cfg = settings()
        cfg['limits']['brief_chars'] = 120
        files = [item('src/small.py', 1, 1, '@@ -1 +1 @@\n-a\n+b'),
                 item('src/large.py', 50, 10, '@@ -1 +1 @@\n-' + 'x' * 60 + '\n+' + 'y' * 40),
                 item('src/huge.py', 400, 0, '@@ -0,0 +1 @@\n+' + 'z' * 500),
                 item('src/binary.png', 0, 0, None), item('../escape.py', 1, 0)]
        files[3]['patch'] = None
        brief = self.build(files, cfg)
        entries = {value['path']: value for value in brief['files']}
        # The file list keeps GitHub's order; only patch admission follows change size.
        self.assertEqual([value['path'] for value in brief['files']], ['src/small.py', 'src/large.py', 'src/huge.py', 'src/binary.png'])
        self.assertEqual(entries['src/huge.py']['patch_omitted'], 'brief_budget')
        self.assertIsNone(entries['src/huge.py']['patch'])
        # The larger change wins the remaining budget even though GitHub listed it later.
        self.assertEqual(entries['src/large.py']['patch'], files[1]['patch'])
        self.assertEqual((entries['src/small.py']['patch'], entries['src/small.py']['patch_omitted']), (None, 'brief_budget'))
        cfg['limits']['brief_chars'] = 140
        # A patch over budget does not stop smaller ones from being admitted.
        self.assertEqual(self.build(files, cfg)['files'][0]['patch'], files[0]['patch'])
        self.assertEqual(entries['src/binary.png']['patch_omitted'], 'no_text_patch')
        self.assertIn({'path': '../escape.py', 'reason': 'unsafe_path'}, brief['omitted'])
        self.assertEqual(brief['stats'], {'changed_files': 5, 'changed_lines': 463})
        self.assertEqual(brief['coverage'], 'repository_snapshot_with_read_tools')
        self.assertEqual(context.change_size({'patch': '@@ -1,2 +1,3 @@\n-a\n+b\n+c\n\n same'}), 3)

    def test_description_truncation_and_incomplete_file_list_are_recorded(self):
        cfg = settings()
        cfg['limits']['description_chars'] = 10
        brief = self.build([item('src/a.py')], cfg, body='claim ' * 5, changed=3)
        self.assertEqual(brief['description'], 'claim clai')
        self.assertEqual((brief['description_chars'], brief['description_truncated']), (30, True))
        self.assertIn({'path': '<description>', 'reason': 'description_budget'}, brief['omitted'])
        self.assertIn({'path': '<file-list>', 'reason': 'github_file_list_incomplete'}, brief['omitted'])
        short = self.build([item('src/a.py')], cfg, body='claim')
        self.assertFalse(short['description_truncated'])
        self.assertEqual(short['omitted'], [])

    def test_generation_policy_thresholds_labels_events_and_full_command(self):
        stats = {'changed_lines': 50, 'changed_files': 2}
        review = {'command': 'review', 'action': 'synchronize', 'labels': []}
        cases = [
            (None, review, (True, None)),
            ({'min_changed_lines': 50}, review, (True, None)),
            ({'min_changed_lines': 51}, review, (False, 'below_generation_threshold')),
            # An absent threshold must not default to zero and always pass.
            ({'min_changed_files': 3}, review, (False, 'below_generation_threshold')),
            ({'min_changed_lines': 500, 'min_changed_files': 2}, review, (True, None)),
            ({'min_changed_lines': 500, 'labels': ['deep-review']}, {**review, 'labels': ['deep-review']}, (True, None)),
            ({'min_changed_lines': 500, 'events': ['ready_for_review']}, {**review, 'action': 'ready_for_review'}, (True, None)),
            ({'min_changed_lines': 500}, {**review, 'command': 'full'}, (True, None)),
            ({'min_changed_lines': 500}, {**review, 'command': 'verify'}, (True, None)),
            ({'min_changed_lines': 500, 'on_full_review': False}, {**review, 'command': 'full'}, (False, 'below_generation_threshold')),
            ({'labels': ['deep-review']}, review, (False, 'below_generation_threshold')),
        ]
        for policy, trigger, expected in cases:
            with self.subTest(policy=policy, trigger=trigger):
                self.assertEqual(context.generation_planned(policy, stats, trigger), expected)

    def test_generation_policy_marks_only_its_lane_skipped(self):
        cfg = settings(generation={'GPT': {'min_changed_lines': 100}})
        lanes = self.build([item('src/a.py')], cfg)['lanes']
        self.assertEqual(lanes['Grok'], {'paths': None, 'reason': 'missing_or_changed_configuration', 'generate': True})
        self.assertEqual(lanes['GPT'], {'paths': None, 'reason': 'below_generation_threshold', 'generate': False})
        lanes = self.build([item('src/a.py')], cfg, trigger={**self.trigger, 'command': 'full'}, force_full=True)['lanes']
        self.assertEqual(lanes['GPT'], {'paths': None, 'reason': 'explicit_full_review', 'generate': True})

    def test_incremental_lane_reasons(self):
        cfg = settings()
        files = [item('src/a.py'), item('src/b.py')]
        old = 'd' * 40
        compare = {'status': 'ahead', 'commits': [{'sha': HEAD}], 'files': [{'filename': 'src/a.py'}]}
        def prior(**lanes):
            value = state.initial(REPO, NUMBER)
            value['lanes'] = {name: {'head_sha': HEAD, 'base_sha': BASE, 'config_id': cfg['config_id'], **change}
                              for name, change in lanes.items()}
            return value
        lanes = self.build(files, cfg, prior(Grok={}, GPT={'head_sha': old}), api=lambda *args: compare)['lanes']
        self.assertEqual(lanes['Grok'], {'paths': [], 'reason': 'identical_successful_snapshot', 'generate': True})
        self.assertEqual(lanes['GPT'], {'paths': ['src/a.py'], 'reason': 'incremental', 'generate': True, 'baseline_head': old})
        outside = {**compare, 'files': [{'filename': 'src/a.py'}, {'filename': 'lib/dependency.py'}]}
        lanes = self.build(files, cfg, prior(Grok={'head_sha': old}), api=lambda *args: outside)['lanes']
        self.assertEqual(lanes['Grok'], {'paths': None, 'reason': 'related_context_changed_full_review', 'generate': True})
        self.assertEqual(lanes['GPT']['reason'], 'missing_or_changed_configuration')
        cases = [({'base_sha': 'e' * 40}, None, 'base_changed'), ({'config_id': 'old'}, None, 'missing_or_changed_configuration'),
                 ({'head_sha': old}, {'status': 'diverged'}, 'history_changed_or_compare_incomplete'),
                 ({'head_sha': old}, core.ReviewError('http_404'), 'comparison_unavailable')]
        for change, response, reason in cases:
            def api(*args, response=response):
                if isinstance(response, Exception):
                    raise response
                return response
            with self.subTest(reason=reason):
                lanes = self.build(files, cfg, prior(Grok=change), api=api)['lanes']
                self.assertEqual((lanes['Grok']['paths'], lanes['Grok']['reason']), (None, reason))
        lanes = self.build(files, cfg, prior(Grok={}), force_full=True)['lanes']
        self.assertEqual(lanes['Grok']['reason'], 'explicit_full_review')
        # A lane with nothing new is not marked skipped by its generation policy.
        cfg_policy = settings(generation={'Grok': {'min_changed_lines': 1000}})
        value = prior(Grok={})
        for lane in value['lanes'].values():
            lane['config_id'] = cfg_policy['config_id']
        lanes = self.build(files, cfg_policy, value)['lanes']
        self.assertEqual(lanes['Grok'], {'paths': [], 'reason': 'identical_successful_snapshot', 'generate': True})

    def test_brief_identity_is_deterministic(self):
        files = [item('src/a.py'), item('src/b.py', 9, 9)]
        self.assertEqual(self.build(files)['packet_id'], self.build(copy.deepcopy(files))['packet_id'])


class CollectionTests(unittest.TestCase):
    def api(self, current=None, pages=None):
        calls = []
        def call(repo, path):
            calls.append(path)
            if path == f'pulls/{NUMBER}':
                return current or pr()
            if path.startswith(f'pulls/{NUMBER}/files'):
                page = int(path.rsplit('page=', 1)[1])
                return (pages or [[item('src/a.py', patch=PATCH)]])[page - 1]
            if path.startswith('compare/'):
                return {'merge_base_commit': {'sha': 'c' * 40}}
            raise AssertionError(path)
        return call, calls

    def test_collect_pages_files_and_records_the_merge_base(self):
        pages = [[item(f'src/m{index:03}.py') for index in range(100)], [item('src/last.py')]]
        api, calls = self.api(pr(changed_files=101), pages)
        brief = context.collect(REPO, NUMBER, pr(changed_files=101), settings(), state.initial(REPO, NUMBER),
                                {'command': 'review', 'action': None, 'labels': []}, api=api)
        self.assertEqual(len(brief['files']), 101)
        self.assertEqual(brief['merge_base_sha'], 'c' * 40)
        self.assertEqual(brief['context_reads'], 3)
        self.assertEqual(len([path for path in calls if 'files?' in path]), 2)

    def test_changed_snapshot_is_rejected(self):
        api, _ = self.api(pr(head={'sha': 'c' * 40, 'repo': {'full_name': REPO}}))
        with self.assertRaisesRegex(core.ReviewError, 'changed_during_collection'):
            context.collect(REPO, NUMBER, pr(), settings(), state.initial(REPO, NUMBER), {'command': 'review', 'labels': []}, api=api)

    def test_fork_draft_closed_and_non_default_targets_are_ineligible(self):
        cases = [pr() for _ in range(4)]
        cases[0]['head']['repo']['full_name'] = 'fork/project'
        cases[1]['draft'] = True
        cases[2]['state'] = 'closed'
        cases[3]['base']['ref'] = 'release'
        self.assertTrue(all(not context.eligible(value, REPO, 'main') for value in cases))
        self.assertTrue(context.eligible(pr(), REPO, 'main'))

    def test_reader_bounds_api_reads(self):
        reader = context.Reader(REPO, 1, lambda *args: {'value': 1})
        reader.get('a')
        reader.get('a')
        with self.assertRaisesRegex(core.ReviewError, 'context_api_budget'):
            reader.get('b')

    def test_repository_metadata_route_has_no_trailing_slash(self):
        with patch.dict(os.environ, {'GH_TOKEN': 'test-token'}), patch.object(core, 'request_json', return_value={}) as request:
            core.github(REPO, '')
        self.assertEqual(request.call_args.args[0], 'https://api.github.com/repos/' + REPO)

    def test_action_launcher_cannot_import_consumer_shadow_package(self):
        with tempfile.TemporaryDirectory() as directory:
            shadow = Path(directory) / 'independent_review'
            shadow.mkdir()
            (shadow / '__init__.py').write_text('raise RuntimeError("consumer code executed")')
            result = subprocess.run([sys.executable, '-I', str(ROOT / 'scripts/action_phase.py'), 'cli', 'validate-config',
                                     '--root', str(ROOT), '--config', 'examples/review.json', '--out', directory],
                                    cwd=directory, capture_output=True, text=True, timeout=20, env=clean_env())
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


class CommandTests(unittest.TestCase):
    def event(self, body, user_type='User'):
        return {'action': 'created', 'issue': {'number': 7, 'pull_request': {'url': 'PR'}},
                'comment': {'body': body, 'user': {'login': 'maintainer', 'type': user_type}}}

    def test_commands_check_current_write_permission_and_reject_shell_suffixes(self):
        event = self.event('/review full')
        self.assertEqual(cli.target('issue_comment', event, REPO, 'review', lambda *args: {'permission': 'write'}), (7, 'full'))
        self.assertIsNone(cli.target('issue_comment', event, REPO, 'review', lambda *args: {'permission': 'read'})[0])
        event['comment']['body'] = '/review verify 0123abcd'
        self.assertEqual(cli.target('issue_comment', event, REPO, 'review', lambda *args: {'permission': 'admin'}), (7, 'verify:0123abcd'))
        for text in ('/review full; curl attacker', '/review\nfull', '/review @someone', '/review full now'):
            event['comment']['body'] = text
            self.assertIsNone(cli.target('issue_comment', event, REPO, 'review')[0])
        event['comment'].update(body='/review', user={'type': 'Bot', 'login': state.BOT})
        self.assertIsNone(cli.target('issue_comment', event, REPO, 'review')[0])

    def test_trigger_context_carries_command_action_and_labels(self):
        event = {'action': 'labeled', 'label': {'name': 'deep-review'},
                 'pull_request': {'number': 7, 'labels': [{'name': 'api'}, {'name': 'deep-review'}]}}
        self.assertEqual(cli.trigger_context('pull_request_target', event, 'review'),
                         {'command': 'review', 'action': 'labeled', 'labels': ['api', 'deep-review']})
        self.assertEqual(cli.trigger_context('issue_comment', self.event('/review verify abcdef12'), 'verify:abcdef12'),
                         {'command': 'verify', 'action': None, 'labels': []})

    def test_identical_successful_snapshot_skips_collection_and_reservation(self):
        cfg = settings()
        prior = state.initial(REPO, 7)
        prior['lanes'] = {slot['id']: {'head_sha': HEAD, 'base_sha': BASE, 'config_id': cfg['config_id']}
                          for slot in cfg['backends']['slots']}
        with patch.dict(os.environ, {'GITHUB_EVENT_NAME': 'workflow_dispatch', 'REVIEW_PUBLISH': 'false'}), \
             patch.object(cli, 'metadata', return_value=(REPO, {'inputs': {'pr_number': '7'}}, 'main')), \
             patch.object(cli.configuration, 'load', return_value=cfg), \
             patch.object(cli.state, 'read', return_value=(prior, 1)), \
             patch.object(cli, 'github', return_value=pr()), \
             patch.object(cli, 'run_identity', return_value=('1:1', 'url')), \
             patch.object(cli, 'collect') as collect, patch.object(cli.state, 'reserve') as reserve, \
             contextlib.redirect_stdout(io.StringIO()):
            cli.prepare(SimpleNamespace(root='.', config='unused', mode='review', out='unused'))
            collect.assert_not_called()
            reserve.assert_not_called()


class RuntimeSettingsTests(unittest.TestCase):
    def test_native_step_cap_clamps_only_the_agy_input_budget(self):
        release = json.loads(Path(core.__file__).with_name('agy-release.json').read_text())
        cap = release['native_input']['max_user_input_step_tokens'] - core.NATIVE_WRAPPER_RESERVE_TOKENS
        self.assertEqual((cap, core.NATIVE_WRAPPER_RESERVE_TOKENS), (60000, 4000))
        native = {'harness': 'antigravity_packet', 'default_context_window_tokens': 1048576, 'default_effort': 'medium'}
        with patch.dict(os.environ, {}, clear=True):
            large = core.runtime_settings(native)
            small = core.runtime_settings({**native, 'default_context_window_tokens': 65536})
            grok = core.runtime_settings({'harness': 'responses_tools', 'default_context_window_tokens': 500000,
                                          'default_effort': 'xhigh', 'output_reserve_tokens': 128000})
        self.assertEqual((large['input_budget_tokens'], large['native_step_cap_tokens'], large['context_window_tokens']),
                         (60000, 60000, 1048576))
        self.assertEqual((small['input_budget_tokens'], small['native_step_cap_tokens']), (65536 - 16000, 60000))
        self.assertEqual(grok['input_budget_tokens'], 500000 - 128000)
        self.assertNotIn('native_step_cap_tokens', grok)
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(core.ReviewError, 'prompt_exceeds_configured_context_budget'):
            core.check_context(native, 'x' * (60000 * 3 + 1))
        for value in ({'max_user_input_step_tokens': 64000, 'bytes_per_token': 4}, {'max_user_input_step_tokens': '64000', 'bytes_per_token': 3}, {}):
            with self.subTest(value=value), patch.object(core.Path, 'read_text', return_value=json.dumps({'native_input': value})):
                with self.assertRaisesRegex(core.ReviewError, 'invalid_native_input_cap'):
                    core.runtime_settings(native)

    def test_known_model_limits_and_effort_names_fail_closed(self):
        backend = {'model_env': 'MODEL', 'effort_env': 'EFFORT', 'context_window_env': 'WINDOW', 'harness': 'responses_tools'}
        with patch.dict(os.environ, {'MODEL': 'grok-4.7', 'EFFORT': 'xhigh', 'WINDOW': '1048576'}):
            with self.assertRaisesRegex(core.ReviewError, 'exceeds_model_capacity'):
                core.runtime_settings(backend)
        with patch.dict(os.environ, {'MODEL': 'grok-4.7', 'EFFORT': 'ultra', 'WINDOW': '500000'}):
            with self.assertRaisesRegex(core.ReviewError, 'unsupported_reasoning_effort'):
                core.runtime_settings(backend)
        codex = {**backend, 'harness': 'codex_cli', 'verify_effort_env': 'VERIFY'}
        with patch.dict(os.environ, {'MODEL': 'gpt-6.1-sol', 'EFFORT': 'ultra', 'VERIFY': 'xhigh', 'WINDOW': '1050000'}):
            value = core.runtime_settings(codex)
        self.assertEqual((value['effort'], value['verify_effort']), ('ultra', 'xhigh'))
        with patch.dict(os.environ, {'MODEL': 'gemini-3.8-flash-high', 'EFFORT': 'medium', 'WINDOW': '1048576'}):
            with self.assertRaisesRegex(core.ReviewError, 'native_model_effort_mismatch'):
                core.runtime_settings({**backend, 'harness': 'antigravity_packet'})


if __name__ == '__main__':
    unittest.main()
