#!/usr/bin/env python3
import shutil
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

from ogc.na.util import load_yaml

from ogc.bblocks.postprocess import postprocess
from ogc.na import ingest_json

MAIN_BBR = 'https://blocks.ogc.org/register.json'
DEFAULT_IMPORT_MARKER = 'default'

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

    parser.add_argument(
        '--ref-root',
        default='https://raw.githubusercontent.com/opengeospatial/bblocks/master/build/',
        help='Value of $_ROOT_ for usage in $ref values inside JSON schemas'
    )

    parser.add_argument(
        '--config-file',
        default='bblocks-config.yaml',
        help='bblocks-config.yml file, if any'
    )

    parser.add_argument(
        '--test-outputs-path',
        default='build/tests',
        help='Directory for test output resources',
    )

    parser.add_argument(
        '--github-base-url',
        help='Base URL for linking to GitHub content',
    )

    args = parser.parse_args()

    fail_on_error = args.fail_on_error in ('true', 'on', 'yes')
    clean = args.clean in ('true', 'on', 'yes')
    bb_config_file = Path(args.config_file) if args.config_file else None

    print(f"""Running with the following configuration:
- register_file: {args.register_file}
- items_dir: {args.items_dir}
- generated_docs_path: {args.generated_docs_path}
- base_url: {args.base_url}
- templates_dir: {str(templates_dir)}
- annotated_path: {str(args.annotated_path)}
- fail_on_error: {fail_on_error}
- clean: {clean}
- config_file: {bb_config_file}
- test_outputs_path: {args.test_outputs_path}
- github_base_url: {args.github_base_url}
    """, file=sys.stderr)

    register_file = Path(args.register_file)
    register_jsonld_fn = register_file.with_name('bblocks.jsonld')
    if register_file.suffix == '.jsonld':
        register_jsonld_fn = register_jsonld_fn.with_suffix('.jsonld.jsonld')
    register_ttl_fn = register_jsonld_fn.with_suffix('.ttl')
    items_dir = Path(args.items_dir)

    # Clean old output
    if clean:
        for old_file in register_file, register_jsonld_fn, register_ttl_fn:
            print(f"Deleting {old_file}", file=sys.stderr)
            old_file.unlink(missing_ok=True)
        cwd = Path().resolve()
        for old_dir in args.generated_docs_path, args.annotated_path, args.test_outputs_path:
            # Only delete if not current path and not ancestor
            old_dir = Path(old_dir).resolve()
            if old_dir != cwd and old_dir not in cwd.parents:
                print(f"Deleting {old_dir} recursively", file=sys.stderr)
                shutil.rmtree(old_dir, ignore_errors=True)

    # Read local bblocks-config.yaml, if present
    id_prefix = 'ogc.'
    annotated_path = Path(args.annotated_path)
    imported_registers = []
    if bb_config_file and bb_config_file.is_file():
        bb_config = load_yaml(filename=bb_config_file)
        id_prefix = bb_config.get('identifier-prefix', id_prefix)
        if id_prefix and id_prefix[-1] != '.':
            id_prefix += '.'
        subdirs = id_prefix.split('.')[1:]
        imported_registers = bb_config.get('imports')
        if imported_registers is None:
            imported_registers = [MAIN_BBR]
        else:
            imported_registers = [ir if ir != DEFAULT_IMPORT_MARKER else MAIN_BBR for ir in imported_registers if ir]

    # 1. Postprocess BBs
    print(f"Running postprocess...", file=sys.stderr)
    postprocess(registered_items_path=items_dir,
                output_file=args.register_file,
                base_url=args.base_url,
                generated_docs_path=args.generated_docs_path,
                templates_dir=templates_dir,
                fail_on_error=fail_on_error,
                id_prefix=id_prefix,
                annotated_path=annotated_path,
                test_outputs_path=args.test_outputs_path,
                github_base_url=args.github_base_url,
                imported_registers=imported_registers)

    # 2. Uplift register.json
    print(f"Running semantic uplift of {register_file}", file=sys.stderr)
    print(f" - {register_jsonld_fn}", file=sys.stderr)
    print(f" - {register_ttl_fn}", file=sys.stderr)
    ingest_json.process_file(register_file,
                             context_fn=uplift_context_file,
                             jsonld_fn=register_jsonld_fn,
                             ttl_fn=register_ttl_fn,
                             provenance_base_uri=args.base_url)

    # 3. Copy Slate assets
    # Run rsync -rlt /src/ogc/bblocks/slate-assets/ "${GENERATED_DOCS_PATH}/slate/"
    print(f"Copying Slate assets to {args.generated_docs_path}/slate", file=sys.stderr)
    subprocess.run([
        'rsync',
        '-rlt',
        str(Path(__file__).parent / 'slate-assets') + '/',
        f"{args.generated_docs_path}/slate/",
    ])

    print(f"Finished Building Blocks postprocessing", file=sys.stderr)
