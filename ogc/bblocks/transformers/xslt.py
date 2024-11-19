#!/usr/bin/env python3
import json
from typing import AnyStr

from ogc.bblocks.models import TransformMetadata, Transformer
from lxml import etree

transform_type = 'xslt'

default_inputs = [
    'text/xml',
]

default_outputs = [
    'text/xml',
]

class XmlTransformer(Transformer):

    def __init__(self):
        super().__init__(['xslt'], default_inputs, default_outputs)

    def do_transform(self, metadata: TransformMetadata) -> AnyStr | None:
        transform = etree.XSLT(etree.XML(metadata.transform_content))
        result = transform(etree.XML(metadata.input_data))
        return etree.to_string(result, pretty=True)
