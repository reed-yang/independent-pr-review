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
if args.check:
    package = re.search(r'^__version__ = "([^"]+)"$', Path('independent_review/__init__.py').read_text(), re.M)
    project = tomllib.loads(Path('pyproject.toml').read_text())['project']['version']
    if not package or package[1] != project:
        raise SystemExit('independent_review.__version__ and the pyproject.toml version must match')
    pins = re.findall(pattern, text)
    if len(pins) != 4 or len(set(pins)) != 1 or pins[0].endswith('0' * 40):
        raise SystemExit('Reusable workflow must contain four identical non-placeholder SHA pins')
    print(f'Package version {project} matches; reusable workflow engine pins are immutable and consistent')
else:
    if not args.engine or not re.fullmatch(r'[0-9a-f]{40}', args.engine):
        raise SystemExit('A full engine SHA is required')
    subprocess.run(['git', 'cat-file', '-e', args.engine + ':action.yml'], check=True)
    path.write_text(re.sub(pattern, 'reed-yang/independent-pr-review@' + args.engine, text))
    print('Updated reusable workflow engine pin')
