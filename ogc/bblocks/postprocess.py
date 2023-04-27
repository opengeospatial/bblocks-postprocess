from __future__ import annotations

import json
import os.path
import sys
from argparse import ArgumentParser
from pathlib import Path
import traceback

from ogc.na.util import load_yaml

from ogc.bblocks.generate_docs import DocGenerator
from ogc.bblocks.util import load_bblocks, write_superbblocks_schemas, annotate_schema, BuildingBlock, \
    generate_fake_json
from ogc.bblocks.validate import validate_test_resources

ANNOTATED_ITEM_CLASSES = ('schema', 'datatype')
OGC_BBR_REF_ROOT = 'https://raw.githubusercontent.com/opengeospatial/bblocks/master/build/'
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
                annotated_path: str | Path = 'annotated') -> list[BuildingBlock]:

    doc_generator = DocGenerator(output_dir=generated_docs_path,
                                 templates_dir=templates_dir,
                                 id_prefix=id_prefix)

    if base_url and base_url[-1] not in ('/', '#'):
        base_url += '/'
    if id_prefix:
        base_url += '/'.join(id_prefix.split('.')[1:])

    def do_postprocess(bblock: BuildingBlock) -> bool:
        cwd = Path().resolve()
        if base_url:
            if bblock.schema:
                rel_schema = os.path.relpath(bblock.schema, cwd)
                schema_url = f"{base_url}{rel_schema}"
                existing_schemas = bblock.metadata.setdefault('schema', [])

                if bblock.annotated_schema:
                    rel_annotated = os.path.relpath(bblock.annotated_schema, cwd)
                    add_schema_url = f"{base_url}{rel_annotated}"

                    # Remove old, non-annotated schema if present
                    if schema_url in existing_schemas:
                        existing_schemas.remove(schema_url)
                else:
                    add_schema_url = schema_url

                if add_schema_url not in existing_schemas:
                    existing_schemas.append(add_schema_url)

                # if bblock.itemClass in ('datatype', 'schema') and not bblock.super_bblock:
                #     # generate fake JSON
                #     print("Generating JSON examples", file=sys.stderr)
                #     try:
                #         if bblock.jsonld_context:
                #             jsonld_context_contents = load_yaml(bblock.jsonld_context).get('@context')
                #         else:
                #             jsonld_context_contents = None
                #
                #         for i in range(FAKE_JSON_COUNT):
                #             fake_json = generate_fake_json(bblock.schema_contents)
                #             fake_json_fn = bblock.annotated_path / f"example{i + 1}.json"
                #             with open(fake_json_fn, 'w') as f:
                #                 print(f"  - {fake_json_fn}", file=sys.stderr)
                #                 json.dump(fake_json, f, indent=2)
                #
                #             if jsonld_context_contents:
                #                 if isinstance(fake_json, dict):
                #                     fake_json = {
                #                         '@context': jsonld_context_contents,
                #                         **fake_json
                #                     }
                #                 elif isinstance(fake_json, list):
                #                     fake_json = {
                #                         '@context': jsonld_context_contents,
                #                         '@graph': fake_json
                #                     }
                #                 fake_jsonld_fn = fake_json_fn.with_suffix('.jsonld')
                #                 with open(fake_jsonld_fn, 'w') as f:
                #                     print(f"  - {fake_jsonld_fn}", file=sys.stderr)
                #                     json.dump(fake_json, f, indent=2)
                #
                #     except Exception as e:
                #         print(f"Error generating fake JSON for {bblock.identifier}", file=sys.stderr)
                #         traceback.print_exception(e, file=sys.stderr)

        doc_generator.generate_doc(bblock, base_url=base_url)
        validate_test_resources(bblock)
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
        elif building_block.itemClass in ANNOTATED_ITEM_CLASSES:
            # Annotate schema
            schema_file = building_block.files_path / 'schema.yaml'
            print(f"Annotating {schema_file}", file=sys.stderr)
            try:
                for annotated in annotate_schema(schema_file, registered_items_path, annotated_path,
                                                 ref_root=OGC_BBR_REF_ROOT):
                    print(f"  - {annotated}", file=sys.stderr)
            except Exception as e:
                if fail_on_error:
                    raise
                traceback.print_exception(e, file=sys.stderr)

        all_bblocks.append(building_block)

    # Create super bblock schemas
    print(f"Generating Super Building Block schemas", file=sys.stderr)
    for super_bblock_schema in write_superbblocks_schemas(super_bblocks, registered_items_path, annotated_path):
        print(f"  - {super_bblock_schema}", file=sys.stderr)

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
