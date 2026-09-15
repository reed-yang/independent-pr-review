"""Load the pinned engine independently of the consumer working directory."""

from pathlib import Path
import runpy
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
module = sys.argv.pop(1)
if module not in ('cli', 'agy_auth'):
    raise SystemExit('Unsupported Action module')
runpy.run_module('independent_review.' + module, run_name='__main__')
