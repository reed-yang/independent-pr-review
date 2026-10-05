"""Build the PR brief and plan each reviewer lane; source is read later from a snapshot."""

from .config import selected_rules
from .core import ReviewError, digest, github, safe_path


def identity(pr):
    return pr['base']['sha'], pr['head']['sha']


def eligible(pr, repo, default_branch):
    return (pr['state'] == 'open' and not pr.get('draft')
            and (pr['head'].get('repo') or {}).get('full_name') == repo
            and pr['base']['ref'] == default_branch)


class Reader:
    def __init__(self, repo, limit, api=github):
        self.repo, self.limit, self.api = repo, limit, api
        self.reads = 0
        self.cache = {}

    def get(self, path):
        if path in self.cache:
            return self.cache[path]
        if self.reads >= self.limit:
            raise ReviewError('context_api_budget')
        self.reads += 1
        value = self.api(self.repo, path)
        self.cache[path] = value
        return value


def incremental(reader, baseline, pr, config_id, force_full):
    if force_full:
        return None, 'explicit_full_review'
    if not baseline or baseline.get('config_id') != config_id:
        return None, 'missing_or_changed_configuration'
    if baseline.get('base_sha') != pr['base']['sha']:
        return None, 'base_changed'
    if baseline.get('head_sha') == pr['head']['sha']:
        return [], 'identical_successful_snapshot'
    try:
        comparison = reader.get(f"compare/{baseline['head_sha']}...{pr['head']['sha']}")
        files = comparison.get('files', [])
        if (comparison.get('status') != 'ahead' or not comparison.get('commits')
                or comparison['commits'][-1]['sha'] != pr['head']['sha'] or len(files) >= 300):
            return None, 'history_changed_or_compare_incomplete'
        return [f['filename'] for f in files] + [f['previous_filename'] for f in files if 'previous_filename' in f], 'incremental'
    except (ReviewError, KeyError, TypeError):
        return None, 'comparison_unavailable'


def change_size(item):
    """Count changed lines from GitHub's file counts, or from the patch itself."""
    counts = (item.get('additions'), item.get('deletions'))
    if all(type(count) is int and count >= 0 for count in counts):
        return sum(counts)
    return sum(1 for line in (item.get('patch') or '').splitlines() if line.startswith(('+', '-')))


def generation_planned(policy, stats, trigger):
    """Decide whether a lane generates opinions for this run; verification is separate."""
    if not policy:
        return True, None
    if trigger.get('command') in ('full', 'verify') and policy.get('on_full_review', True):
        return True, None
    if trigger.get('action') in policy.get('events', []):
        return True, None
    if set(trigger.get('labels', [])) & set(policy.get('labels', [])):
        return True, None
    # Only configured thresholds count; an absent one would otherwise always pass.
    if any(key in policy and stats[name] >= policy[key] for key, name in
           (('min_changed_lines', 'changed_lines'), ('min_changed_files', 'changed_files'))):
        return True, None
    return False, 'below_generation_threshold'


def build(repo, number, pr, items, merge_base, config, state, trigger, force_full=False, reader=None):
    """Assemble the brief from already listed PR files; shared by Actions and offline replay."""
    limits = config['limits']
    base, head = identity(pr)
    body = pr.get('body') or ''
    brief = {'schema_version': 3, 'repository': repo, 'pr_number': number,
             'base_sha': base, 'merge_base_sha': merge_base, 'head_sha': head,
             'title': pr['title'][:1000], 'description': body[:limits['description_chars']],
             'description_chars': len(body), 'description_truncated': len(body) > limits['description_chars'],
             'files': [], 'omitted': [], 'coverage': 'repository_snapshot_with_read_tools',
             'full_repository_review': False, 'lanes': {}}
    if brief['description_truncated']:
        brief['omitted'].append({'path': '<description>', 'reason': 'description_budget'})
    if len(items) != pr['changed_files']:
        brief['omitted'].append({'path': '<file-list>', 'reason': 'github_file_list_incomplete'})
    paths = [item['filename'] for item in items]
    brief['rules'] = selected_rules(config, paths)
    brief['expected_changed_files'] = pr['changed_files']
    # Patches orient the reviewer; anything over budget stays readable through the snapshot.
    used = 0
    for item in sorted(items, key=lambda value: -change_size(value)):
        path = item['filename']
        if not safe_path(path):
            brief['omitted'].append({'path': path[:500], 'reason': 'unsafe_path'})
            continue
        entry = {'path': path, 'status': item['status'], 'additions': item.get('additions'),
                 'deletions': item.get('deletions'), 'patch': None}
        if safe_path(item.get('previous_filename')):
            entry['previous_filename'] = item['previous_filename']
        patch = item.get('patch')
        if not patch:
            entry['patch_omitted'] = 'no_text_patch'
        elif used + len(patch) > limits['brief_chars']:
            entry['patch_omitted'] = 'brief_budget'
        else:
            entry['patch'] = patch
            used += len(patch)
        brief['files'].append(entry)
    order = {path: index for index, path in enumerate(paths)}
    brief['files'].sort(key=lambda entry: order[entry['path']])
    stats = {'changed_files': len(items), 'changed_lines': sum(change_size(item) for item in items)}
    brief['stats'] = stats
    reader = reader or Reader(repo, limits['max_api_reads'])
    for slot in config['backends']['slots']:
        baseline = state['lanes'].get(slot['id'])
        lane_paths, reason = incremental(reader, baseline, pr, config['config_id'], force_full)
        if lane_paths and any(path not in paths for path in lane_paths):
            # A dependency-only update can affect an unchanged PR hunk.
            lane_paths, reason = None, 'related_context_changed_full_review'
        lane = {'paths': lane_paths, 'reason': reason, 'generate': True}
        if lane_paths:
            lane['baseline_head'] = baseline['head_sha']
        planned, skip = generation_planned(config['generation'].get(slot['id']), stats, trigger)
        if lane_paths != [] and not planned:
            lane.update(generate=False, reason=skip)
        brief['lanes'][slot['id']] = lane
    brief['packet_id'] = digest(brief)
    return brief


def collect(repo, number, pr, config, state, trigger, force_full=False, api=github):
    reader = Reader(repo, config['limits']['max_api_reads'], api)
    base, head = identity(pr)
    items = []
    for page in range(1, 31):
        batch = reader.get(f'pulls/{number}/files?per_page=100&page={page}')
        items.extend(batch)
        if len(batch) < 100:
            break
    try:
        merge_base = reader.get(f'compare/{base}...{head}')['merge_base_commit']['sha']
    except (ReviewError, KeyError, TypeError):
        raise ReviewError('merge_base_unavailable') from None
    brief = build(repo, number, pr, items, merge_base, config, state, trigger, force_full, reader)
    brief['context_reads'] = reader.reads
    current = api(repo, f'pulls/{number}')
    if identity(current) != (base, head) or current['state'] != pr['state'] or current.get('draft') != pr.get('draft'):
        raise ReviewError('pr_changed_during_collection')
    return brief
