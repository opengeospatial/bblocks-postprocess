"""Regression test for ogc.bblocks.models.ImportedBBlockProxy.validation_resources.

Guards against issue #79: validate_transform_output() calls
`profile_bblock.validation_resources` for a transform's output profile, but when
that profile lives in an imported (external) register, `profile_bblock` is an
ImportedBBlockProxy rather than a BuildingBlock -- and the proxy didn't define
validation_resources, raising `AttributeError: 'ImportedBBlockProxy' object has
no attribute 'validation_resources'`.
"""
from ogc.bblocks.models import ImportedBBlockProxy


def test_validation_resources_filters_by_role():
    proxy = ImportedBBlockProxy({
        'itemIdentifier': 'ogc.example.profile',
        'resources': [
            {'role': 'data', 'ref': 'https://example.com/data.ttl', 'format': 'text/turtle'},
            {'role': 'validation', 'ref': 'https://example.com/shapes.ttl', 'format': 'text/turtle',
             'conformsTo': 'https://example.com/spec'},
        ],
    })

    resources = proxy.validation_resources

    assert resources == [{
        'ref': 'https://example.com/shapes.ttl',
        'format': 'text/turtle',
        'conformsTo': 'https://example.com/spec',
    }]


def test_validation_resources_empty_when_no_resources():
    proxy = ImportedBBlockProxy({'itemIdentifier': 'ogc.example.profile'})
    assert proxy.validation_resources == []


def test_validation_resources_skips_entries_without_ref():
    proxy = ImportedBBlockProxy({
        'itemIdentifier': 'ogc.example.profile',
        'resources': [{'role': 'validation', 'format': 'text/turtle'}],
    })
    assert proxy.validation_resources == []
