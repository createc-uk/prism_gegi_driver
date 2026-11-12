#!/usr/bin/env python3
"""Run offline unit tests; require native bindings only with --require-prism."""
from pathlib import Path
import sys
import types
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src/phds_gegi_driver'))
args = sys.argv[1:]
require_prism = '--require-prism' in args
if require_prism:
    args.remove('--require-prism')
try:
    import prism
except ModuleNotFoundError as error:
    # Do not hide broken transitive dependencies of an installed Prism package.
    if error.name != 'prism' or require_prism:
        raise
    # Processing unit tests import node modules but never construct live nodes.
    # Fail on API access rather than supplying fake transport behaviour.
    prism = types.ModuleType('prism')
    prism.__file__ = None
    def unavailable(name):
        raise RuntimeError('This offline unit test attempted to use prism.' + name +
                           '; install native Prism bindings to test transport')
    prism.__getattr__ = unavailable
    sys.modules['prism'] = prism
    print('Offline unit tests: native Prism is unavailable; transport API access is disabled.', flush=True)
else:
    print('Unit tests: native Prism bindings available.', flush=True)

unittest.main(module=None, argv=[sys.argv[0], 'discover', '-s', str(ROOT / 'test'),
                                '-p', 'test_*.py', *args])
