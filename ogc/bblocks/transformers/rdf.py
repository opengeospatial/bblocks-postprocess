#!/usr/bin/env python3
import logging
from io import UnsupportedOperation
from sys import prefix
from typing import AnyStr

from ogc.bblocks.models import TransformMetadata, Transformer
from rdflib import Graph
import pyshacl

transform_types = ['shacl-af-rule', 'sparql-update', 'sparql-construct']

media_types = {
    'text/turtle': 'ttl',
    'application/rdf+xml': 'xml',
}
media_types_keys = list(media_types.keys())

rdflib_logger = logging.getLogger('rdflib')


def add_prefixes_ttl(data: str, prefixes: dict[str, str]):
    return '\n'.join(f"@prefix {k}: <{v}> ." for k, v in prefixes.items()) + '\n' + data


def add_prefixes_xml(data: str, prefixes: dict[str, str]):
    # TODO: Not supported yet
    return data


class RdfTransformer(Transformer):

    def __init__(self):
        super().__init__(transform_types, media_types_keys, media_types_keys)

    def do_transform(self, metadata: TransformMetadata) -> AnyStr | None:
        rdflib_level = rdflib_logger.level
        try:
            rdflib_logger.setLevel(logging.ERROR)

            media_type = media_types.get(metadata.source_mime_type)
            if not media_type:
                raise ValueError(f'Unsupported media type for rdf validator: {metadata.source_mime_type}')

            prefixes = metadata.metadata.get('_prefixes') if metadata.metadata else None
            if prefixes:
                input_data = globals()[f"add_prefixes_{media_type}"](metadata.input_data, prefixes)
            else:
                input_data = metadata.input_data

            data_graph = Graph().parse(data=input_data, format=media_type)

            if metadata.type == 'shacl-af-rule':
                shacl_graph = Graph().parse(data=metadata.transform_content)
                pyshacl.validate(data_graph=data_graph, shacl_graph=shacl_graph, advanced=True, inplace=True)
            elif metadata.type == 'sparql-update':
                data_graph.update(metadata.transform_content)
            elif metadata.type == 'sparql-construct':
                data_graph = data_graph.query(metadata.transform_content).graph
            else:
                raise ValueError(f'Unsupported transform type: {metadata.type}')
        finally:
            rdflib_logger.setLevel(rdflib_level)
        return data_graph.serialize(format=metadata.target_mime_type)
