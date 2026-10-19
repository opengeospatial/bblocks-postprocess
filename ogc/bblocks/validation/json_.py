import json
import logging
import os
import sys
import traceback

logger = logging.getLogger(__name__)
from json import JSONDecodeError
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urljoin
from urllib.request import urlopen

import jsonref
import jsonschema
import requests
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT3, DRAFT4, DRAFT202012, specification_with
from ogc.bblocks.util import load_yaml, is_url, PathOrUrl
from yaml import MarkedYAMLError

from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister
from ogc.bblocks.validation import Validator, ValidationItemSourceType, ValidationReportSection, ValidationReportEntry, \
    ValidationReportItem


def get_json_validator(contents, base_uri, bblocks_register: BuildingBlockRegister | None) -> Validator:
    if isinstance(contents, dict):
        schema = contents
    else:
        schema = load_yaml(content=contents)
    validator_cls = validator_for(schema)
    validator_cls.check_schema(schema)

    dialect = schema.get('$schema') if isinstance(schema, dict) else None
    spec = specification_with(dialect, default=DRAFT202012) if isinstance(dialect, str) else DRAFT202012
    if base_uri and isinstance(schema, dict) and spec.id_of(schema) is None:
        # The validator resolves relative references against the root schema's own id, so give it
        # one if it doesn't declare any (shallow copy: the original is left untouched)
        id_key = 'id' if spec in (DRAFT3, DRAFT4) else '$id'
        schema = {**schema, id_key: base_uri}

    registry = Registry(retrieve=_make_retriever(bblocks_register, spec))
    return validator_cls(schema, registry=registry)


def validate_json(instance: Any, validator: Validator):
    error = jsonschema.exceptions.best_match(validator.iter_errors(instance))
    if error is not None:
        raise error


def _make_retriever(bblocks_register: BuildingBlockRegister | None, default_spec):
    """
    Creates a ``retrieve`` callback for a ``referencing.Registry``. Schemas belonging to building blocks
    of the register being processed are served directly from their annotated contents (they might not
    have been committed or published yet); anything else is fetched, and parsed as YAML or JSON.
    """

    def retrieve(uri: str) -> Resource:
        if bblocks_register and uri in bblocks_register.local_bblock_files:
            bblock_id = bblocks_register.local_bblock_files[uri]
            bblock = bblocks_register.bblocks[bblock_id]
            contents = load_yaml(content=bblock.annotated_schema_contents)
        elif urlsplit(uri).scheme in ('http', 'https'):
            contents = load_yaml(content=requests.get(uri).content)
        else:
            # Otherwise, pass off to urllib and assume utf-8
            with urlopen(uri) as url:
                contents = load_yaml(content=url.read().decode('utf-8'))
        return Resource.from_contents(contents, default_specification=default_spec)

    return retrieve


class JsonValidator(Validator):

    def __init__(self, bblock: BuildingBlock, register: BuildingBlockRegister):
        super().__init__(bblock, register)

        self.schema_error = None
        self.schema_validator = None

        try:
            if bblock.annotated_schema and bblock.annotated_schema.is_file():
                schema = bblock.annotated_schema
                schema_uri = (schema.as_uri() if isinstance(schema, PathOrUrl)
                              else schema.resolve().as_uri())
                self.schema_validator = get_json_validator(bblock.annotated_schema_contents,
                                                           schema_uri,
                                                           register)
        except Exception as e:
            self.schema_error = f"Error creating JSON validator: {type(e).__name__}: {e}"

    def validate(self, filename: Path, output_filename: Path, report: ValidationReportItem,
                 contents: str | None = None,
                 schema_ref: str | None = None,
                 file_format: str | None = None,
                 resource_url: str | None = None,
                 **kwargs) -> bool | None:

        if filename.suffix not in ('.json', '.jsonld', '.yaml', '.yml', '.geojson')\
                and file_format not in ('application/json', 'application/x-yaml', 'application/geo+json'):
            return False

        if report.source.type == ValidationItemSourceType.EXAMPLE:
            file_from = 'examples'
        elif report.source.type == ValidationItemSourceType.TRANSFORM_OUTPUT:
            file_from = 'transform outputs'
        else:
            file_from = 'test resources'

        try:
            if contents is not None:
                if filename.suffix.startswith('.json'):
                    json_doc = json.loads(contents)
                else:
                    json_doc = load_yaml(content=contents)
                if resource_url:
                    using_fn = resource_url
                elif filename.name == output_filename.name:
                    using_fn = filename.name
                else:
                    using_fn = f"{output_filename.name} ({filename.stem})"
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.FILES,
                    message=f'Using {using_fn} from {file_from}',
                ))
            else:
                json_doc = load_yaml(filename=filename)
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.FILES,
                    message=f'Using {resource_url or filename.name} from {file_from}',
                ))
            # json_doc = jsonref.replace_refs(json_doc, base_uri=filename.as_uri(), merge_props=True, proxies=False)

            graph_doc = json_doc['@graph'] if isinstance(json_doc, dict) and '@graph' in json_doc else None

            schema_validator = self.schema_validator

            if schema_ref:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.JSON_SCHEMA,
                    message=f"Using the following JSON Schema: {schema_ref}",
                    payload={
                        'filename': schema_ref,
                    }
                ))
                try:
                    annotated_schema = self.bblock.annotated_schema
                    annotated_uri = (annotated_schema.as_uri() if isinstance(annotated_schema, PathOrUrl)
                                     else annotated_schema.resolve().as_uri())
                    # Relative references (including bare "#fragment"s) are resolved against the
                    # annotated schema, so the snippet's $ref is always absolute and needs no base URI
                    schema_ref = urljoin(annotated_uri, schema_ref)
                    schema_validator = get_json_validator({'$ref': schema_ref}, None, self.register)
                except Exception as e:
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.JSON_SCHEMA,
                        message=f"Error loading schema from snippet schema-ref: {e.__class__.__qualname__}: {e}",
                        is_error=True,
                        is_global=False,
                    ))
                    return

            if self.schema_error:
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.JSON_SCHEMA,
                    message=self.schema_error,
                    is_error=True,
                    is_global=True,
                ))
                return

            if schema_validator:
                def report_validation_error(e: Exception) -> None:
                    if not isinstance(e, jsonschema.exceptions.ValidationError):
                        logger.error("Unexpected error encountered when validating resource %s for %s",
                                     os.path.relpath(filename), self.bblock.identifier, exc_info=e)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.JSON_SCHEMA,
                        message=f"{type(e).__name__}: {e}",
                        is_error=True,
                        payload={
                            'op': 'validation',
                            'result': False,
                            'exception': e.__class__.__qualname__,
                            'errorMessage': getattr(e, 'message', str(e)),
                        }
                    ))

                if graph_doc is not None:
                    # The document has a top-level "@graph": try validating with it unwrapped first
                    # (the common case, where "@graph" is just a convenience wrapper for a batch of
                    # instances), and only fall back to the full, wrapped document -- e.g. for schemas
                    # where "@context"/"@graph" are themselves part of the required shape -- if that fails.
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.FILES,
                        message='"@graph" found, trying validation with contents unwrapped',
                        payload={
                            'op': '@graph-unwrap'
                        }
                    ))
                    try:
                        validate_json(graph_doc, schema_validator)
                        json_doc = graph_doc
                        report.add_entry(ValidationReportEntry(
                            section=ValidationReportSection.JSON_SCHEMA,
                            message='Validation passed',
                            payload={
                                'op': 'validation',
                                'result': True,
                            }
                        ))
                    except Exception as graph_error:
                        try:
                            validate_json(json_doc, schema_validator)
                        except Exception:
                            # Both the unwrapped and the full document failed validation: report the
                            # original (unwrapped) error, since that's the assumption we validate against
                            # by default.
                            report_validation_error(graph_error)
                        else:
                            # The full, wrapped document validates on its own -- e.g. a schema where
                            # "@graph" is a required envelope field rather than a batch-of-instances
                            # wrapper. Keep json_doc as-is (wrapped) and report a pass.
                            report.add_entry(ValidationReportEntry(
                                section=ValidationReportSection.FILES,
                                message='Validation of unwrapped "@graph" contents failed; '
                                        'validated successfully as a full object instead',
                                payload={
                                    'op': '@graph-unwrap-fallback'
                                }
                            ))
                            report.add_entry(ValidationReportEntry(
                                section=ValidationReportSection.JSON_SCHEMA,
                                message='Validation passed',
                                payload={
                                    'op': 'validation',
                                    'result': True,
                                }
                            ))
                else:
                    try:
                        validate_json(json_doc, schema_validator)
                        report.add_entry(ValidationReportEntry(
                            section=ValidationReportSection.JSON_SCHEMA,
                            message='Validation passed',
                            payload={
                                'op': 'validation',
                                'result': True,
                            }
                        ))
                    except Exception as e:
                        report_validation_error(e)

            if contents is not None:
                # This is an example or a ref, write it to disk
                with open(output_filename, 'w') as f:
                    json.dump(json_doc, f, indent=2)

        except MarkedYAMLError as e:
            report.add_entry(ValidationReportEntry(
                section=ValidationReportSection.JSON_SCHEMA,
                message=f"Error parsing YAML example: {str(e)} "
                        f"on or near line {e.context_mark.line + 1} "
                        f"column {e.context_mark.column + 1}",
                is_error=True,
                payload={
                    'exception': e.__class__.__qualname__,
                    'line': e.context_mark.line + 1,
                    'col': e.context_mark.column + 1,
                }
            ))
        except JSONDecodeError as e:
            report.add_entry(ValidationReportEntry(
                section=ValidationReportSection.JSON_SCHEMA,
                message=f"Error parsing JSON example: {str(e)} "
                        f"on or near line {e.lineno} "
                        f"column {e.colno}",
                is_error=True,
                payload={
                    'exception': e.__class__.__qualname__,
                    'line': e.lineno,
                    'col': e.colno,
                }
            ))
