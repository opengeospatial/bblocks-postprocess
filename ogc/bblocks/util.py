import json
import sys
from pathlib import Path
import os.path
from typing import Generator, Any
import jsonschema

from ogc.na.util import load_yaml


def load_file(fn):
    with open(fn) as f:
        return f.read()


def get_bblock_identifier(metadata_file: Path, root_path: Path = Path(),
                          prefix: str = '') -> tuple[str, Path]:
    rel_parts = Path(os.path.relpath(metadata_file.parent, root_path)).parts
    return f"{prefix}{'.'.join(rel_parts)}", Path(*rel_parts)


class BuildingBlock:

    def __init__(self, identifier: str, metadata_file: Path,
                 rel_path: Path,
                 metadata_schema: Any | None = None,
                 annotated_path: Path = Path()):
        self.identifier = identifier
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

    for metadata_file in sorted(registered_items_path.glob("**/bblock.json")):
        bblock_id, bblock_rel_path = get_bblock_identifier(metadata_file, registered_items_path, prefix)
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
