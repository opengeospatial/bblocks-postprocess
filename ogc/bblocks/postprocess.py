from __future__ import annotations

import json
import os.path
import re
import sys
from argparse import ArgumentParser
from pathlib import Path
import traceback

from ogc.na.util import is_url

from ogc.bblocks.generate_docs import DocGenerator
from ogc.bblocks.util import load_bblocks, write_superbblocks_schemas, annotate_schema, BuildingBlock
from ogc.bblocks.validate import validate_test_resources

ANNOTATED_ITEM_CLASSES = ('schema', 'datatype')
FAKE_JSON_COUNT = 3


def postprocess(registered_items_path: str | Path = 'registereditems',
                output_file: str | Path | None = 'register.json',
                filter_ids: str | list[str] | None = None,
                base_url: str | None = None,
                metadata_schema: str | Path | None = None,
                generated_docs_path: str | Path = 'generateddocs',
                templates_dir: str | Path = 'templates',
                fail_on_error: bool = False,
                id_prefix: str = '',
                annotated_path: str | Path = 'annotated',
                ref_root: str | None = None) -> list[BuildingBlock]:

    doc_generator = DocGenerator(output_dir=generated_docs_path,
                                 templates_dir=templates_dir,
                                 id_prefix=id_prefix)

    if base_url:
        if base_url[-1] != '/':
            base_url += '/'

    def do_postprocess(bblock: BuildingBlock) -> bool:
        cwd = Path().resolve()
        output_file_root = Path(output_file).resolve().parent
        if bblock.annotated_schema.is_file():
            if base_url:
                rel_annotated = os.path.relpath(bblock.annotated_schema, cwd)
                schema_url_yaml = f"{base_url}{rel_annotated}"
            else:
                schema_url_yaml = './' + os.path.relpath(bblock.annotated_schema, output_file_root)
            schema_url_json = re.sub(r'\.yaml$', '.json', schema_url_yaml)
            bblock.metadata['schema'] = [
                schema_url_yaml,
                schema_url_json
            ]
        if bblock.jsonld_context.is_file():
            if base_url:
                rel_context = os.path.relpath(bblock.jsonld_context, cwd)
                ld_context_url = f"{base_url}{rel_context}"
            else:
                ld_context_url = './' + os.path.relpath(bblock.jsonld_context, output_file_root)
            bblock.metadata['ldContext'] = ld_context_url

        rel_files_path = os.path.relpath(bblock.files_path)
        if base_url:
            bblock.metadata['sourceFiles'] = f"{base_url}{rel_files_path}/"
        else:
            bblock.metadata['sourceFiles'] = f"./{os.path.relpath(rel_files_path, output_file_root)}/"

        print(f"  > Generating documentation for {bblock.identifier}")
        doc_generator.generate_doc(bblock, base_url=base_url)
        print(f"  > Running tests for {bblock.identifier}")
        bblock.metadata['validationPassed'] = validate_test_resources(bblock)
        return True

    if not isinstance(registered_items_path, Path):
        registered_items_path = Path(registered_items_path)

    all_bblocks = []
    super_bblocks = {}
    for building_block in load_bblocks(registered_items_path,
                                       filter_ids=filter_ids,
                                       metadata_schema_file=metadata_schema,
                                       fail_on_error=fail_on_error,
                                       prefix=id_prefix,
                                       annotated_path=annotated_path):
        if building_block.super_bblock:
            super_bblocks[building_block.files_path] = building_block
        else:
            # Annotate schema
            print(f"Annotating schema for {building_block.identifier}", file=sys.stderr)

            if building_block.ldContext:
                if is_url(building_block.ldContext):
                    # Use URL directly
                    default_jsonld_context = building_block.ldContext
                else:
                    # Use path relative to bblock.json
                    default_jsonld_context = building_block.files_path / building_block.ldContext
            else:
                # Try local context.jsonld
                default_jsonld_context = building_block.files_path / 'context.jsonld'
                if not default_jsonld_context.is_file():
                    default_jsonld_context = None

            try:
                for annotated in annotate_schema(building_block,
                                                 annotated_path,
                                                 ref_root=ref_root,
                                                 context=default_jsonld_context):
                    print(f"  - {annotated}", file=sys.stderr)
            except Exception as e:
                if fail_on_error:
                    raise
                traceback.print_exception(e, file=sys.stderr)

        all_bblocks.append(building_block)

    # Create super bblock schemas
    print(f"Generating Super Building Block schemas", file=sys.stderr)
    for super_bblock_schema in write_superbblocks_schemas(super_bblocks, registered_items_path, annotated_path):
        print(f"  - {os.path.relpath(super_bblock_schema, '.')}", file=sys.stderr)

    output_bblocks = []
    for building_block in all_bblocks:
        print(f"Processing building block {building_block.identifier}", file=sys.stderr)
        if do_postprocess(building_block):
            output_bblocks.append(building_block.metadata)
        else:
            print(f"{building_block.identifier} failed postprocessing, skipping...", file=sys.stderr)

    if output_file:
        if output_file == '-':
            print(json.dumps(output_bblocks, indent=2))
        else:
            with open(output_file, 'w') as f:
                json.dump(output_bblocks, f, indent=2)

    print(f"Finished processing {len(output_bblocks)} building blocks", file=sys.stderr)
    return output_bblocks


def _main():
    parser = ArgumentParser()

    parser.add_argument(
        'output_register',
        default='register.json',
        help='Output JSON Building Blocks register document',
    )

    parser.add_argument(
        '-u',
        '--base-url',
        help='Base URL for hyperlink generation',
    )

    parser.add_argument(
        '-i',
        '--filter-id',
        nargs='+',
        help='Only process building blocks matching these ids',
    )

    parser.add_argument(
        'registered_items',
        default='registereditems',
        help='Registered items directory',
    )

    parser.add_argument(
        '-N',
        '--no-output',
        action='store_true',
        help='Do not generate output JSON register document',
    )

    parser.add_argument(
        '-s',
        '--metadata-schema',
        help="JSON schema for Building Block metadata validation",
    )

    parser.add_argument(
        '-t',
        '--templates-dir',
        help='Templates directory',
        default='templates'
    )

    parser.add_argument(
        '--fail-on-error',
        action='store_true',
        help='Fail run if an error is encountered',
    )

    parser.add_argument(
        '-p',
        '--identifier-prefix',
        default='r1.',
        help='Building blocks identifier prefix',
    )

    args = parser.parse_args()

    postprocess(args.registered_items,
                output_file=None if args.no_output else args.output_register,
                filter_ids=args.filter_id,
                base_url=args.base_url,
                metadata_schema=args.metadata_schema,
                templates_dir=args.templates_dir,
                fail_on_error=args.fail_on_error,
                id_prefix=args.identifier_prefix)


if __name__ == '__main__':
    _main()
