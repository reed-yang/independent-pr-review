"""Collect immutable code as data with explicit file, character and API budgets."""

import base64
from fnmatch import fnmatch
import json
from pathlib import PurePosixPath
import re
from urllib.parse import quote

from .config import selected_rules
from .core import ReviewError, digest, github, safe_path


TEXT_SUFFIXES = {'.py', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.cjs', '.rs', '.go',
                 '.java', '.kt', '.swift', '.c', '.h', '.cpp', '.cs', '.rb', '.sh',
                 '.yml', '.yaml', '.json', '.toml', '.md', '.sql', '.css', '.html'}
# The remaining budget bounds each file. This absolute ceiling sits near the 1 MB
# limit up to which the contents API returns base64 file content.
MAX_TEXT_CHARS = 1000000
# Individually named related-context omissions before one aggregate record.
RELATED_OMISSION_RECORDS = 20


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

    def text(self, path, sha, max_chars=MAX_TEXT_CHARS):
        """Return UTF-8 text or None when unreadable; raise when it exceeds max_chars."""
        max_chars = min(max_chars, MAX_TEXT_CHARS)
        if not safe_path(path):
            return None
        content = self.get(f'contents/{quote(path, safe="/")}?ref={sha}')
        if (not isinstance(content, dict) or content.get('type') != 'file'
                or content.get('encoding') != 'base64'):
            return None
        # UTF-8 uses at most four bytes per character.
        if content.get('size', 0) > max_chars * 4:
            raise ReviewError('context_budget')
        try:
            value = base64.b64decode(content['content']).decode('utf-8')
        except (KeyError, ValueError, UnicodeError):
            return None
        if '\x00' in value:
            return None
        if len(value) > max_chars:
            raise ReviewError('context_budget')
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
    return sum(1 for line in item['patch'].splitlines() if line.startswith(('+', '-')))


def related_candidates(paths, entries, tree, context):
    """Rank explicit includes, imports, sibling tests and likely caller modules."""
    available = {entry['path'] for entry in tree if entry.get('type') == 'blob'
                 and safe_path(entry['path']) and PurePosixPath(entry['path']).suffix in TEXT_SUFFIXES}
    selected = {}
    stems = {PurePosixPath(path).stem for path in paths}
    directories = {str(PurePosixPath(path).parent) for path in paths}
    imports = []
    for entry in entries:
        source = entry.get('head_text') or entry['patch']
        imports.extend(re.findall(r'(?:from\s+|import\s*\(?\s*)[\'\"]([^\'\"]+)[\'\"]', source))
        for module in re.findall(r'^\s*(?:from|import)\s+([\w.]+)', source, re.M):
            imports.append(module.replace('.', '/'))
    # A bare relative import must not become the empty suffix, which matches
    # every path. Nearby modules are still collected through the sibling rule.
    imports = [module.lstrip('./') for module in imports if module.lstrip('./')]
    for path in available - set(paths):
        pure = PurePosixPath(path)
        roots = context.get('source_roots', [])
        if roots and not any(path.startswith(root.rstrip('/') + '/') for root in roots) and not any(fnmatch(path, p) for p in context.get('include', [])):
            continue
        rank, reason = 0, ''
        if any(fnmatch(path, pattern) for pattern in context.get('include', [])):
            rank, reason = 100, 'configured_context'
        if any(str(pure.with_suffix('')).endswith(module) or pure.stem == PurePosixPath(module).name for module in imports):
            rank, reason = max(rank, 90), reason or 'import_dependency'
        if any(stem in pure.stem for stem in stems) and ('test' in path.lower() or 'spec' in path.lower()):
            rank, reason = max(rank, 80), reason or 'related_test'
        if str(pure.parent) in directories:
            rank, reason = max(rank, 40), reason or 'sibling_module'
        if rank:
            selected[path] = (rank, reason)
    return [(path, value[1]) for path, value in sorted(selected.items(), key=lambda item: (-item[1][0], item[0]))]


def collect(repo, number, pr, config, state, force_full=False, api=github):
    limits = config['limits']
    # Reserve two reads for immutable identity checks outside the bounded reader.
    reader = Reader(repo, limits['max_api_reads'], api)
    base, head = identity(pr)
    packet = {'schema_version': 2, 'repository': repo, 'pr_number': number,
              'base_sha': base, 'head_sha': head, 'title': pr['title'][:1000],
              'description': (pr.get('body') or '')[:6000], 'files': [], 'context': [],
              'omitted': [], 'coverage': 'bounded_diff_and_related_source',
              'full_repository_review': False, 'lanes': {}}
    for slot in config['backends']['slots']:
        paths, reason = incremental(reader, state['lanes'].get(slot['id']), pr, config['config_id'], force_full)
        packet['lanes'][slot['id']] = {'paths': paths, 'reason': reason}
    items = []
    for page in range(1, 31):
        batch = reader.get(f'pulls/{number}/files?per_page=100&page={page}')
        items.extend(batch)
        if len(batch) < 100:
            break
    if len(items) != pr['changed_files']:
        packet['omitted'].append({'path': '<file-list>', 'reason': 'github_file_list_incomplete'})
    paths = [item['filename'] for item in items]
    packet['rules'] = selected_rules(config, paths)
    packet['expected_changed_files'] = pr['changed_files']
    # Diffs take priority over optional context. Never truncate a patch mid-hunk.
    changes = {}
    for item in items:
        path = item['filename']
        if not safe_path(path) or not item.get('patch'):
            packet['omitted'].append({'path': path[:500], 'reason': 'no_text_patch'})
            continue
        entry = {'path': path, 'status': item['status'], 'patch': item['patch'],
                 'head_text': None, 'base_text': None}
        if safe_path(item.get('previous_filename')):
            entry['previous_filename'] = item['previous_filename']
        if len(json.dumps(packet)) + len(json.dumps(entry)) > limits['packet_chars'] - 12000:
            packet['omitted'].append({'path': path[:500], 'reason': 'packet_budget'})
            continue
        packet['files'].append(entry)
        changes[path] = change_size(item)
    context_used = 0
    trees = {}
    overflow = False

    def source_tree(sha):
        if sha not in trees:
            try:
                trees[sha] = reader.get(f'git/trees/{sha}?recursive=1')
            except ReviewError:
                trees[sha] = {'tree': [], 'unavailable': True}
        return trees[sha]

    def omit(path, reason):
        """Record one omission while its metadata fits the packet budget."""
        nonlocal overflow
        record = {'path': path[:500], 'reason': reason}
        # Source text stops 12,000 characters short of packet_chars. Records use that
        # margin; the last 4,000 remain for aggregate markers and lane metadata.
        if overflow or len(json.dumps(packet)) + len(json.dumps(record)) + 2 > limits['packet_chars'] - 4000:
            overflow = True
        else:
            packet['omitted'].append(record)

    def add_text(target, key, path, sha):
        """Admit one immutable source text; return None or why it was not admitted."""
        nonlocal context_used
        available = min(limits['context_chars'] - context_used,
                        limits['packet_chars'] - 12000 - len(json.dumps(packet)))
        if available <= 2:
            return 'context_budget'
        try:
            tree = source_tree(sha)
            size = next((item.get('size') for item in tree.get('tree', []) if item['path'] == path), None)
            # JSON with ASCII escaping cannot be smaller than the UTF-8 source.
            # Tree sizes let us skip downloads that cannot fit the remaining budget.
            if type(size) is int and size + 2 > available:
                return 'context_budget'
            value = reader.text(path, sha, max_chars=available - 2)
        except ReviewError as error:
            return str(error) if str(error) in ('context_budget', 'context_api_budget') else 'context_unavailable'
        if value is None:
            return 'context_unavailable'
        cost = len(json.dumps(value))
        if context_used + cost > limits['context_chars'] or len(json.dumps(packet)) + cost > limits['packet_chars'] - 12000:
            return 'context_budget'
        target[key] = value
        context_used += cost
        return None

    def admit(path, reason):
        """Add one non-changed file to the context; return None or why it was omitted."""
        if len(packet['context']) >= limits['max_context_files']:
            return 'context_file_limit'
        entry = {'path': path, 'reason': reason}
        failure = add_text(entry, 'head_text', path, head)
        if not failure:
            packet['context'].append(entry)
        return failure

    try:
        comparison = reader.get(f'compare/{base}...{head}')
        merge_base = comparison['merge_base_commit']['sha']
    except (ReviewError, KeyError, TypeError):
        merge_base = None
        omit('<base-context>', 'merge_base_unavailable')
    # Larger changes usually carry the PR's core logic, so their source is admitted
    # first. The sort is stable for equal sizes; packet['files'] keeps the API order.
    ordered = sorted(packet['files'], key=lambda entry: -changes[entry['path']])
    for entry in ordered:
        if entry['status'] != 'removed' and add_text(entry, 'head_text', entry['path'], head):
            omit(entry['path'], 'head_text_unavailable_or_budget')
    candidates = []
    try:
        tree = source_tree(head)
        if tree.get('unavailable'):
            omit('<tree>', 'tree_unavailable')
        if tree.get('truncated'):
            omit('<tree>', 'tree_truncated')
        candidates = related_candidates(paths, packet['files'], tree.get('tree', []), config['context'])
    except ReviewError:
        omit('<related-context>', 'context_unavailable_or_budget')
    # Old findings remain evidence requests even when their file left the diff.
    prior_paths = [finding['path'] for finding in state['findings'].values() if finding['status'] != 'fixed' and finding['path'] not in paths]
    explicit = [(path, 'previous_finding') for path in prior_paths]
    explicit += [(path, reason) for path, reason in candidates if reason == 'configured_context']
    seen = set(paths)
    # Requested context precedes old versions and heuristic neighbors. Every
    # previous-finding miss is recorded; configured includes share the related bound.
    related_omitted = 0
    for path, reason in explicit:
        if path not in seen:
            seen.add(path)
            if failure := admit(path, reason):
                if reason == 'configured_context':
                    related_omitted += 1
                    if related_omitted > RELATED_OMISSION_RECORDS:
                        continue
                omit(path, failure)
    # Old versions of changed files follow current and explicitly requested source.
    for entry in ordered:
        if merge_base and entry['status'] != 'added':
            if add_text(entry, 'base_text', entry.get('previous_filename', entry['path']), merge_base):
                omit(entry['path'], 'base_text_unavailable_or_budget')
    for path, reason in candidates:
        if path in seen:
            continue
        seen.add(path)
        if failure := admit(path, reason):
            related_omitted += 1
            if related_omitted <= RELATED_OMISSION_RECORDS:
                omit(path, failure)
    if related_omitted > RELATED_OMISSION_RECORDS:
        omit('<related-context>', 'related_context_omitted_additional')
    if overflow:
        packet['omitted'].append({'path': '<omitted>', 'reason': 'omission_metadata_exceeds_budget'})
    for lane in packet['lanes'].values():
        if lane['paths'] is not None and lane['paths']:
            # A dependency-only update can affect an unchanged PR hunk.
            if any(path not in paths for path in lane['paths']):
                lane.update(paths=None, reason='related_context_changed_full_review')
    packet['context_reads'] = reader.reads
    current = api(repo, f'pulls/{number}')
    if identity(current) != (base, head) or current['state'] != pr['state'] or current.get('draft') != pr.get('draft'):
        raise ReviewError('pr_changed_during_collection')
    if len(json.dumps(packet)) > limits['packet_chars']:
        raise ReviewError('packet_metadata_exceeds_budget')
    packet['packet_id'] = digest(packet)
    return packet
