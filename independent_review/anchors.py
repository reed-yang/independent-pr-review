"""Map only demonstrable changed lines and keep finding identity title-independent."""

import hashlib
import json
import re


def changed_lines(patch):
    lines = []
    old = new = None
    for row in patch.splitlines():
        match = re.match(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', row)
        if match:
            old, new = map(int, match.groups())
        elif old is not None and row.startswith('+'):
            lines.append({'side': 'RIGHT', 'line': new, 'text': row[1:]})
            new += 1
        elif old is not None and row.startswith('-'):
            lines.append({'side': 'LEFT', 'line': old, 'text': row[1:]})
            old += 1
        elif old is not None and row.startswith(' '):
            old += 1
            new += 1
    return lines


def locate(entry, evidence, requested_line=None):
    pieces = [line.strip() for line in evidence.splitlines() if len(line.strip()) >= 8]
    matches = [line for line in changed_lines(entry['patch'])
               if any(piece in line['text'] or piece.lstrip('+-') == line['text'].strip() for piece in pieces)]
    requested = [line for line in matches if line['line'] == requested_line and line['side'] == 'RIGHT']
    selected = requested if len(requested) == 1 else matches
    if len(selected) != 1:
        return None
    return {key: selected[0][key] for key in ('line', 'side')}


def fingerprint(path, evidence, scope=''):
    encoded = json.dumps([path, scope, ''.join(evidence.split())], ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


def scope_at(entry, anchor):
    if not anchor or anchor['side'] != 'RIGHT' or not entry.get('head_text'):
        return ''
    scope = ''
    for line in entry['head_text'].splitlines()[:anchor['line']]:
        match = re.match(r'\s*(?:async\s+)?(?:def|class|function)\s+([\w$]+)|\s*(?:export\s+)?(?:const|let)\s+([\w$]+)\s*=.*=>', line)
        if match:
            scope = next(value for value in match.groups() if value)
    return scope


def bind(finding, entry):
    result = dict(finding)
    anchor = locate(entry, finding['evidence'], finding.get('line'))
    result['anchor'] = anchor
    result['scope'] = scope_at(entry, anchor)
    result['finding_id'] = fingerprint(entry.get('previous_filename') or finding['path'],
                                       finding['evidence'], result['scope'])
    result['category'] = finding.get('category', 'correctness')
    result['line'] = anchor['line'] if anchor else None
    return result
