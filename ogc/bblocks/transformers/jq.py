#!/usr/bin/env python3
import json
from typing import AnyStr

from ogc.bblocks.models import TransformMetadata, Transformer
import jq

transform_type = 'jq'

default_inputs = [
    'application/json',
]

default_outputs = [
    'application/json',
]

class JqTransformer(Transformer):

    def __init__(self):
        super().__init__('jq', default_inputs, default_outputs)

    def do_transform(self, metadata: TransformMetadata) -> AnyStr | None:
        transformed = jq.compile(metadata.transform_content).input_text(metadata.input_data).text()
        return json.dumps(json.loads(transformed), indent=2)
