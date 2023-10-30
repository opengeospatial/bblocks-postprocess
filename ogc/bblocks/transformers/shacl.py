#!/usr/bin/env python3
import logging
from typing import AnyStr

from ogc.bblocks.util import TransformMetadata
from rdflib import Graph
import pyshacl

transform_type = 'shacl'

source_mime_types = [
    'text/turtle',
    'application/rdf+xml',
    'application/ld+json',
]

target_mime_types = [
    'text/turtle',
    'application/rdf+xml',
    'application/ld+json',
]

rdflib_logger = logging.getLogger('rdflib')


def transform(metadata: TransformMetadata) -> AnyStr:
    rdflib_level = rdflib_logger.level
    try:
        rdflib_logger.setLevel(logging.ERROR)
        data_graph = Graph().parse(data=metadata.input_data, format=metadata.source_mime_type)
        shacl_graph = Graph().parse(data=metadata.transform_content)
        pyshacl.validate(data_graph=data_graph, shacl_graph=shacl_graph, advanced=True, inplace=True)
    finally:
        rdflib_logger.setLevel(rdflib_level)
    return data_graph.serialize(format=metadata.target_mime_type)
