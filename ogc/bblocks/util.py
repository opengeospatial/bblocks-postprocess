import json
import sys
from pathlib import Path
import os.path
from typing import Generator, Any
import jsonschema
from ogc.na import annotate_schema

from ogc.na.util import load_yaml, dump_yaml

SUPERBBLOCK_DIRNAME = '_superbblock'
BBLOCK_METADATA_FILE = 'bblock.json'


def load_file(fn):
    with open(fn) as f:
        return f.read()


def get_bblock_identifier(metadata_file: Path, root_path: Path = Path(),
                          prefix: str = '') -> tuple[str, Path]:
    rel_parts = Path(os.path.relpath(metadata_file.parent, root_path)).parts
    if rel_parts[-1] == SUPERBBLOCK_DIRNAME:
        # Super Building Block -> remove suffix
        rel_parts = rel_parts[:-1]
    return f"{prefix}{'.'.join(rel_parts)}", Path(*rel_parts)


class BuildingBlock:

    def __init__(self, identifier: str, metadata_file: Path,
                 rel_path: Path,
                 metadata_schema: Any | None = None,
                 annotated_path: Path = Path()):
        self.identifier = identifier
        metadata_file = metadata_file.resolve()

        # Super Building Block whose schema is an aggregation
        # of all the building blocks in its same directory and its descendants
        self.superbblock = metadata_file.parent.name == SUPERBBLOCK_DIRNAME
        potential_clash = metadata_file.parent.parent / BBLOCK_METADATA_FILE
        if self.superbblock and potential_clash.exists():
            raise ValueError(f"Found superbblock at {metadata_file}, but another one exists at {potential_clash}")

        with open(metadata_file) as f:
            self.metadata = json.load(f)

            if metadata_schema:
                jsonschema.validate(self.metadata, metadata_schema)

            self.metadata['itemIdentifier'] = identifier

        self.subdirs = rel_path
        if '.' in self.identifier:
            self.subdirs = Path(*(identifier.split('.')[1:]))

        fp = metadata_file.parent
        self.files_path = fp

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

        annotated_path = annotated_path / self.subdirs
        if annotated_path.is_dir():
            self.annotated_path = annotated_path
            annotated_schema = annotated_path / 'schema.yaml'
            if not annotated_schema.exists():
                annotated_schema = annotated_path / 'schema.json'
            self.annotated_schema = annotated_schema if annotated_schema.is_file() else None
            jsonld_context = annotated_path / 'context.jsonld'
            self.jsonld_context = jsonld_context if jsonld_context.is_file() else None
        else:
            self.annotated_path = None
            self.annotated_schema = None
            self.jsonld_context = None

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


def write_superbblock_schemas(items_dir: Path,
                              annotated_path: Path | None = None) -> list[Path]:
    result = []
    for super_bblock_dir in items_dir.glob(f"**/{SUPERBBLOCK_DIRNAME}"):
        if not super_bblock_dir.is_dir():
            continue

        if annotated_path:
            annotated_path = annotated_path.resolve()
            resolved_super_bblock_dir = super_bblock_dir.resolve()
            if annotated_path in resolved_super_bblock_dir.parents:
                # If we are in the annotated directory, skip
                continue

        metadata = load_yaml(super_bblock_dir / BBLOCK_METADATA_FILE)

        def process_sbb(schemas_path: Path, process_inside_annotated = False):
            one_of = []
            schemas_path = schemas_path.resolve()
            for fn in ('schema.yaml', 'schema.json'):
                for schema_file in sorted(schemas_path.glob(f"**/{fn}")):
                    if schema_file.parent.name == SUPERBBLOCK_DIRNAME:
                        # Skip descendant superbblocks
                        continue
                    if annotated_path in schema_file.parents and not process_inside_annotated:
                        # If not processing annotated schemas but this schema is
                        # inside the annotated directory, skip it
                        continue

                    schema = load_yaml(schema_file)
                    if 'schema' in schema:
                        # OpenAPI sub spec - skip
                        continue
                    imported_props = {k: v for k, v in schema.items() if k[0] != '$'}
                    one_of.append(imported_props)

            output_schema = {
                '$schema': 'https://json-schema.org/draft/2020-12/schema',
                'description': metadata['name'],
            }
            if one_of:
                output_schema['oneOf'] = one_of
            return output_schema

        dump_yaml(process_sbb(super_bblock_dir.parent), super_bblock_dir / 'schema.yaml')
        result.append(super_bblock_dir / 'schema.yaml')
        annotated_output_file = annotated_path / super_bblock_dir.relative_to(items_dir) / 'schema.yaml'
        annotated_output_file.parent.mkdir(parents=True, exist_ok=True)
        dump_yaml(process_sbb(annotated_output_file.parent.parent, True), annotated_output_file)
        result.append(annotated_output_file)
        return result


def write_jsonld_context(annotated_schema: Path) -> Path:
    ctx_builder = annotate_schema.ContextBuilder(fn=annotated_schema)
    context_fn = annotated_schema.parent / 'context.jsonld'
    with open(context_fn, 'w') as f:
        json.dump(ctx_builder.context, f, indent=2)
    return context_fn
