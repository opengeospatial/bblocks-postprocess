#!/usr/bin/env python3
import json
from typing import AnyStr

from ogc.bblocks.models import TransformMetadata, Transformer
from lxml import etree

transform_type = 'xslt'

default_inputs = [
    'application/xml',
]

default_outputs = [
    'application/xml',
]

class XmlTransformer(Transformer):

    def __init__(self):
        super().__init__(['xslt'], default_inputs, default_outputs)

    def do_transform(self, metadata: TransformMetadata) -> AnyStr | None:
        transform = etree.XSLT(etree.XML(metadata.transform_content.encode('utf-8')))
        result = transform(etree.XML(metadata.input_data.encode('utf-8')))
        return etree.tostring(result, encoding='utf-8', pretty_print=True, xml_declaration=True).decode('utf-8')
