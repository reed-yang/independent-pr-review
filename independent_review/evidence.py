"""Match literal source, including contiguous source sides of unified hunks."""

import hashlib
import json
import os
import re


def diff_sources(patch):
    old, new = [], []
    active = False
    for row in patch.splitlines():
        if re.match(r'^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@', row):
            if active:
                yield 'diff_base', '\n'.join(old)
                yield 'diff_head', '\n'.join(new)
            old, new, active = [], [], True
        elif active and row.startswith((' ', '+', '-')):
            if row[0] in ' -':
                old.append(row[1:])
            if row[0] in ' +':
                new.append(row[1:])
        elif active and not row.startswith('\\ No newline'):
            yield 'diff_base', '\n'.join(old)
            yield 'diff_head', '\n'.join(new)
            old, new, active = [], [], False
    if active:
        yield 'diff_base', '\n'.join(old)
        yield 'diff_head', '\n'.join(new)


def match_evidence(entry, evidence, *, allow_base=False, current_only=False):
    if not isinstance(evidence, str) or not 8 <= len(evidence.strip()) <= 4000:
        return None
    sources = [('head_text', entry.get('head_text') or '')]
    if not current_only:
        sources.append(('patch', entry.get('patch') or ''))
        if allow_base:
            sources.append(('base_text', entry.get('base_text') or ''))
    sources.extend((kind, text) for kind, text in diff_sources(entry.get('patch') or '')
                   if not current_only or kind == 'diff_head')
    return next((kind for kind, text in sources if evidence in text), None)


def redact(text):
    values = [os.environ.get(name, '') for name in
              ('GROK_API_KEY', 'GEMINI_API_KEY', 'AGY_OAUTH_JSON', 'REVIEW_STATE_KEY', 'GH_TOKEN')]
    try:
        document = json.loads(os.environ.get('AGY_OAUTH_JSON', '{}'))
        values.extend(document.get('token', {}).get(key, '') for key in ('access_token', 'refresh_token'))
        values.append(document.get('id_token', ''))
    except (ValueError, AttributeError, TypeError):
        pass
    for value in values:
        if isinstance(value, str) and len(value) >= 8:
            text = text.replace(value, '[REDACTED]')
    return re.sub(r'(?i)(?:sk-[a-z0-9_-]{16,}|gh[pousr]_[a-z0-9_]{16,}|github_pat_[a-z0-9_]{16,}|ya29\.[a-z0-9._-]+|1//[a-z0-9_-]{16,})', '[REDACTED]', text)


def rejection(index, finding, code, files):
    finding = finding if isinstance(finding, dict) else {}
    evidence = finding.get('evidence')
    evidence = evidence if isinstance(evidence, str) else ''
    path = finding.get('path')
    return {'index': index, 'error': code,
            'path': path if isinstance(path, str) and path in files else None,
            'evidence_chars': len(evidence),
            'evidence_sha256': hashlib.sha256(evidence.encode()).hexdigest(),
            'evidence_preview': redact(evidence)[:1000]}
