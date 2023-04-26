from __future__ import annotations

import json
import sys
from pathlib import Path
import os.path
from typing import Generator, Any, Sequence
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
    return f"{prefix}{'.'.join(p for p in rel_parts if not p.startswith('_'))}", Path(*rel_parts)


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

        self.schema_contents = None
        self.schema = None
        self.assets_path = None
        self.description = None
        self.examples = None
        self.annotated_path = None
        self.annotated_schema = None
        self.jsonld_context = None

        self._annotated_path = annotated_path / self.subdirs

    def load_files(self):

        fp = self.files_path

        examples_file = fp / 'examples.yaml'
        self.examples = load_yaml(filename=examples_file) if examples_file.exists() else None

        desc_file = fp / 'description.md'
        if desc_file.exists():
            self.description = load_file(desc_file)
        else:
            self.description = None

        ap = fp / 'assets'
        self.assets_path = ap if ap.is_dir() else None

        schema = fp / 'schema.yaml'
        if not schema.exists():
            schema = fp / 'schema.json'
        if schema.is_file():
            self.schema = schema
            self.schema_contents = load_file(schema)
        else:
            self.schema = None
            self.schema_contents = None

        if self._annotated_path.is_dir():
            annotated_path = self._annotated_path
            self.annotated_path = annotated_path
            annotated_schema = annotated_path / 'schema.yaml'
            if not annotated_schema.exists():
                annotated_schema = annotated_path / 'schema.json'
            self.annotated_schema = annotated_schema if annotated_schema.is_file() else None
            jsonld_context = annotated_path / 'context.jsonld'
            self.jsonld_context = jsonld_context if jsonld_context.is_file() else None

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
        for schema_fn in ('schema.yaml', 'schema.json'):
            for schema_file in sorted(sbb_dir.glob(f"**/{schema_fn}")):
                if schema_file in skip_dirs:
                    # Skip descendant super bblocks
                    continue

                schema = load_yaml(schema_file)
                if 'schema' in schema:
                    # OpenAPI sub spec - skip
                    continue
                imported_props = {k: v for k, v in schema.items() if k[0] != '$'}
                one_of.append(imported_props)
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

        dump_yaml(process_sbb(super_bblock_dir, super_bblock, super_bblocks.keys()),
                  super_bblock_dir / 'schema.yaml')
        result.append(super_bblock_dir / 'schema.yaml')
        annotated_output_file = annotated_path / super_bblock.subdirs / 'schema.yaml'
        annotated_output_file.parent.mkdir(parents=True, exist_ok=True)
        dump_yaml(process_sbb(annotated_output_file.parent, super_bblock, annotated_super_bblock_dirs),
                  annotated_output_file)
        result.append(annotated_output_file)
        return result


def write_jsonld_context(annotated_schema: Path) -> Path:
    ctx_builder = ContextBuilder(fn=annotated_schema)
    context_fn = annotated_schema.parent / 'context.jsonld'
    with open(context_fn, 'w') as f:
        json.dump(ctx_builder.context, f, indent=2)
    return context_fn


def annotate_schema(schema_file: Path, items_path: Path, annotated_path: Path) -> list[Path]:
    result = []
    if schema_file.is_file():
        print(f"Annotating {schema_file}", file=sys.stderr)
        annotator = SchemaAnnotator(
            fn=schema_file,
            follow_refs=False
        )
        for annotated_schema in dump_annotated_schemas(annotator,
                                                       annotated_path,
                                                       items_path):
            context_fn = write_jsonld_context(annotated_schema)
            result.append(annotated_schema)
            result.append(context_fn)
    return result
