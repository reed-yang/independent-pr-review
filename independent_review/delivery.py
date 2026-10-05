"""Publish concise English summaries and verified comments with current-SHA checks."""

import json
import os
import re

from .context import eligible, identity
from .core import ReviewError, github, plain, request_json, failure_description
from .state import BOT, MARKER, encode, lane_access


SUMMARY_LIMIT = 64000
REPORT_LIMIT = 900000
RESULT_MARKER = 'independent-pr-review-result:v1'
ACCESS_NOTE = ('Reviewers read an immutable snapshot of the PR head and merge base through read-only tools '
               "or a read-only sandboxed CLI. Repository code may execute only inside the GPT lane's read-only, "
               'no-network sandbox; this harness itself runs no tests or PR code.')
SCOPES = {'missing_or_changed_configuration': 'Full review: new policy or first successful baseline',
          'explicit_full_review': 'Full review requested', 'incremental': 'Changes since previous successful review',
          'identical_successful_snapshot': 'Same base, head and configuration', 'base_changed': 'Full review: base changed',
          'related_context_changed_full_review': 'Full review: files outside the PR diff changed since the previous review',
          'history_changed_or_compare_incomplete': 'Full review: history changed or comparison incomplete',
          'comparison_unavailable': 'Full review: previous comparison unavailable',
          'no_reviewable_changed_text': 'No reviewable text changes',
          'below_generation_threshold': ("Skipped: below the generation threshold for this lane; "
                                         "it still verifies the other lane's candidates"),
          'lane_result_missing': 'No result: the lane job produced no output',
          'replay_lane_not_selected': 'Not run: lane not selected for this replay'}
STATUS_ORDER = ('open', 'uncertain', 'fixed', 'dismissed')
# Per-list item limits, most detailed first; lists shrink before the size cap fails.
DETAIL = ({'open': 10, 'uncertain': 10, 'dismissed': 10, 'observations': 5, 'limitations': 3, 'records': 50},
          {'open': 5, 'uncertain': 3, 'dismissed': 3, 'observations': 2, 'limitations': 1, 'records': 15},
          {'open': 3, 'uncertain': 0, 'dismissed': 0, 'observations': 0, 'limitations': 0, 'records': 0})


def clip(value, size):
    text = '' if value is None else str(value)
    return text if len(text) <= size else text[:size - 1] + '…'


def scope_label(scope):
    return SCOPES.get(scope, (scope or 'Pending').replace('_', ' '))


def cell(value):
    return plain(value).replace('|', '／')


def access_text(access):
    access = access or {}
    counts = [access.get(key) if type(access.get(key)) is int else 0 for key in ('files_examined', 'tool_calls', 'files_read')]
    if not any(counts):
        return '—'
    return f'{counts[0]} examined · {counts[1]} tool calls · {counts[2]} files read'


def more(lines, items, shown, where):
    if len(items) > shown:
        lines.append(f'- {len(items) - shown} more {where}')


def result_block(state, findings, limit):
    """A machine-readable result for agents; model text cannot close the comment."""
    def count(access, key):
        value = (access or {}).get(key)
        return value if type(value) is int and value >= 0 else 0
    lanes = [{'slot': clip(lane.get('slot'), 40), 'status': clip(lane.get('status'), 40), 'scope': clip(lane.get('scope'), 80),
              'model': clip(lane.get('model'), 100) or None, 'effort': clip(lane.get('effort'), 20) or None,
              'access': {key: count(lane.get('access'), key) for key in ('files_examined', 'tool_calls', 'files_read')}}
             for lane in state.get('last_reviews', [])[:4]]
    records = [{'finding_id': clip(item.get('finding_id'), 24), 'status': clip(item.get('status'), 20),
                'severity': clip(item.get('severity'), 4), 'path': clip(item.get('path'), 300),
                'line': item['line'] if type(item.get('line')) is int else None, 'title': clip(item.get('title'), 200)}
               for item in findings[:limit]]
    value = {'repository': state['repository'], 'pr_number': state['pr_number'],
             'base_sha': state.get('base_sha'), 'merge_base_sha': state.get('merge_base_sha'),
             'head_sha': state.get('head_sha'), 'config_id': state.get('config_id'),
             'engine_version': state.get('engine_version'), 'run_url': state.get('last_run'),
             'status': 'paused' if state.get('paused') else state['status'], 'lanes': lanes,
             'findings': records, 'findings_total': len(findings),
             'counts': {status: sum(1 for item in findings if item.get('status') == status) for status in STATUS_ORDER}}
    text = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'))
    # JSON stays valid: '<', '>', '@' and the second dash of '--' become \u escapes.
    text = text.replace('<', '\\u003c').replace('>', '\\u003e').replace('@', '\\u0040').replace('--', '-\\u002d')
    return f'<!-- {RESULT_MARKER} {text} -->'


def render(state, limits, detail):
    findings = sorted(state['findings'].values(), key=lambda item: (
        STATUS_ORDER.index(item['status']) if item.get('status') in STATUS_ORDER else len(STATUS_ORDER),
        str(item.get('severity')), str(item.get('finding_id'))))
    groups = {status: [item for item in findings if item.get('status') == status] for status in STATUS_ORDER}
    head = (state.get('reservation') or {}).get('head_sha') or state.get('head_sha') or ''
    status = 'paused' if state.get('paused') else state['status'].replace('_', ' ')
    title = f'**{plain(status.capitalize())}**'
    if state.get('last_run'):
        title += f" · `{head[:12] or 'pending'}` · [Workflow run]({state['last_run']})"
    lines = [MARKER, '### Independent AI review', '', title, '',
             f"{len(groups['open'])} verified open · {len(groups['uncertain'])} uncertain · "
             f"{len(groups['fixed'])} verified fixed · {len(groups['dismissed'])} dismissed", '']
    for item in groups['open'][:detail['open']]:
        lines.append(f"- **{plain(item.get('severity'))}** `{plain(clip(item.get('path'), 200))}` — "
                     f"{plain(clip(item.get('title'), 180))} (`{plain(str(item.get('finding_id'))[:8])}`)")
    more(lines, groups['open'], detail['open'], 'verified open findings are listed in the workflow report.')
    if not groups['open'] and state['status'] == 'completed':
        lines.append('No verified actionable findings in the reviewed scope. This is not an approval or a full-repository audit.')
    elif state['status'] == 'ineligible':
        lines.append('This PR is no longer eligible for automatic review. Previous review outcomes are retained below.')
    elif state['status'] not in ('completed', 'ready'):
        lines.append('Review coverage is incomplete or work is pending. Do not interpret this status as a clean review.')
    if state.get('description_truncated'):
        chars = state.get('description_chars')
        size = f'{chars:,} characters' if type(chars) is int else 'more characters than the brief limit'
        lines.extend(['', f"Note: the PR description has {size}; reviewers received only its first "
                          f"{limits['description_chars']:,} characters as the author's claims."])
    lines.extend(['', '| Reviewer | Model | Effort | Status | Scope | Repository read |',
                  '| --- | --- | --- | --- | --- | --- |'])
    for lane in state.get('last_reviews', []):
        lane_status = (lane.get('status') or 'pending').capitalize()
        errors = list(lane.get('errors', []))
        if lane.get('error') and lane['error'] not in errors:
            errors.append(lane['error'])
        if lane.get('rejected_count'):
            errors.append(str(lane['rejected_count']) + ' rejected candidate(s)')
        if errors:
            lane_status += ': ' + ', '.join(errors)
        cells = (lane.get('slot'), clip(lane.get('model'), 100) or '—', clip(lane.get('effort'), 20) or '—',
                 lane_status, scope_label(lane.get('scope')), access_text(lane.get('access')))
        lines.append('| ' + ' | '.join(cell(value) for value in cells) + ' |')
    for lane in state.get('last_reviews', []):
        for note in lane.get('failure_notes', []):
            lines.extend(['', f"**{plain(lane['slot'])}:** {plain(note)}"])
    unresolved = [item for item in groups['fixed'] if item.get('comment_id') and not item.get('thread_resolved')
                  and item.get('thread_resolve_error')]
    if unresolved:
        kinds = ', '.join(sorted({clip(item['thread_resolve_error'], 60) for item in unresolved})[:3])
        lines.extend(['', f'**Publication warning:** {len(unresolved)} verified-fixed review thread(s) could not be '
                          f'resolved ({plain(kinds)}); a later run retries.'])
    lines.extend(['', '<details>', '<summary>Scope, budget and controls</summary>', '', ACCESS_NOTE,
                  f"Omitted inputs: {state.get('omitted_count', 0)}. Runs reserved: {state['runs']}/{limits['max_runs_per_pr']}. "
                  f"Accounted/estimated tokens: {state['tokens']}/{limits['max_tokens_per_pr']}.",
                  'Token accounting is a soft guard; interrupted calls retain their reservation. Subscription usage is not a dollar-cost estimate.', '',
                  'Maintainers: `/review`, `/review full`, `/review pause`, `/review resume`, `/review verify <finding-id>`.',
                  'Commands do not override eligibility or budgets. Pausing prevents future runs; it does not cancel an already running model.', '', '</details>'])
    if groups['uncertain'] and detail['uncertain']:
        lines.extend(['', '<details>', f"<summary>{len(groups['uncertain'])} unresolved or uncertain observations</summary>", ''])
        for item in groups['uncertain'][:detail['uncertain']]:
            reason = (item.get('verification') or {}).get('reason') or 'Not rechecked.'
            lines.append(f"- `{plain(str(item.get('finding_id'))[:8])}` {plain(clip(item.get('title'), 150))}: {plain(clip(reason, 400))}")
        more(lines, groups['uncertain'], detail['uncertain'], 'in the workflow report.')
        lines.extend(['', '</details>'])
    if groups['dismissed'] and detail['dismissed']:
        lines.extend(['', '<details>', f"<summary>{len(groups['dismissed'])} candidate(s) dismissed by cross-family verification</summary>", '',
                      "The other reviewer family's verification dismissed these candidates. They are not reported as bugs; "
                      'they are listed so maintainers can audit the decisions.', ''])
        for item in groups['dismissed'][:detail['dismissed']]:
            verification = item.get('verification') or {}
            verifier = f" by {plain(verification['verifier'])}" if verification.get('verifier') else ''
            lines.append(f"- `{plain(str(item.get('finding_id'))[:8])}` {plain(clip(item.get('title'), 150))} — dismissed{verifier}: "
                         f"{plain(clip(verification.get('reason') or 'No reason recorded.', 400))}")
        more(lines, groups['dismissed'], detail['dismissed'], 'in the workflow report.')
        lines.extend(['', '</details>'])
    notes = []
    for lane in state.get('last_reviews', []):
        observations = (lane.get('observations') or [])[:detail['observations']]
        limitations = (lane.get('limitations') or [])[:detail['limitations']]
        if observations or limitations:
            notes.extend(['', f"**{plain(lane.get('slot'))}**"])
            notes.extend(f"- Observation ({plain(clip(item.get('kind'), 40))}) `{plain(clip(item.get('path'), 200))}`: "
                         f"{plain(clip(item.get('text'), 500))}" for item in observations)
            notes.extend(f'- Limitation: {plain(clip(text, 300))}' for text in limitations)
    if notes:
        lines.extend(['', '<details>', '<summary>Reviewer observations and limitations</summary>', *notes, '', '</details>'])
    lines.extend(['', result_block(state, findings, detail['records'])])
    return '\n'.join(lines) + '\n'


def summary(state, limits, budget=SUMMARY_LIMIT):
    for detail in DETAIL:
        text = render(state, limits, detail)
        if len(text) <= budget:
            return text
    raise ReviewError('summary_capacity_exceeded')


USAGE = (('input_tokens', 'input'), ('cached_input_tokens', 'cached input'), ('output_tokens', 'output'),
         ('reasoning_tokens', 'reasoning'), ('total_tokens', 'total'), ('requests', 'requests'))


def usage_text(usage):
    if not isinstance(usage, dict):
        return None
    parts = [f'{label} {usage[key]:,}' for key, label in USAGE if type(usage.get(key)) is int]
    return '- Usage: ' + ' · '.join(parts) + '.' if parts else None


def facts(item, *extra):
    values = [f"model {plain(item['model'])}" if item.get('model') else None,
              f"effort {plain(item['effort'])}" if item.get('effort') else None, *extra,
              f"elapsed {item['elapsed_seconds']} s" if item.get('elapsed_seconds') is not None else None]
    return '- ' + ' · '.join(value for value in values if value)


def lane_report(lane):
    lines = [f"## {plain(lane['slot'])}: {plain(lane['status'])}", '', facts(lane, f"scope: {plain(scope_label(lane.get('scope')))}")]
    if usage := usage_text(lane.get('usage')):
        lines.append(usage)
    trace = lane.get('trace') if isinstance(lane.get('trace'), dict) else {}
    access = lane_access(lane)['access']
    if any(access.values()) or trace:
        line = (f"- Access: {access['files_examined']} files examined · {access['tool_calls']} tool calls · "
                f"{access['files_read']} files read")
        if trace.get('stopped_reason'):
            line += f" · stopped: {plain(clip(trace['stopped_reason'], 60))}"
        lines.append(line + '.')
    commands = [clip(command, 200) for command in (trace.get('commands') or [])[:5] if isinstance(command, str)]
    if commands:
        lines.append('- Commands (first five): ' + '; '.join(f'`{plain(command)}`' for command in commands))
    for attempt in lane.get('attempts', []):
        if attempt.get('error'):
            lines.append(f"- Failure: {plain(attempt['error'])}; stage: {plain(attempt.get('stage', 'unknown'))}; "
                         f"elapsed: {attempt.get('elapsed_seconds', 'unknown')} seconds.")
            if note := failure_description(attempt):
                lines.append('- ' + note)
            if attempt.get('diagnostics'):
                lines.append('- Transport diagnostics: ' + plain(json.dumps(attempt['diagnostics'], sort_keys=True)))
    if lane.get('error') and not any(attempt.get('error') == lane['error'] for attempt in lane.get('attempts', [])):
        lines.append(f"- Status reason: {plain(lane['error'])}.")
    lines.extend(['', plain(lane.get('summary') or 'No completed opinion.'), ''])
    for finding in lane.get('findings', []):
        lines.append(f"- **{plain(finding.get('severity'))}** `{plain(finding.get('path'))}`"
                     + (f":{finding['line']}" if type(finding.get('line')) is int else '')
                     + f": {plain(finding.get('title'))}" + (f" (`{plain(finding['finding_id'])}`)" if finding.get('finding_id') else ''))
        steps = finding.get('trigger_steps') or []
        if steps:
            lines.append('  - Trigger: ' + ' '.join(f'({index}) {plain(step)}' for index, step in enumerate(steps, 1)))
        for key in ('mechanism', 'consequence'):
            if finding.get(key):
                lines.append(f'  - {key.capitalize()}: {plain(finding[key])}')
        if finding.get('evidence'):
            lines.append(f"  - Evidence ({plain(finding.get('evidence_source') or 'quote')}): {plain(clip(finding['evidence'], 1000))}")
    for rejected in lane.get('rejected_findings', []):
        lines.append(f"- Rejected candidate {rejected['index'] + 1}: {plain(rejected['error'])}; path: "
                     f"{plain(rejected.get('path') or 'unrecognized')}. Redacted evidence diagnostics are in result.json.")
    for item in lane.get('observations', []):
        lines.append(f"- Observation ({plain(item.get('kind'))}) `{plain(item.get('path'))}`: {plain(item.get('text'))}")
    for limitation in lane.get('limitations', []):
        lines.append(f'- Limitation: {plain(limitation)}')
    return lines + ['']


def verification_report(verification):
    lines = [f"### {plain(verification['slot'])} verification: {plain(verification['status'])}", '']
    if verification['status'] == 'not_needed':
        return lines + ['No candidates were routed to this verifier.', '']
    lines.append(facts(verification, f"error {plain(verification['error'])}" if verification.get('error') else None))
    if usage := usage_text(verification.get('usage')):
        lines.append(usage)
    for fid, decision in sorted((verification.get('decisions') or {}).items()):
        step = f"; failing step: {plain(decision['failing_step'])}" if decision.get('failing_step') else ''
        lines.append(f"- `{plain(fid)}` **{plain(decision.get('status'))}** by {plain(verification['slot'])}{step}; "
                     f"reason: {plain(decision.get('reason', ''))}")
    for rejected in verification.get('rejected_decisions', []):
        lines.append(f"- Rejected decision `{plain(rejected['finding_id'])}` ({plain(rejected['error'])}); retained as "
                     'uncertain. Redacted quote diagnostics are in result.json.')
    return lines + ['']


def report(result):
    lines = [f"# Independent review: {plain(result['status'])}", '',
             f"PR: {plain(result['repository'])}#{result['pr_number']} · Base `{result['base_sha']}` · "
             f"Merge base `{result.get('merge_base_sha') or 'unknown'}` · Head `{result['head_sha']}`",
             f"Engine {plain(result.get('engine_version') or 'unknown')} · configuration `{plain(result['config_id'][:16])}`", '',
             'This report contains independent opinions and verification decisions. Raw candidates are not published '
             'as confirmed bugs. ' + ACCESS_NOTE, '']
    if result.get('description_truncated'):
        chars = result.get('description_chars')
        lines.extend([f"The PR description ({chars:,} characters) exceeded the brief limit and was truncated."
                      if type(chars) is int else 'The PR description exceeded the brief limit and was truncated.', ''])
    for lane in result['reviews']:
        lines.extend(lane_report(lane))
    lines.extend(['## Verification', ''])
    for verification in result.get('verifications', []):
        lines.extend(verification_report(verification))
    lines.extend(['## Final finding statuses', ''])
    for finding in result['findings']:
        verification = finding.get('verification') or {}
        verifier = f" by {plain(verification['verifier'])}" if verification.get('verifier') else ''
        note = ' Dismissed by cross-family verification; not reported as a bug.' if finding['status'] == 'dismissed' else ''
        lines.append(f"- `{plain(finding['finding_id'])}` **{plain(finding['status'])}** {plain(finding.get('severity'))} "
                     f"`{plain(finding.get('path'))}`: {plain(finding.get('title'))}. Decision{verifier}: "
                     f"{plain(verification.get('reason', 'none'))}{note}")
    if not result['findings']:
        lines.append('No candidate findings required verification.')
    lines.extend(['', f"Coverage: {plain(result['coverage'])}; omitted inputs: {len(result['omitted'])}."])
    text = '\n'.join(lines) + '\n'
    if len(text) > REPORT_LIMIT:
        text = text[:REPORT_LIMIT] + '\n\nReport truncated; result.json contains every record.\n'
    return text


def write_summary(state, comment_id, key, limits, api=github):
    signed = encode(state, key)
    body = summary(state, limits, SUMMARY_LIMIT - len(signed) - 1) + '\n' + signed
    if len(body) > SUMMARY_LIMIT:
        raise ReviewError('summary_capacity_exceeded')
    if comment_id:
        result = api(state['repository'], f'issues/comments/{comment_id}', {'body': body}, 'PATCH')
    else:
        result = api(state['repository'], f"issues/{state['pr_number']}/comments", {'body': body}, 'POST')
    return result['id']


def current_pr(state, default_branch, api=github):
    pr = api(state['repository'], f"pulls/{state['pr_number']}")
    expected = state.get('reservation') or state
    if not eligible(pr, state['repository'], default_branch) or identity(pr) != (expected.get('base_sha'), expected.get('head_sha')):
        raise ReviewError('pr_changed_before_publication')
    return pr


def graphql_diagnostics(errors):
    """Classify a GraphQL failure by type and top-level field only; messages are never kept."""
    first = errors[0] if isinstance(errors, list) and errors and isinstance(errors[0], dict) else {}
    kind = first.get('type')
    diagnostics = {'error_type': kind if isinstance(kind, str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,39}', kind)
                   else ('unclassified' if errors else 'missing_data')}
    path = first.get('path')
    if isinstance(path, list) and path and isinstance(path[0], str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,39}', path[0]):
        diagnostics['path'] = path[0]
    return diagnostics


def graphql(query, variables):
    response = request_json('https://api.github.com/graphql', os.environ.get('GH_TOKEN', ''), {'query': query, 'variables': variables})
    if not isinstance(response, dict) or response.get('errors') or not isinstance(response.get('data'), dict):
        raise ReviewError('github_graphql_failed', graphql_diagnostics(response.get('errors') if isinstance(response, dict) else None))
    return response['data']


def resolve_error(exc):
    return clip(exc.diagnostics.get('error_type') or str(exc), 60)


THREADS = '''query($owner:String!,$name:String!,$number:Int!,$cursor:String){
  repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){
    pageInfo{hasNextPage endCursor} nodes{id isResolved comments(first:1){nodes{fullDatabaseId author{login __typename}}}}
  }}}}'''


def resolve_fixed(state, default_branch, api=github, gql=graphql):
    """Resolve only this harness's threads; a GitHub failure is recorded and retried later."""
    targets = {item['comment_id']: item for item in state['findings'].values()
               if item['status'] == 'fixed' and item.get('comment_id') and not item.get('thread_resolved')}
    if not targets:
        return
    owner, name = state['repository'].split('/')
    cursor = None
    for _ in range(20):
        try:
            data = gql(THREADS, {'owner': owner, 'name': name, 'number': state['pr_number'], 'cursor': cursor})
            threads = data['repository']['pullRequest']['reviewThreads']
            nodes, page = list(threads['nodes']), dict(threads['pageInfo'])
        except ReviewError as exc:
            error = resolve_error(exc)
        except (KeyError, TypeError):
            error = 'invalid_response'
        else:
            error = None
        if error:
            for item in targets.values():
                item['thread_resolve_error'] = error
            return
        for thread in nodes:
            comments = ((thread.get('comments') or {}).get('nodes') or []) if isinstance(thread, dict) else []
            author = (comments[0].get('author') or {}) if comments and isinstance(comments[0], dict) else {}
            if author.get('__typename') != 'Bot' or author.get('login') != BOT.removesuffix('[bot]'):
                continue
            identifier = str(comments[0].get('fullDatabaseId', ''))
            if not identifier.isdigit() or int(identifier) not in targets:
                continue
            item = targets.pop(int(identifier))
            if not thread.get('isResolved'):
                current_pr(state, default_branch, api)
                try:
                    gql('mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}', {'id': thread['id']})
                except ReviewError as exc:
                    item['thread_resolve_error'] = resolve_error(exc)
                    continue
            item['thread_resolved'] = True
            item.pop('thread_resolve_error', None)
        if not targets or not page.get('hasNextPage'):
            return
        cursor = page.get('endCursor')
    for item in targets.values():
        item['thread_resolve_error'] = 'review_thread_history_incomplete'


def publish_inline(state, config, default_branch, api=github, gql=graphql):
    if not config['inline_comments']:
        return
    # Recover our own already-posted root comments after a network interruption.
    recover = [item for item in state['findings'].values() if item['status'] == 'open' and not item.get('comment_id')]
    if recover:
        for page in range(1, 21):
            comments = api(state['repository'], f"pulls/{state['pr_number']}/comments?per_page=100&page={page}")
            for comment in comments:
                if comment['user']['login'] != BOT or comment.get('in_reply_to_id'):
                    continue
                for item in recover:
                    if f"<!-- independent-pr-review-finding:{item['finding_id']} -->" in comment['body']:
                        item['comment_id'] = comment['id']
            if len(comments) < 100:
                break
        else:
            raise ReviewError('review_comment_history_incomplete')
    pending = [item for item in state['findings'].values() if item['status'] == 'open' and item.get('anchor') and not item.get('comment_id')]
    pending.sort(key=lambda item: (item['severity'], item['finding_id']))
    pending = pending[:config['limits']['max_inline_comments']]
    if pending:
        current_pr(state, default_branch, api)
        comments = []
        for item in pending:
            body = (f"**{item['severity']}: {plain(item['title'])}**\n\n{plain(item['body'])}\n\n"
                    f"Independent verification: {plain(item['verification']['reason'])}\n\n"
                    f"Finding `{item['finding_id'][:8]}` · AI suggestion; maintainer judgment required.\n"
                    f"<!-- independent-pr-review-finding:{item['finding_id']} -->")
            comments.append({'path': item['path'], **item['anchor'], 'body': body})
        response = api(state['repository'], f"pulls/{state['pr_number']}/reviews",
                       {'commit_id': state['head_sha'], 'event': 'COMMENT',
                        'body': 'Verified independent AI findings. See the persistent summary for coverage and limitations.', 'comments': comments}, 'POST')
        # Retrieve only the comments in the review we just created; never adopt
        # an arbitrary user-authored marker or resolve someone else's thread.
        posted = api(state['repository'], f"pulls/{state['pr_number']}/reviews/{response['id']}/comments?per_page=100")
        for comment in posted:
            if comment['user']['login'] != BOT:
                continue
            for item in pending:
                if f"<!-- independent-pr-review-finding:{item['finding_id']} -->" in comment['body']:
                    item['comment_id'] = comment['id']
                    item['review_id'] = response['id']
    resolve_fixed(state, default_branch, api, gql)
