"""Publish concise English summaries and verified comments with current-SHA checks."""

import json
import os

from .context import eligible, identity
from .core import ReviewError, github, plain, request_json, failure_description
from .state import BOT, MARKER, encode


def summary(state, limits):
    findings = list(state['findings'].values())
    active = [item for item in findings if item['status'] == 'open']
    uncertain = [item for item in findings if item['status'] == 'uncertain']
    fixed = [item for item in findings if item['status'] == 'fixed']
    head = (state.get('reservation') or {}).get('head_sha') or state.get('head_sha', '')
    status = state['status'].replace('_', ' ')
    if state['paused']:
        status = 'paused'
    lines = [MARKER, '### Independent AI review', '',
             f"**{plain(status.capitalize())}** · `{head[:12] or 'pending'}` · "
             f"[Workflow run]({state['last_run']})" if state.get('last_run') else f'**{plain(status.capitalize())}**', '',
             f"{len(active)} verified open · {len(uncertain)} uncertain · {len(fixed)} verified fixed", '']
    for item in active[:10]:
        lines.append(f"- **{item['severity']}** `{plain(item['path'])}` — {plain(item['title'][:180])} (`{item['finding_id'][:8]}`)")
    if not active and state['status'] == 'completed':
        lines.append('No verified actionable findings in the reviewed scope. This is not an approval or a full-repository audit.')
    elif state['status'] == 'ineligible':
        lines.append('This PR is no longer eligible for automatic review. Previous review outcomes are retained below.')
    elif state['status'] not in ('completed', 'ready'):
        lines.append('Review coverage is incomplete or work is pending. Do not interpret this status as a clean review.')
    lines.extend(['', '| Reviewer | Status | Scope |', '| --- | --- | --- |'])
    scopes = {'missing_or_changed_configuration': 'Full review: new policy or first successful baseline',
              'explicit_full_review': 'Full review requested', 'incremental': 'Changes since previous successful review',
              'identical_successful_snapshot': 'Same base, head and configuration', 'base_changed': 'Full review: base changed',
              'related_context_changed_full_review': 'Full review: related context changed',
              'history_changed_or_compare_incomplete': 'Full review: history changed or comparison incomplete',
              'comparison_unavailable': 'Full review: previous comparison unavailable',
              'no_reviewable_changed_text': 'No reviewable text changes'}
    for lane in state.get('last_reviews', []):
        name = lane['slot'] + (' · ' + lane['model'] if lane.get('model') else '')
        if lane.get('effort'):
            name += ' · ' + lane['effort']
        if lane.get('context_window_tokens'):
            name += ' · ' + format(lane['context_window_tokens'], ',') + ' context'
        status = (lane.get('status') or 'pending').capitalize()
        errors = list(lane.get('errors', []))
        if lane.get('rejected_count'):
            errors.append(str(lane['rejected_count']) + ' rejected candidate(s)')
        if errors:
            status += ': ' + ', '.join(errors)
        scope = scopes.get(lane.get('scope'), (lane.get('scope') or 'Pending').replace('_', ' '))
        cells = (name, status, scope)
        lines.append('| ' + ' | '.join(plain(cell).replace('|', '／') for cell in cells) + ' |')
    for lane in state.get('last_reviews', []):
        for note in lane.get('failure_notes', []):
            lines.extend(['', f"**{plain(lane['slot'])}:** {plain(note)}"])
    lines.extend(['', '<details>', '<summary>Scope, budget and controls</summary>', '',
                  'The reviewers received bounded diff and related-source context. Repository code was not executed; no tests were run by this harness.',
                  f"Omitted inputs: {state.get('omitted_count', 0)}. Runs reserved: {state['runs']}/{limits['max_runs_per_pr']}. "
                  f"Accounted/estimated tokens: {state['tokens']}/{limits['max_tokens_per_pr']}.",
                  'Token accounting is a soft guard; interrupted calls retain their reservation. Subscription usage is not a dollar-cost estimate.', '',
                  'Maintainers: `/review`, `/review full`, `/review pause`, `/review resume`, `/review verify <finding-id>`.',
                  'Commands do not override eligibility or budgets. Pausing prevents future runs; it does not cancel an already running model.', '', '</details>'])
    if uncertain:
        lines.extend(['', '<details>', f'<summary>{len(uncertain)} unresolved or uncertain observations</summary>', ''])
        for item in uncertain[:10]:
            lines.append(f"- `{item['finding_id'][:8]}` {plain(item['title'][:150])}: {plain(item.get('verification', {}).get('reason', 'Not rechecked.')[:400])}")
        lines.extend(['', '</details>'])
    return '\n'.join(lines) + '\n'


def report(result):
    lines = [f"# Independent review: {result['status']}", '',
             f"PR: {result['repository']}#{result['pr_number']} · Head: `{result['head_sha']}`", '',
             'This report contains independent opinions and verification decisions. Raw candidates are not published as confirmed bugs.', '']
    for lane in result['reviews']:
        lines.extend([f"## {plain(lane['slot'])}: {plain(lane['status'])}", '', plain(lane.get('summary', 'No completed opinion.')), ''])
        if lane.get('elapsed_seconds') is not None:
            lines.append(f"- Elapsed: {lane['elapsed_seconds']} seconds.")
        for attempt in lane.get('attempts', []):
            if attempt.get('error'):
                lines.append(f"- Failure: {plain(attempt['error'])}; stage: {plain(attempt.get('stage', 'unknown'))}; elapsed: {attempt.get('elapsed_seconds', 'unknown')} seconds.")
                if note := failure_description(attempt):
                    lines.append('- ' + note)
                if attempt.get('diagnostics'):
                    lines.append('- Transport diagnostics: ' + plain(json.dumps(attempt['diagnostics'], sort_keys=True)))
        for rejected in lane.get('rejected_findings', []):
            lines.append(f"- Rejected candidate {rejected['index'] + 1}: {plain(rejected['error'])}; path: {plain(rejected.get('path') or 'unrecognized')}. Redacted evidence diagnostics are in result.json.")
        for finding in lane.get('findings', []):
            lines.extend([f"- {finding['severity']} `{plain(finding['path'])}`: {plain(finding['title'])}",
                          f"  {plain(finding['body'])}"])
        for limitation in lane.get('limitations', []):
            lines.append(f'- Limitation: {plain(limitation)}')
        if lane.get('input_context'):
            context = lane['input_context']
            lines.append(f"- Input: estimated {context['estimated_prompt_tokens']:,} tokens; configured window {context['context_window_tokens']:,}; effort {lane.get('effort') or 'default'}.")
            lines.append(f"- Lane-specific omissions: {len(context.get('omitted', [])) if 'omitted' in context else context.get('omitted_count', 0)}. Estimates reserve space for native overhead and output; they are not exact tokenizer counts.")
    lines.extend(['', '## Verification', ''])
    for finding in result['findings']:
        lines.append(f"- `{finding['finding_id']}` **{finding['status']}**: {plain(finding['verification']['reason'])}")
    if not result['findings']:
        lines.append('No candidate findings required verification.')
    lines.extend(['', f"Coverage: {result['coverage']}; omitted inputs: {len(result['omitted'])}. No repository tests were executed."])
    return '\n'.join(lines) + '\n'


def write_summary(state, comment_id, key, limits, api=github):
    body = summary(state, limits) + '\n' + encode(state, key)
    if len(body) > 64000:
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


def graphql(query, variables):
    response = request_json('https://api.github.com/graphql', os.environ.get('GH_TOKEN', ''), {'query': query, 'variables': variables})
    if response.get('errors') or not isinstance(response.get('data'), dict):
        raise ReviewError('github_graphql_failed')
    return response['data']


def resolve_fixed(state, default_branch, api=github, gql=graphql):
    targets = {item['comment_id']: item for item in state['findings'].values()
               if item['status'] == 'fixed' and item.get('comment_id') and not item.get('thread_resolved')}
    if not targets:
        return
    owner, name = state['repository'].split('/')
    cursor = None
    for _ in range(20):
        data = gql('''query($owner:String!,$name:String!,$number:Int!,$cursor:String){
          repository(owner:$owner,name:$name){pullRequest(number:$number){reviewThreads(first:100,after:$cursor){
            pageInfo{hasNextPage endCursor} nodes{id isResolved comments(first:1){nodes{fullDatabaseId author{login __typename}}}}
          }}}}''', {'owner': owner, 'name': name, 'number': state['pr_number'], 'cursor': cursor})
        threads = data['repository']['pullRequest']['reviewThreads']
        for thread in threads['nodes']:
            comments = thread['comments']['nodes']
            author = (comments[0].get('author') or {}) if comments else {}
            if author.get('__typename') != 'Bot' or author.get('login') != BOT.removesuffix('[bot]'):
                continue
            identifier = str(comments[0].get('fullDatabaseId', ''))
            if not identifier.isdigit() or int(identifier) not in targets:
                continue
            item = targets.pop(int(identifier))
            if not thread['isResolved']:
                current_pr(state, default_branch, api)
                gql('mutation($id:ID!){resolveReviewThread(input:{threadId:$id}){thread{id isResolved}}}', {'id': thread['id']})
            item['thread_resolved'] = True
        if not targets or not threads['pageInfo']['hasNextPage']:
            return
        cursor = threads['pageInfo']['endCursor']
    raise ReviewError('review_thread_history_incomplete')


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
