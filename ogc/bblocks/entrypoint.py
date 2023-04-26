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
        default='_sources',
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
    bb_config_file = Path(args.items_dir) / 'bblocks-config.yaml'
    items_dir = Path(args.items_dir)

    # Clean old output
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

    # Read local bblocks-config.yaml, if present
    id_prefix = 'r1.'
    annotated_path = Path(args.annotated_path)
    if bb_config_file.is_file():
        bb_config = load_yaml(filename=bb_config_file)
        id_prefix = bb_config.get('identifier-prefix', id_prefix)
        subdirs = id_prefix.split('.')[1:]
        annotated_path = annotated_path.joinpath(Path(*subdirs))

    # 1. Postprocess BBs
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

    # 2. Uplift register.json
    print(f"Running semantic uplift of {register_file}", file=sys.stderr)
    print(f" - {register_jsonld_fn}", file=sys.stderr)
    print(f" - {register_ttl_fn}", file=sys.stderr)
    ingest_json.process_file(register_file,
                             context_fn=uplift_context_file,
                             jsonld_fn=register_jsonld_fn,
                             ttl_fn=register_ttl_fn)

    # 3. Copy Slate assets
    # Run rsync -rlt /src/ogc/bblocks/slate-assets/ "${GENERATED_DOCS_PATH}/slate/"
    print(f"Copying Slate assets to {args.generated_docs_path}/slate", file=sys.stderr)
    subprocess.run([
        'rsync',
        '-rlt',
        '/src/ogc/bblocks/slate-assets/',
        f"{args.generated_docs_path}/slate/",
    ])
