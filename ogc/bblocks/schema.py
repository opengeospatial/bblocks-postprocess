from __future__ import annotations

import json
import os
import re
import sys

from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from ogc.na.annotate_schema import SchemaResolver, SchemaAnnotator
from ogc.na.util import load_yaml, dump_yaml, is_url

from ogc.bblocks import oas30
from ogc.bblocks.util import update_refs, PathOrUrl, BBLOCKS_REF_ANNOTATION

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ogc.bblocks.models import BuildingBlockRegister, BuildingBlock


class RegisterSchemaResolver(SchemaResolver):
    """
    Overrides load_contents to read annotated versions of the local and
    imported bblock schemas.
    """

    def __init__(self, register: BuildingBlockRegister, working_directory=Path()):
        super().__init__(working_directory=working_directory)
        self.register = register

    def find_schema(self, s: str | Path):
        if isinstance(s, Path):
            s = os.path.relpath(s)
        if s in self.register.local_bblock_files:
            bblock_id = self.register.local_bblock_files[s]
            bblock = self.register.bblocks[bblock_id]
            if bblock.annotated_schema.is_file():
                # NOTE: Only change it if it exists, otherwise the first time reading
                # the source schema (to begin the annotation) will fail
                s = bblock.annotated_schema
        elif s in self.register.imported_bblock_files:
            bblock_id = self.register.imported_bblock_files[s]
            bblock = self.register.imported_bblocks[bblock_id]
            s = bblock['schema']['application/yaml']
        return s

    def load_contents(self, s: str | Path) -> tuple[dict, bool]:
        return super().load_contents(self.find_schema(s))


def annotate_schema(bblock: BuildingBlock,
                    bblocks_register: BuildingBlockRegister,
                    context: Path | dict | None = None,
                    base_url: str | None = None) -> list[Path]:
    result = []
    schema_fn = None
    schema_url = None

    if bblock.schema.is_url:
        schema_url = bblock.schema.value
    elif bblock.schema.exists:
        schema_fn = bblock.schema.value

    if not schema_fn and not schema_url:
        return result

    override_schema = load_yaml(filename=schema_fn, url=schema_url)
    override_schema = resolve_all_schema_references(override_schema, bblocks_register, bblock,
                                                    bblock.schema, base_url)

    annotator = SchemaAnnotator(schema_resolver=bblocks_register.schema_resolver)

    bb_extends = bblock.extends
    if bb_extends:
        bb_path = None
        if isinstance(bb_extends, dict):
            bb_path = bb_extends.get('path')
            bb_extends = bb_extends['itemIdentifier']

        if bb_path in (None, '', '.', '$'):
            inserted_schema = override_schema
        else:
            bb_path = re.split(r'\.(?=(?:[^"]*"[^"]*")*[^"]*$)',
                               re.sub(r'^[.$]', '', bb_path.strip()))
            inserted_schema = {}
            inner_schema = inserted_schema
            for p in bb_path:
                p = p.replace('"', '')
                inner_schema['properties'] = {}

                if p.endswith('[]'):
                    p = p[:-2]
                    inner_schema['properties'][p] = {
                        'type': 'array',
                        'items': {}
                    }
                    inner_schema = inner_schema['properties'][p]['items']
                else:
                    inner_schema = inner_schema['properties'].setdefault(p, {})
            for k, v in override_schema.items():
                if k != '$schema' and not k.startswith('x-jsonld-'):
                    inner_schema[k] = v

        override_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'allOf': [
                {'$ref': f"bblocks://{bb_extends}"},
                inserted_schema,
            ],
            **{k: v for k, v in override_schema.items() if k.startswith('x-jsonld-')}
        }

    annotated_schema = annotator.process_schema(schema_url or schema_fn, context, override_schema)

    if not annotated_schema:
        return result

    annotated_schema = annotated_schema.schema
    # if schema_url and '$id' not in annotated_schema:
    #      annotated_schema['$id'] = schema_url

    result = []

    # YAML
    annotated_schema_fn = bblock.annotated_path / 'schema.yaml'
    annotated_schema_fn.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(annotated_schema, annotated_schema_fn)
    result.append(annotated_schema_fn)

    potential_yaml_refs = {}

    def update_json_ref(ref: str):
        if ref[0] == '#' or not is_url(ref):
            return ref
        if '#' in ref:
            ref, fragment = ref.split('#', 1)
            fragment = '#' + fragment
        else:
            fragment = ''
        if ref in bblocks_register.local_bblock_files or ref in bblocks_register.imported_bblock_files:
            return re.sub(r'\.yaml$', r'.json', ref) + fragment
        elif ref.endswith('.yaml'):
            potential_yaml_refs[ref] = True
        return ref

    # JSON
    update_refs(annotated_schema, update_json_ref)
    annotated_schema_json_fn = annotated_schema_fn.with_suffix('.json')
    with open(annotated_schema_json_fn, 'w') as f:
        json.dump(annotated_schema, f, indent=2)
    result.append(annotated_schema_json_fn)
    if potential_yaml_refs:
        print('\n[WARNING] Potential YAML $ref\'s found in JSON version of schema:\n -',
              '\n - '.join(potential_yaml_refs.keys()), '\n\n')

    # OAS 3.0
    try:
        if base_url:
            oas30_schema_fn = annotated_schema_fn.with_stem('schema-oas3.0')
            dump_yaml(oas30.schema_to_oas30(annotated_schema_fn,
                                            urljoin(base_url, str(os.path.relpath(oas30_schema_fn))),
                                            bblocks_register),
                      oas30_schema_fn)
            result.append(oas30_schema_fn)

            oas30_schema_json_fn = annotated_schema_json_fn.with_stem('schema-oas3.0')
            with open(oas30_schema_json_fn, 'w') as f:
                json.dump(oas30.schema_to_oas30(annotated_schema_json_fn,
                                                urljoin(base_url, str(os.path.relpath(oas30_schema_json_fn))),
                                                bblocks_register), f, indent=2)
            result.append(oas30_schema_json_fn)
    except Exception as e:
        print('Error building OAS 3.0 documents:', e, file=sys.stderr)

    return result


def resolve_all_schema_references(schema: Any,
                                  bblocks_register: BuildingBlockRegister,
                                  from_bblock: BuildingBlock,
                                  from_document: PathOrUrl,
                                  base_url: str | None = None) -> Any:
    def walk(subschema):
        if isinstance(subschema, dict):
            if isinstance(subschema.get('$ref'), str):
                subschema['$ref'] = resolve_schema_reference(subschema['$ref'],
                                                             subschema,
                                                             bblocks_register,
                                                             from_bblock,
                                                             from_document,
                                                             base_url)
            for v in subschema.values():
                walk(v)
        elif isinstance(subschema, list):
            for item in subschema:
                walk(item)
        return subschema

    return walk(schema)


def resolve_schema_reference(ref: str,
                             schema: Any,
                             bblocks_register: BuildingBlockRegister,
                             from_bblock: BuildingBlock,
                             from_document: PathOrUrl,
                             base_url: str | None = None) -> str:
    ref = schema.pop(BBLOCKS_REF_ANNOTATION, ref)

    if not ref or ref[0] == '#':
        # Local $ref -> returned as is
        return ref

    if '#' in ref:
        ref, fragment = ref.split('#', 1)
        fragment = '#' + fragment
    else:
        fragment = ''

    # Find bblock id for ref
    target_bblock_id = None
    if ref.startswith('bblocks://'):
        target_bblock_id = ref[len('bblocks://'):]
    elif not is_url(ref):
        # Relative ref -> search in local bblock schemas, both as .yaml and as .json

        if from_document.is_url:
            # Reference to a remote schema
            check_refs = {from_document.resolve_ref(ref).url}
        else:
            # Reference to a local schema (same repo)
            # First make ref relative to cwd
            rel_ref = str(os.path.relpath(from_bblock.files_path.joinpath(ref)))

            # Then check json/yaml variants
            check_refs = {rel_ref,
                          re.sub(r'\.json$', '.yaml', rel_ref),
                          re.sub(r'\.ya?ml', '.json', rel_ref)}
        for check_ref in check_refs:
            if check_ref in bblocks_register.local_bblock_files:
                target_bblock_id = bblocks_register.local_bblock_files[check_ref]
        if not target_bblock_id:
            if from_document.is_url:
                return f"{from_document.parent.resolve_ref(ref).url}{fragment}"
            elif base_url:
                # Return the URL to the $ref
                return f"{base_url}{os.path.relpath(str(from_document.parent.resolve_ref(ref).value))}{fragment}"
            else:
                # Relativize from annotated schema path
                # TODO: OpenAPI?
                return os.path.relpath(from_document.parent.resolve_ref(ref).resolve(),
                                       from_bblock.annotated_schema.parent) + fragment
    else:
        # URL -> search in both local and imported bblock schemas
        target_bblock_id = bblocks_register.local_bblock_files.get(
            ref,
            bblocks_register.imported_bblock_files.get(ref)
        )

    if target_bblock_id:
        # Search local
        target_bblock = bblocks_register.bblocks.get(target_bblock_id)
        if target_bblock:
            if target_bblock.schema.exists:
                target_doc = target_bblock.annotated_schema
            elif target_bblock.openapi.exists:
                target_doc = target_bblock.output_openapi
            else:
                raise ValueError(f"Unknown reference to {target_bblock_id} "
                                 f" from {from_bblock.identifier} ({from_document})"
                                 f" - target has no schema or OpenAPI document")
            if base_url:
                # If we have a base_url, we return the full URL
                return f"{base_url}{os.path.relpath(target_doc)}{fragment}"
            else:
                # Otherwise, the local relative path
                return os.path.relpath(target_doc, from_bblock.annotated_path) + fragment
        else:
            target_bblock = bblocks_register.imported_bblocks.get(target_bblock_id)
            if target_bblock:
                if target_bblock.get('schema'):
                    return f"{target_bblock['schema']['application/yaml']}{fragment}"
                if target_bblock.get('openAPIDocument'):
                    return target_bblock.get('openAPIDocument')

        raise ValueError(f'Error replacing dependency {target_bblock_id}'
                         f' from {from_bblock.identifier} ({from_document}). Is an import missing?')

    # If we're here, ref is unknown -> return the original value
    return f"{ref}{fragment}"
