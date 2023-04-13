#!/usr/bin/env python3
import json
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

from ogc.na.util import load_yaml

from ogc.bblocks.postprocess import postprocess
from ogc.na import ingest_json, annotate_schema

templates_dir = Path(__file__).parent / 'templates'
uplift_context_file = Path(__file__).parent / 'register-context.yaml'

if __name__ == '__main__':

    parser = ArgumentParser()

    parser.add_argument(
        'register_file',
        default='register.json',
        nargs='?',
        help='Output JSON Building Blocks register document',
    )

    parser.add_argument(
        'items_dir',
        default='.',
        nargs='?',
        help='Registered items directory',
    )

    parser.add_argument(
        'generated_docs_path',
        default='generateddocs',
        nargs='?',
        help='Output directory for generated documentation',
    )

    parser.add_argument(
        'base_url',
        default='',
        nargs='?',
        help='Base URL for hyperlink generation',
    )

    parser.add_argument(
        'fail_on_error',
        default='false',
        nargs='?',
        help='Fail run if an error is encountered',
    )

    parser.add_argument(
        'annotated_path',
        default='annotated',
        nargs='?',
        help='Fail run if an error is encountered',
    )

    args = parser.parse_args()

    print(f"""Running with the following configuration:
- register_file: {args.register_file}
- items_dir: {args.items_dir}
- generated_docs_path: {args.generated_docs_path}
- base_url: {args.base_url}
- templates_dir: {str(templates_dir)}
- annotated_path: {str(args.annotated_path)}
    """, file=sys.stderr)

    fail_on_error = args.fail_on_error in ('true', 'on', 'yes')

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
    for fn in ('schema.yaml', 'schema.json'):
        for schema in items_dir.glob(f"**/{fn}"):
            annotator = annotate_schema.SchemaAnnotator(
                fn=schema,
                follow_refs=False)
            for annotated_schema in annotate_schema.dump_annotated_schemas(annotator, annotated_path):
                ctx_builder = annotate_schema.ContextBuilder(fn=annotated_schema)
                jsonld_fn = annotated_schema.parent / 'context.jsonld'
                with open(jsonld_fn, 'w') as f:
                    json.dump(ctx_builder.context, f, indent=2)

    # 2. Postprocess BBs
    postprocess(registered_items_path=items_dir,
                output_file=args.register_file,
                base_url=args.base_url,
                metadata_schema='/metadata-schema.yaml',
                generated_docs_path=args.generated_docs_path,
                templates_dir=templates_dir,
                fail_on_error=fail_on_error,
                id_prefix=id_prefix)

    # 3. Uplift register.json
    register_file = Path(args.register_file)
    jsonld_fn = register_file.with_suffix('.jsonld') \
        if register_file.suffix != '.jsonld' else register_file.with_suffix(register_file.suffix + '.jsonld')
    ttl_fn = register_file.with_suffix('.ttl')
    ingest_json.process_file(register_file,
                             context_fn=uplift_context_file,
                             jsonld_fn=jsonld_fn,
                             ttl_fn=ttl_fn)

    # 4. Copy Slate assets
    # Run rsync -rlt /src/ogc/bblocks/slate-assets/ "${GENERATED_DOCS_PATH}/slate/"
    subprocess.run([
        'rsync',
        '-rlt',
        '/src/ogc/bblocks/slate-assets/',
        f"{args.generated_docs_path}/slate/",
    ])
