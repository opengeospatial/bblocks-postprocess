#!/usr/bin/env python3
import json
from typing import AnyStr

from ogc.na.util import load_yaml
from rdflib import Graph

from ogc.bblocks.models import TransformMetadata, Transformer
from ogc.na import ingest_json

transform_type = 'semantic-uplift'

default_inputs = [
    'application/json',
]

default_outputs = [
    'text/turtle',
    'rdf/xml',
]

class SemanticUpliftTransformer(Transformer):

    def __init__(self):
        super().__init__([transform_type], default_inputs, default_outputs)

    def do_transform(self, metadata: TransformMetadata) -> AnyStr | None:
        uplift_def = load_yaml(content=metadata.transform_content)
        uplifted = json.dumps(ingest_json.uplift_json(json.loads(metadata.input_data), uplift_def))
        data_graph = Graph().parse(data=uplifted, format='json-ld')
        return data_graph.serialize(format=metadata.target_mime_type)
