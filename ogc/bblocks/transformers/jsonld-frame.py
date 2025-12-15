import json
from typing import AnyStr

from ogc.na.util import load_yaml
from pyld import jsonld
from rdflib import Graph

from ogc.bblocks.models import TransformMetadata, Transformer
from ogc.na import ingest_json

transform_type = 'jsonld-frame'

default_inputs = [
    'text/turtle',
    'rdf/xml',
    'application/ld+json',
]

default_outputs = [
    'application/ld+json',
]

class JsonLdFrameTransformer(Transformer):

    def __init__(self):
        super().__init__([transform_type], default_inputs, default_outputs)

    def do_transform(self, metadata: TransformMetadata) -> AnyStr | None:
        frame = json.loads(metadata.transform_content)

        if metadata.source_mime_type == 'application/ld+json':
            input_doc = json.loads(metadata.input_data)
        else:
            data_graph = Graph().parse(data=metadata.input_data, format=metadata.source_mime_type)
            input_doc = json.loads(data_graph.serialize(format='json-ld'))

        framed = jsonld.frame(input_doc, frame)

        if metadata.target_mime_type == 'application/json':
            if '@graph' in framed:
                framed = framed['@graph']
            else:
                framed.pop('@context', None)

        return json.dumps(framed, indent=2)
