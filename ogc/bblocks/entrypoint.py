#!/usr/bin/env python3
import json
import shutil
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

from ogc.na.util import load_yaml

from ogc.bblocks import util
from ogc.bblocks.postprocess import postprocess
from ogc.na import ingest_json, annotate_schema

templates_dir = Path(__file__).parent / 'templates'
uplift_context_file = Path(__file__).parent / 'register-context.yaml'

if __name__ == '__main__':

    parser = ArgumentParser()

    parser.add_argument(
        '--register-file',
        default='build/register.json',
        help='Output JSON Building Blocks register document',
    )

    parser.add_argument(
        '--items-dir',
        default='.',
        help='Registered items directory',
    )

    parser.add_argument(
        '--generated-docs-path',
        default='build/generateddocs',
        help='Output directory for generated documentation',
    )

    parser.add_argument(
        '--base-url',
        default='',
        help='Base URL for hyperlink generation',
    )

    parser.add_argument(
        '--fail-on-error',
        default='false',
        help='Fail run if an error is encountered',
    )

    parser.add_argument(
        '--annotated-path',
        default='build/annotated',
        help='Fail run if an error is encountered',
    )

    parser.add_argument(
        '--clean',
        default='false',
        help='Delete output directories and files before generating the new ones',
    )

    args = parser.parse_args()

    fail_on_error = args.fail_on_error in ('true', 'on', 'yes')
    clean = args.clean in ('true', 'on', 'yes')

    print(f"""Running with the following configuration:
- register_file: {args.register_file}
- items_dir: {args.items_dir}
- generated_docs_path: {args.generated_docs_path}
- base_url: {args.base_url}
- templates_dir: {str(templates_dir)}
- annotated_path: {str(args.annotated_path)}
- fail_on_error: {fail_on_error}
- clean: {clean}
    """, file=sys.stderr)

    register_file = Path(args.register_file)
    register_jsonld_fn = register_file.with_suffix('.jsonld') \
        if register_file.suffix != '.jsonld' else register_file.with_suffix(register_file.suffix + '.jsonld')
    register_ttl_fn = register_file.with_suffix('.ttl')

    if clean:
        for old_file in register_file, register_jsonld_fn, register_ttl_fn:
            print(f"Deleting {old_file}", file=sys.stderr)
            old_file.unlink(missing_ok=True)
        cwd = Path().resolve()
        for old_dir in args.generated_docs_path, args.annotated_path:
            # Only delete if not current path and not ancestor
            old_dir = Path(old_dir).resolve()
            if old_dir != cwd and old_dir not in cwd.parents:
                print(f"Deleting {old_dir} recursively", file=sys.stderr)
                shutil.rmtree(old_dir, ignore_errors=True)

    bb_config_file = Path(args.items_dir) / 'bblocks-config.yaml'

    items_dir = Path(args.items_dir)

    id_prefix = 'r1.'
    annotated_path = Path(args.annotated_path)
    if bb_config_file.is_file():
        bb_config = load_yaml(filename=bb_config_file)
        id_prefix = bb_config.get('identifier-prefix', id_prefix)
        subdirs = id_prefix.split('.')[1:]
        annotated_path = annotated_path.joinpath(Path(*subdirs))

    # 1. Annotate schemas
    print('Annotating schemas', file=sys.stderr)
    for fn in ('schema.yaml', 'schema.json'):
        for schema in items_dir.glob(f"**/{fn}"):

            # Skip schemas inside "build", "annotated" and "_superbblock" directories
            schema_path_parts = schema.parts
            if any(x in schema_path_parts for x in ('build', 'annotated', util.SUPERBBLOCK_DIRNAME)):
                continue

            try:
                print(f" - Schema {schema}", file=sys.stderr)
                annotator = annotate_schema.SchemaAnnotator(
                    fn=schema,
                    follow_refs=False
                )
                for annotated_schema in annotate_schema.dump_annotated_schemas(annotator, annotated_path, items_dir):
                    print(f"  - {annotated_schema}", file=sys.stderr)
                    ctx_builder = annotate_schema.ContextBuilder(fn=annotated_schema)
                    context_fn = annotated_schema.parent / 'context.jsonld'
                    print(f"  - {context_fn}", file=sys.stderr)
                    with open(context_fn, 'w') as f:
                        json.dump(ctx_builder.context, f, indent=2)
            except Exception as e:
                if fail_on_error:
                    raise
                import traceback
                traceback.print_exception(e, file=sys.stderr)

    # 2. Write superbblock schemas
    print('Building superbblock schemas', file=sys.stderr)
    annotated_superbblock_schemas = util.write_superbblock_schemas(items_dir, annotated_path=annotated_path)
    if annotated_superbblock_schemas:
        print(' -', '\n - '.join(str(f) for f in annotated_superbblock_schemas), file=sys.stderr)
    else:
        print('  None found', file=sys.stderr)

    # 3. Postprocess BBs
    print(f"Running postprocess...", file=sys.stderr)
    postprocess(registered_items_path=items_dir,
                output_file=args.register_file,
                base_url=args.base_url,
                metadata_schema='/metadata-schema.yaml',
                generated_docs_path=args.generated_docs_path,
                templates_dir=templates_dir,
                fail_on_error=fail_on_error,
                id_prefix=id_prefix,
                annotated_path=annotated_path)

    # 4. Uplift register.json
    print(f"Running semantic uplift of {register_file}", file=sys.stderr)
    print(f" - {register_jsonld_fn}", file=sys.stderr)
    print(f" - {register_ttl_fn}", file=sys.stderr)
    ingest_json.process_file(register_file,
                             context_fn=uplift_context_file,
                             jsonld_fn=register_jsonld_fn,
                             ttl_fn=register_ttl_fn)

    # 5. Copy Slate assets
    # Run rsync -rlt /src/ogc/bblocks/slate-assets/ "${GENERATED_DOCS_PATH}/slate/"
    print(f"Copying Slate assets to {args.generated_docs_path}/slate", file=sys.stderr)
    subprocess.run([
        'rsync',
        '-rlt',
        '/src/ogc/bblocks/slate-assets/',
        f"{args.generated_docs_path}/slate/",
    ])
