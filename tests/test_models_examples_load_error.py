"""Regression test for issue #48: a broken/unreachable 'ref' in examples.yaml used to
raise unhandled out of BuildingBlock._load_examples(), which either aborted the whole
postprocess run (fail_on_error=True) or silently dropped the entire building block
(fail_on_error=False) instead of just that one example.

_load_examples() should now catch the load failure, keep the block/example around,
and flag the offending snippet with a 'load_error' so validate.py can turn it into a
scoped validation error instead of crashing.
"""
from pathlib import Path

import yaml

from ogc.bblocks.models import BuildingBlock


def _bblock_stub(files_path: Path) -> BuildingBlock:
    # Bypass BuildingBlock.__init__ (which requires a full bblock.json) and set up
    # only what _load_examples() reads/writes.
    bblock = object.__new__(BuildingBlock)
    bblock.identifier = 'test.broken-example-ref'
    bblock.files_path = files_path
    bblock.examples_file = files_path / 'examples.yaml'
    return bblock


def test_load_examples_flags_missing_ref_instead_of_raising(tmp_path):
    (tmp_path / 'examples.yaml').write_text(yaml.safe_dump({
        'examples': [{
            'title': 'Broken example',
            'snippets': [{'language': 'json', 'ref': 'does-not-exist.json'}],
        }],
    }))

    bblock = _bblock_stub(tmp_path)
    bblock._load_examples()

    snippet = bblock.examples[0]['snippets'][0]
    assert snippet['code'] is None
    assert 'load_error' in snippet and snippet['load_error']


def test_load_examples_leaves_valid_snippets_untouched(tmp_path):
    (tmp_path / 'valid.json').write_text('{"a": 1}')
    (tmp_path / 'examples.yaml').write_text(yaml.safe_dump({
        'examples': [{
            'title': 'Working example',
            'snippets': [{'language': 'json', 'ref': 'valid.json'}],
        }],
    }))

    bblock = _bblock_stub(tmp_path)
    bblock._load_examples()

    snippet = bblock.examples[0]['snippets'][0]
    assert snippet['code'] == '{"a": 1}'
    assert 'load_error' not in snippet
