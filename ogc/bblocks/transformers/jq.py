#!/usr/bin/env python3
import json
from typing import AnyStr

from ogc.bblocks.util import TransformMetadata
import jq

transform_type = 'jq'

source_mime_types = [
    'application/json',
]

target_mime_types = [
    'application/json',
]


def transform(metadata: TransformMetadata) -> AnyStr:
    transformed = jq.compile(metadata.transform_content).input_text(metadata.input_data).text()
    return json.dumps(json.loads(transformed), indent=2)
