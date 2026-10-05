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
    return (next((kind for kind, text in sources if evidence in text), None)
            or next((kind for kind, text in sources if reindented_in(evidence, text)), None))


def reindented_in(evidence, text):
    """Match a quote whose lines differ from the source only in leading and trailing whitespace.

    Reviewers often re-indent a quote that starts mid-line. The first quoted line
    must end a source line, the last must start one and every line between must
    equal a whole source line, so no content is relaxed.
    """
    quote = [line.strip() for line in evidence.strip('\n').split('\n')]
    if len(quote) < 2:
        return quote[0] in text
    lines = [line.strip() for line in text.split('\n')]
    last = len(quote) - 1
    return any(lines[start].endswith(quote[0]) and lines[start + last].startswith(quote[last])
               and lines[start + 1:start + last] == quote[1:last]
               for start in range(len(lines) - last))


def redact(text):
    values = [os.environ.get(name, '') for name in
              ('GROK_API_KEY', 'GPT_API_KEY', 'GEMINI_API_KEY', 'AGY_OAUTH_JSON', 'REVIEW_STATE_KEY', 'GH_TOKEN')]
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
