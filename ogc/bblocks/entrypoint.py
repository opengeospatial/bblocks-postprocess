#!/usr/bin/env python3

import sys
from pathlib import Path

from ogc.na.util import load_yaml

from ogc.bblocks.postprocess import postprocess
from ogc.na import ingest_json

templates_dir = Path(__file__).parent / 'templates'
uplift_context_file = Path(__file__).parent / 'register-context.yaml'

register_file, items_dir, generated_docs_path, base_url, fail_on_error = sys.argv[1:]

fail_on_error = fail_on_error in ('true', 'on', 'yes')

bb_config_file = Path(items_dir) / 'bblocks-config.yaml'

id_prefix = 'r1.'
if bb_config_file.is_file():
    bb_config = load_yaml(filename=bb_config_file)
    id_prefix = bb_config.get('identifier-prefix', id_prefix)


print(f"""Running with the following configuration:
 - register_file: {register_file}
 - items_dir: {items_dir}
 - generated_docs_path: {generated_docs_path}
 - base_url: {base_url}
 - templates_dir: {str(templates_dir)}
""", file=sys.stderr)

postprocess(registered_items_path=Path(),
            output_file=register_file,
            base_url=base_url,
            metadata_schema='/metadata-schema.yaml',
            generated_docs_path=generated_docs_path,
            templates_dir=templates_dir,
            fail_on_error=fail_on_error,
            id_prefix=id_prefix)

register_file = Path(register_file)
jsonld_fn = register_file.with_suffix('.jsonld') \
    if register_file.suffix != '.jsonld' else register_file.with_suffix(register_file.suffix + '.jsonld')
ttl_fn = register_file.with_suffix('.ttl')

ingest_json.process_file(register_file,
                         context_fn=uplift_context_file,
                         jsonld_fn=jsonld_fn,
                         ttl_fn=ttl_fn)
