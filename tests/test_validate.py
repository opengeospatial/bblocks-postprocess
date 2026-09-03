"""Regression test for ogc.bblocks.validate._mime_type_for_extension.

Guards against a bug where validate_transform_output() / validate_test_resources()
passed the raw dict returned by ogc.bblocks.mimetypes.from_extension() straight
through as `file_format`, instead of unwrapping its 'mimeType' string. That dict
eventually reached NATIVE_RDF_LANGS.get(file_format, ...) in
ogc.bblocks.validation.rdf.RdfValidator._load_graph, which raised
"TypeError: unhashable type: 'dict'" for any RDF-suffixed transform output
(.ttl/.jsonld/.rdf) -- see the reported traceback in validate_transform_output.
"""
from ogc.bblocks.validate import _mime_type_for_extension


def test_mime_type_for_extension_returns_string_not_dict():
    mime_type = _mime_type_for_extension('.ttl')
    assert mime_type == 'text/turtle'
    assert isinstance(mime_type, str)


def test_mime_type_for_extension_accepts_suffix_with_or_without_dot():
    assert _mime_type_for_extension('.ttl') == _mime_type_for_extension('ttl')


def test_mime_type_for_extension_unknown_suffix():
    assert _mime_type_for_extension('.not-a-real-extension') is None


def test_mime_type_for_extension_empty_suffix():
    assert _mime_type_for_extension('') is None
