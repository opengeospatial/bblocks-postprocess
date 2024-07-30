from typing import Any

import jq
import pyshacl
from rdflib import Graph

from ogc.bblocks.models import BuildingBlock
from ogc.bblocks.util import load_file, PathOrUrl
from ogc.bblocks.validation import ValidationReportItem, ValidationReportEntry, ValidationReportSection


class Uplifter:

    def __init__(self, bblock: BuildingBlock):
        self.bblock = bblock
        self.bblock_files = PathOrUrl(bblock.files_path)

    def _run_steps(self, stage: str, report: ValidationReportItem, input_data: Any, *args):
        if self.bblock.semanticUplift and self.bblock.semanticUplift.get('additionalSteps'):
            for idx, step in enumerate(self.bblock.semanticUplift['additionalSteps']):
                func_name = f"_{stage}_{step['type'].replace('-', '_')}"
                if hasattr(self, func_name):
                    code = step.get('code')
                    report_source = 'inline code'
                    if not code:
                        if step.get('ref'):
                            code = load_file(self.bblock_files.resolve_ref(step['ref']), self.bblock.remote_cache_dir)
                            report_source = step['ref']
                        else:
                            raise ValueError(
                                f'No code or ref found for semanticUplift step {idx} in {self.bblock.identifier}')
                    step['stage'] = stage
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.SEMANTIC_UPLIFT,
                        message=f"Running {stage}-uplift {step['type']} transform step from {report_source}",
                    ))
                    input_data = getattr(self, func_name)(code, input_data, *args)
        return input_data

    def pre_uplift(self, report: ValidationReportItem, json_doc: dict | list):
        return self._run_steps('pre', report, json_doc)

    def post_uplift(self, report: ValidationReportItem, g: Graph):
        return self._run_steps('post', report, g)

    @staticmethod
    def _pre_jq(code: str, json_doc: dict | list):
        return jq.compile(code).input_value(json_doc).first()

    @staticmethod
    def _post_sparql_construct(code: str, g: Graph):
        result = g.query(code)
        if result.type != 'CONSTRUCT':
            raise ValueError('SPARQL query is not of type CONSTRUCT')
        return result.graph

    @staticmethod
    def _post_sparql_update(code: str, g: Graph):
        g.update(code)
        return g

    @staticmethod
    def _post_shacl(code: str, g: Graph):
        shacl_graph = Graph().parse(data=code)
        pyshacl.validate(data_graph=g, shacl_graph=shacl_graph, inplace=True, advanced=True)
        return g
