"""Pin reusable workflow internals to a preceding reviewed engine commit."""

import argparse
from pathlib import Path
import re
import subprocess
import tomllib


parser = argparse.ArgumentParser()
parser.add_argument('--engine')
parser.add_argument('--check', action='store_true')
args = parser.parse_args()
path = Path('.github/workflows/review.yml')
text = path.read_text()
pattern = r'reed-yang/independent-pr-review@[0-9a-f]{40}'


def undeclared_outputs(action):
    """Step outputs the workflow reads that the composite Action does not export."""
    block = re.search(r'^outputs:\n((?:  .*\n|\n)*)', action, re.M)
    declared = set(re.findall(r'^  ([A-Za-z_][\w-]*):', block[1], re.M)) if block else set()
    return sorted(set(re.findall(r'steps\.[\w-]+\.outputs\.([\w-]+)', text)) - declared)


if args.check:
    package = re.search(r'^__version__ = "([^"]+)"$', Path('independent_review/__init__.py').read_text(), re.M)
    project = tomllib.loads(Path('pyproject.toml').read_text())['project']['version']
    if not package or package[1] != project:
        raise SystemExit('independent_review.__version__ and the pyproject.toml version must match')
    pins = re.findall(pattern, text)
    if len(pins) != 4 or len(set(pins)) != 1 or pins[0].endswith('0' * 40):
        raise SystemExit('Reusable workflow must contain four identical non-placeholder SHA pins')
    if missing := undeclared_outputs(Path('action.yml').read_text()):
        raise SystemExit(f"action.yml does not export outputs the workflow reads: {', '.join(missing)}")
    print(f'Package version {project} matches; reusable workflow engine pins are immutable and consistent')
else:
    if not args.engine or not re.fullmatch(r'[0-9a-f]{40}', args.engine):
        raise SystemExit('A full engine SHA is required')
    action = subprocess.run(['git', 'show', args.engine + ':action.yml'], check=True, capture_output=True, text=True).stdout
    if missing := undeclared_outputs(action):
        raise SystemExit(f"The engine commit's action.yml does not export: {', '.join(missing)}")
    path.write_text(re.sub(pattern, 'reed-yang/independent-pr-review@' + args.engine, text))
    print('Updated reusable workflow engine pin')
