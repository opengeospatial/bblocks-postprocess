from __future__ import annotations

import functools
import json
import re
import subprocess
import sys
from pathlib import Path
import os.path
from typing import Generator, Any, Sequence
from urllib.parse import urljoin

import jsonschema
from ogc.na.annotate_schema import dump_annotated_schemas, SchemaAnnotator, ContextBuilder

from ogc.na.util import load_yaml, dump_yaml, is_url

BBLOCK_METADATA_FILE = 'bblock.json'


def load_file(fn):
    with open(fn) as f:
        return f.read()


def get_bblock_identifier(metadata_file: Path, root_path: Path = Path(),
                          prefix: str = '') -> tuple[str, Path]:
    rel_parts = Path(os.path.relpath(metadata_file.parent, root_path)).parts
    identifier = f"{prefix}{'.'.join(p for p in rel_parts if not p.startswith('_'))}"
    if identifier[-1] == '.':
        identifier = identifier[:-1]
    return identifier, Path(*rel_parts)


def get_bblock_subdirs(identifier: str) -> Path:
    return Path(*(identifier.split('.')[1:]))


class BuildingBlockError(Exception):
    pass


class BuildingBlock:

    def __init__(self, identifier: str, metadata_file: Path,
                 rel_path: Path,
                 metadata_schema: Any | None = None,
                 examples_schema: Any | None = None,
                 annotated_path: Path = Path()):
        self.identifier = identifier
        metadata_file = metadata_file.resolve()
        self.metadata_file = metadata_file

        with open(metadata_file) as f:
            self.metadata = json.load(f)

            if metadata_schema:
                try:
                    jsonschema.validate(self.metadata, metadata_schema)
                except Exception as e:
                    raise BuildingBlockError('Error validating building block metadata') from e

            self.metadata['itemIdentifier'] = identifier

        self._lazy_properties = {}

        self.subdirs = rel_path
        if '.' in self.identifier:
            self.subdirs = get_bblock_subdirs(identifier)

        self.super_bblock = self.metadata.get('superBBlock', False)

        fp = metadata_file.parent
        self.files_path = fp

        schema = fp / 'schema.yaml'
        if not schema.exists():
            schema = fp / 'schema.json'
        self.schema = schema

        ap = fp / 'assets'
        self.assets_path = ap if ap.is_dir() else None

        self.examples_file = fp / 'examples.yaml'
        if examples_schema and self.examples_file.is_file():
            examples = load_yaml(self.examples_file)
            try:
                jsonschema.validate(examples, examples_schema)
            except Exception as e:
                raise BuildingBlockError('Error validating building block examples') from e
            self._lazy_properties['examples'] = examples

        self.tests_dir = fp / 'tests'

        self.annotated_path = annotated_path / self.subdirs
        self.annotated_schema = self.annotated_path / 'schema.yaml'
        self.jsonld_context = self.annotated_path / 'context.jsonld'

    @property
    def examples(self):
        if 'examples' not in self._lazy_properties:
            self._lazy_properties['examples'] = load_yaml(filename=self.examples_file) if self.examples_file.exists() else None
        return self._lazy_properties['examples']

    @property
    def schema_contents(self):
        if 'schema_contents' not in self._lazy_properties:
            if not self.schema.exists():
                return None
            self._lazy_properties['schema_contents'] = load_file(self.schema)
        return self._lazy_properties['schema_contents']

    @property
    def description(self):
        if 'description' not in self._lazy_properties:
            desc_file = self.files_path / 'description.md'
            self._lazy_properties['description'] = load_file(desc_file) if desc_file.is_file() else None
        return self._lazy_properties['description']

    def __getattr__(self, item):
        return self.metadata.get(item)

    @property
    def annotated_schema_contents(self):
        # We try to read it each time until we succeed, since it could
        # be created later during the postprocessing
        if 'annotated_schema_contents' not in self._lazy_properties:
            if not self.annotated_schema.is_file():
                return None
            self._lazy_properties['annotated_schema_contents'] = load_file(self.annotated_schema)
        return self._lazy_properties['annotated_schema_contents']


def load_bblocks(registered_items_path: Path,
                 annotated_path: Path = Path(),
                 filter_ids: str | list[str] | None = None,
                 metadata_schema_file: str | Path | None = None,
                 examples_schema_file: str | Path | None = None,
                 fail_on_error: bool = False,
                 prefix: str = 'ogc.') -> Generator[BuildingBlock, None, None]:

    metadata_schema = load_yaml(metadata_schema_file) if metadata_schema_file else None
    examples_schema = load_yaml(examples_schema_file) if examples_schema_file else None

    seen_ids = set()
    for metadata_file in sorted(registered_items_path.glob(f"**/{BBLOCK_METADATA_FILE}")):
        bblock_id, bblock_rel_path = get_bblock_identifier(metadata_file, registered_items_path, prefix)
        if bblock_id in seen_ids:
            raise ValueError(f"Found duplicate bblock id: {bblock_id}")
        seen_ids.add(bblock_id)
        if not filter_ids or bblock_id in filter_ids:
            try:
                yield BuildingBlock(bblock_id, metadata_file,
                                    metadata_schema=metadata_schema,
                                    examples_schema=examples_schema,
                                    rel_path=bblock_rel_path,
                                    annotated_path=annotated_path)
            except Exception as e:
                if fail_on_error:
                    raise
                print('==== Exception encountered while processing', bblock_id, '====', file=sys.stderr)
                import traceback
                traceback.print_exception(e, file=sys.stderr)
                print('=========', file=sys.stderr)
        else:
            print(f"Skipping building block {bblock_id} (not in filter_ids)", file=sys.stderr)


def write_superbblocks_schemas(super_bblocks: dict[Path, BuildingBlock],
                               annotated_path: Path | None = None) -> list[Path]:

    def update_refs(schema, s, c):
        for prop, val in schema.items():
            if prop == '$ref':
                if not is_url(val):
                    # update
                    ref = c / val
                    schema[prop] = os.path.relpath(ref, s)
            elif isinstance(val, dict):
                update_refs(val, s, c)
            elif isinstance(val, list):
                for item in val:
                    if isinstance(item, dict):
                        update_refs(item, s, c)

    def process_sbb(sbb_dir: Path, sbb: BuildingBlock, skip_dirs) -> dict:
        any_of = []
        parsed = set()
        for schema_fn in ('schema.yaml', 'schema.json'):
            for schema_file in sorted(sbb_dir.glob(f"**/{schema_fn}")):
                # Skip schemas in superbblock directory, avoid double parsing
                # (schema.yaml and schema.json) and in child superbblock directories
                if schema_file.parent == sbb_dir \
                        or schema_file.with_suffix('') in parsed \
                        or schema_file.parents in skip_dirs:
                    continue

                schema = load_yaml(schema_file)
                if not isinstance(schema, dict):
                    continue

                if 'schema' in schema:
                    # OpenAPI sub spec - skip
                    continue

                # update relative $ref's
                update_refs(schema, sbb_dir.resolve(), schema_file.parent.resolve())

                any_of.append(schema)

                parsed.add(schema_file.with_suffix(''))

        output_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'description': sbb.name,
        }
        if any_of:
            output_schema['anyOf'] = any_of
        return output_schema

    annotated_super_bblock_dirs = set(annotated_path / b.subdirs for b in super_bblocks.values())
    result = []

    for super_bblock_dir, super_bblock in super_bblocks.items():
        # Should we generate the schema in the source directory? Let's not for now...
        # super_schema = process_sbb(super_bblock_dir, super_bblock, super_bblocks.keys())
        # dump_yaml(super_schema, super_bblock_dir / 'schema.yaml')
        # result.append(super_bblock_dir / 'schema.yaml')

        annotated_output_file = annotated_path / super_bblock.subdirs / 'schema.yaml'
        annotated_output_file.parent.mkdir(parents=True, exist_ok=True)
        super_schema_annotated = process_sbb(annotated_output_file.parent, super_bblock, annotated_super_bblock_dirs)
        dump_yaml(super_schema_annotated, annotated_output_file)
        result.append(annotated_output_file)
        with open(annotated_output_file.with_suffix('.json'), 'w') as f:
            json.dump(super_schema_annotated, f, indent=2)

        jsonld_context = write_jsonld_context(annotated_output_file)
        if jsonld_context:
            result.append(jsonld_context)

    return result


def write_jsonld_context(annotated_schema: Path) -> Path | None:
    ctx_builder = ContextBuilder(fn=annotated_schema)
    if not ctx_builder.context.get('@context'):
        return None
    context_fn = annotated_schema.parent / 'context.jsonld'
    with open(context_fn, 'w') as f:
        json.dump(ctx_builder.context, f, indent=2)
    return context_fn


def annotate_schema(bblock: BuildingBlock,
                    context: Path | dict | None = None,
                    default_base_url: str | None = None,
                    identifier_url_mappings: list[dict[str, str]] | None = None) -> list[Path]:
    result = []
    schema_fn = None
    schema_url = None
    metadata_schemas = bblock.metadata.get('schema')

    if isinstance(metadata_schemas, Sequence):
        # Take only first, if more than one
        ref_schema = metadata_schemas if isinstance(metadata_schemas, str) else metadata_schemas[0]
        if is_url(ref_schema, http_only=True):
            schema_url = ref_schema
        else:
            schema_fn = ref_schema
    elif bblock.schema.is_file():
        schema_fn = bblock.schema

    if not schema_fn and not schema_url:
        return result

    ref_mapper = functools.partial(resolve_schema_reference,
                                   from_identifier=bblock.identifier,
                                   default_base_url=default_base_url,
                                   identifier_url_mappings=identifier_url_mappings)

    annotator = SchemaAnnotator(
        url=schema_url,
        fn=schema_fn,
        follow_refs=False,
        ref_mapper=ref_mapper,
        context=context,
    )

    # follow_refs=False => only one schema
    annotated_schema = next(iter(annotator.schemas.values()), None)

    if not annotated_schema:
        return result

    annotated_schema = annotated_schema.schema
    if schema_url and '$id' not in annotated_schema:
        annotated_schema['$id'] = schema_url

    result = []

    annotated_schema_fn = bblock.annotated_path / 'schema.yaml'
    annotated_schema_fn.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(annotated_schema, annotated_schema_fn)
    result.append(annotated_schema_fn)
    annotated_schema_json_fn = annotated_schema_fn.with_suffix('.json')
    # TODO: Replace schema.yaml refs with schema.json
    with open(annotated_schema_json_fn, 'w') as f:
        json.dump(annotated_schema, f, indent=2)
    result.append(annotated_schema_json_fn)
    return result


def resolve_schema_reference(ref: str,
                             from_identifier: str | None = None,
                             default_base_url: str | None = None,
                             identifier_url_mappings: list[dict[str, str]] | None = None) -> str:

    if not ref.startswith('bblocks://'):
        return ref

    target_id = ref[len('bblocks://'):]

    base_url = default_base_url
    if identifier_url_mappings:
        for mapping in identifier_url_mappings:
            prefix = mapping['prefix']
            if prefix[-1] != '.':
                prefix += '.'
            if target_id.startswith(prefix):
                target_id = target_id[len(prefix):]
                base_url = mapping.get('base_url')
                break

    if not base_url:
        if from_identifier:
            # Compute local relative path
            target_path = get_bblock_subdirs(target_id)
            from_path = get_bblock_subdirs(from_identifier)
            rel_path = os.path.relpath(target_path, from_path)
            return f"{rel_path}/schema.yaml"
        else:
            return ref

    if base_url[-1] != '/':
        base_url += '/'
    subdirs = get_bblock_subdirs(target_id)
    return f"{base_url}{subdirs}/schema.yaml"
