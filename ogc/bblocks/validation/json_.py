import json
import random
import traceback
from json import JSONDecodeError
from pathlib import Path
from time import time
from typing import Any
from urllib.parse import urlsplit, urljoin
from urllib.request import urlopen

import jsonref
import jsonschema
import requests
from jsonschema.validators import validator_for
from ogc.na.util import load_yaml, is_url
from yaml import MarkedYAMLError

from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister
from ogc.bblocks.validation import Validator, ValidationItemSourceType, ValidationReportSection, ValidationReportEntry, \
    ValidationReportItem


def get_json_validator(contents, base_uri, bblocks_register: BuildingBlockRegister) -> jsonschema.Validator:
    if isinstance(contents, dict):
        schema = contents
    else:
        schema = load_yaml(content=contents)
    resolver = RefResolver(
        base_uri=base_uri,
        referrer=schema,
        bblocks_register=bblocks_register,
    )
    validator_cls = validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema, resolver=resolver)


def validate_json(instance: Any, validator: jsonschema.Validator):
    error = jsonschema.exceptions.best_match(validator.iter_errors(instance))
    if error is not None:
        raise error


class RefResolver(jsonschema.validators.RefResolver):

    def __init__(self, bblocks_register: BuildingBlockRegister, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.bblocks_register = bblocks_register

    def resolve_remote(self, uri):
        if uri in self.bblocks_register.local_bblock_files:
            bblock_id = self.bblocks_register.local_bblock_files[uri]
            bblock = self.bblocks_register.bblocks[bblock_id]
            return load_yaml(content=bblock.annotated_schema_contents)

        scheme = urlsplit(uri).scheme

        if scheme in self.handlers:
            result = self.handlers[scheme](uri)
        elif scheme in ["http", "https"]:
            result = load_yaml(content=requests.get(uri).content)
        else:
            # Otherwise, pass off to urllib and assume utf-8
            with urlopen(uri) as url:
                result = load_yaml(content=url.read().decode("utf-8"))

        if self.cache_remote:
            self.store[uri] = result
        return result


class JsonValidator(Validator):

    def __init__(self, bblock: BuildingBlock, register: BuildingBlockRegister):
        super().__init__(bblock, register)

        self.schema_error = None
        self.schema_validator = None

        try:
            if bblock.annotated_schema.is_file():
                self.schema_validator = get_json_validator(bblock.annotated_schema_contents,
                                                           bblock.annotated_schema.resolve().as_uri(),
                                                           register)
        except Exception as e:
            self.schema_error = f"Error creating JSON validator: {type(e).__name__}: {e}"

    def validate(self, filename: Path, output_filename: Path, report: ValidationReportItem,
                 contents: str | None = None,
                 schema_ref: str | None = None,
                 file_format: str | None = None,
                 **kwargs) -> bool | None:

        if filename.suffix not in ('.json', '.jsonld', '.yaml', '.yml')\
                and file_format not in ('application/json', 'application/x-yaml'):
            return False

        file_from = 'examples' if report.source.type == ValidationItemSourceType.EXAMPLE else 'test resources'

        try:
            if contents:
                if filename.suffix.startswith('.json'):
                    json_doc = json.loads(contents)
                else:
                    json_doc = load_yaml(content=contents)
                if filename.name == output_filename.name:
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
                    message=f'Using {filename.name} from {file_from}',
                ))
            json_doc = jsonref.replace_refs(json_doc, base_uri=filename.as_uri(), merge_props=True, proxies=False)

            if '@graph' in json_doc:
                json_doc = json_doc['@graph']
                report.add_entry(ValidationReportEntry(
                    section=ValidationReportSection.FILES,
                    message='"@graph" found, unwrapping',
                    payload={
                        'op': '@graph-unwrap'
                    }
                ))

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
                    random_fn = f"example.{time()}.{random.randint(0, 1000)}.yaml"
                    if self.bblock.schema.is_path:
                        schema_uri = self.bblock.schema.value.with_name(random_fn).as_uri()
                    else:
                        schema_uri = urljoin(self.bblock.schema.value, random_fn)
                    if schema_ref.startswith('#'):
                        # $ref
                        schema_ref = f"{self.bblock.annotated_schema.resolve()}{schema_ref}"
                    elif not is_url(schema_ref):
                        if '#' in schema_ref:
                            path, fragment = schema_ref.split('#', 1)
                            schema_ref = f"{self.bblock.annotated_schema.parent.resolve().joinpath(path)}#{fragment}"
                            ppath = path.rsplit('/', 1)
                            newpath = f"{ppath}/{random_fn}" if ppath else random_fn
                            schema_uri = f"{self.bblock.schema.resolve_ref(newpath)}#{fragment}"
                        else:
                            schema_uri = self.bblock.schema.resolve_ref(schema_ref).with_name(random_fn).as_uri()
                    snippet_schema = {'$ref': schema_ref}
                    schema_validator = get_json_validator(snippet_schema,
                                                          schema_uri,
                                                          self.register)
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
                    if not isinstance(e, jsonschema.exceptions.ValidationError):
                        traceback.print_exception(e)
                    report.add_entry(ValidationReportEntry(
                        section=ValidationReportSection.JSON_SCHEMA,
                        message=f"{type(e).__name__}: {e}",
                        is_error=True,
                        payload={
                            'op': 'validation',
                            'result': False,
                            'exception': e.__class__.__qualname__,
                            'errorMessage': e.message,
                        }
                    ))

            if contents:
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
