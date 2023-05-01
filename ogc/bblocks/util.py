from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
import os.path
from typing import Generator, Any
import jsonschema
from ogc.na.annotate_schema import dump_annotated_schemas, SchemaAnnotator, ContextBuilder

from ogc.na.util import load_yaml, dump_yaml

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


class BuildingBlock:

    def __init__(self, identifier: str, metadata_file: Path,
                 rel_path: Path,
                 metadata_schema: Any | None = None,
                 annotated_path: Path = Path()):
        self.identifier = identifier
        metadata_file = metadata_file.resolve()
        self.metadata_file = metadata_file

        with open(metadata_file) as f:
            self.metadata = json.load(f)

            if metadata_schema:
                jsonschema.validate(self.metadata, metadata_schema)

            self.metadata['itemIdentifier'] = identifier

        self.subdirs = rel_path
        if '.' in self.identifier:
            self.subdirs = Path(*(identifier.split('.')[1:]))

        self.super_bblock = self.metadata.get('superBBlock', False)

        fp = metadata_file.parent
        self.files_path = fp

        schema = fp / 'schema.yaml'
        if not schema.exists():
            schema = fp / 'schema.json'
        self.schema = schema if schema.is_file() else None

        ap = fp / 'assets'
        self.assets_path = ap if ap.is_dir() else None

        self.examples_file = fp / 'examples.yaml'
        self.tests_dir = fp / 'tests'

        self.annotated_path = annotated_path / self.subdirs
        self.annotated_schema = self.annotated_path / 'schema.yaml'
        self.jsonld_context = annotated_path / 'context.jsonld'

        self._lazy_properties = {}

    @property
    def examples(self):
        if 'examples' not in self._lazy_properties:
            self._lazy_properties['examples'] = load_yaml(filename=self.examples_file) if self.examples_file.exists() else None
        return self._lazy_properties['examples']

    @property
    def schema_contents(self):
        if 'schema_contents' not in self._lazy_properties:
            self._lazy_properties['schema_contents'] = load_file(self.schema) if self.schema else None
        return self._lazy_properties['schema_contents']

    @property
    def description(self):
        if 'description' not in self._lazy_properties:
            desc_file = self.files_path / 'description.md'
            self._lazy_properties['description'] = load_file(desc_file) if desc_file.is_file() else None
        return self._lazy_properties['description']

    def __getattr__(self, item):
        return self.metadata.get(item)


def load_bblocks(registered_items_path: Path,
                 annotated_path: Path = Path(),
                 filter_ids: str | list[str] | None = None,
                 metadata_schema_file: str | Path | None = None,
                 fail_on_error: bool = False,
                 prefix: str = 'r1.') -> Generator[BuildingBlock, None, None]:
    if metadata_schema_file:
        metadata_schema = load_yaml(metadata_schema_file)
    else:
        metadata_schema = None

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
                               items_dir: Path,
                               annotated_path: Path | None = None) -> list[Path]:
    def process_sbb(sbb_dir: Path, sbb: BuildingBlock, skip_dirs) -> dict:
        one_of = []
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
                if 'schema' in schema:
                    # OpenAPI sub spec - skip
                    continue
                imported_props = {k: v for k, v in schema.items() if k[0] != '$'}
                one_of.append(imported_props)

                parsed.add(schema_file.with_suffix(''))

        output_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'description': sbb.name,
        }
        if one_of:
            output_schema['oneOf'] = one_of
        return output_schema

    annotated_super_bblock_dirs = set(annotated_path / b.subdirs for b in super_bblocks.values())
    result = []

    for super_bblock_dir, super_bblock in super_bblocks.items():
        super_schema = process_sbb(super_bblock_dir, super_bblock, super_bblocks.keys())

        dump_yaml(super_schema, super_bblock_dir / 'schema.yaml')
        result.append(super_bblock_dir / 'schema.yaml')

        annotated_output_file = annotated_path / super_bblock.subdirs / 'schema.yaml'
        annotated_output_file.parent.mkdir(parents=True, exist_ok=True)
        super_schema_annotated = process_sbb(annotated_output_file.parent, super_bblock, annotated_super_bblock_dirs)
        dump_yaml(super_schema_annotated, annotated_output_file)
        result.append(annotated_output_file)
        with open(annotated_output_file.with_suffix('.json'), 'w') as f:
            json.dump(super_schema_annotated, f)

        result.append(write_jsonld_context(annotated_output_file))

    return result


def write_jsonld_context(annotated_schema: Path) -> Path:
    ctx_builder = ContextBuilder(fn=annotated_schema)
    context_fn = annotated_schema.parent / 'context.jsonld'
    with open(context_fn, 'w') as f:
        json.dump(ctx_builder.context, f, indent=2)
    return context_fn


def annotate_schema(bblock: BuildingBlock, annotated_path: Path,
                    ref_root: str | None = None) -> list[Path]:
    if not bblock.schema:
        return []

    annotator = SchemaAnnotator(
        fn=bblock.schema,
        follow_refs=False,
        ref_root=ref_root,
    )

    # follow_refs=False => only one schema
    annotated_schema = next(iter(annotator.schemas.values()), None)

    if not annotated_schema:
        return []

    annotated_schema = annotated_schema.schema

    result = []

    annotated_schema_fn = annotated_path / bblock.subdirs / 'schema.yaml'
    annotated_schema_fn.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(annotated_schema, annotated_schema_fn)
    result.append(annotated_schema_fn)
    annotated_schema_json_fn = annotated_schema_fn.with_suffix('.json')
    with open(annotated_schema_json_fn, 'w') as f:
        json.dump(annotated_schema, f, indent=2)
    result.append(annotated_schema_json_fn)
    context_fn = write_jsonld_context(annotated_schema_fn)
    result.append(context_fn)
    return result


def generate_fake_json(schema_contents: str) -> Any:
    return json.loads(subprocess.run([
        'node',
        str(Path(__file__).parent / 'schema-faker')
    ], input=schema_contents, capture_output=True, text=True).stdout)
