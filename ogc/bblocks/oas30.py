#!/usr/bin/env python3
from __future__ import annotations

import logging
import re
from builtins import isinstance
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Unpack
from urllib.parse import urlparse

from jsonpointer import resolve_pointer
from ogc.na.util import is_url, load_yaml

from ogc.bblocks.util import load_file_cached, PathOrUrl

if TYPE_CHECKING:
    from ogc.bblocks.models import BuildingBlockRegister

OAS_OPERATION_KEYS = [
    'get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace',
]
OAS_REF_OBJECT_KEYS = ['$ref', 'summary', 'description']
X_RESOLVED_REF = 'x-resolved-ref'
DOWNCOMPILED_MARKER = '__DOWNCOMPILED__'


def walk_schema(schema: dict[str, Any], fn: Callable[[Any, Unpack], dict | bool | None],
                **kwargs):

    def walk(subschema, **inner_kwargs):
        new_kwargs = fn(subschema, **inner_kwargs)
        if new_kwargs is False:
            return
        elif new_kwargs is None:
            new_kwargs = {}
        if isinstance(subschema, dict):
            for k, v in subschema.items():
                walk(v, property=k, parent=subschema, **{**kwargs, **new_kwargs})
        elif isinstance(subschema, list):
            for v in subschema:
                walk(v, parent=subschema, **{**kwargs, **new_kwargs})

    walk(schema, **kwargs)


def deep_extract_refs(root_schema: dict | str | Path,
                      root_schema_location: PathOrUrl | None = None,
                      bbr: BuildingBlockRegister | None = None,
                      fix_schemas=True,
                      add_annotations=True) -> tuple[dict[str | Path, list[dict]], dict[str | Path, dict | str]]:
    # Map of found $ref's and a list of the objects ({"$ref": key}) pointing to them
    refs: dict[str | Path, list[dict]] = {}

    # Map of processed schemas. If value is str, then it is a reference to another key
    schemas: dict[str | Path, dict | str] = {}

    # Queue of schemas pending processing
    pending_schemas = deque((root_schema,))

    cwd = PathOrUrl(Path())

    def walk(subschema: dict | str | Path, from_location: PathOrUrl | None = None,
             is_properties=False):
        from_location = from_location or root_schema_location or cwd

        # If processing reference, load it
        if not isinstance(subschema, dict):
            schema_location = subschema

            if str(schema_location) in schemas:
                # Do not process schemas more than once
                return

            if bbr and schema_location in bbr.local_bblock_files:
                # If local schema, use annotated_schema_contents
                bblock = bbr.bblocks[bbr.local_bblock_files[schema_location]]
                subschema = load_yaml(
                    content=bblock.annotated_schema_contents
                )
                extension = re.sub(r'.*\.', '', schema_location)
                schema_location = bbr.get_url(bblock.annotated_schema.with_suffix('.' + extension))
            else:
                logging.debug('Loading schema from %s', schema_location)
                subschema = load_yaml(content=load_file_cached(schema_location))

            if schemas:
                if add_annotations:
                    subschema['x-schema-source'] = str(schema_location)
                # Only change from_location if this is not the root schema
                from_location = PathOrUrl(schema_location)

            # Store cached schema
            schemas[str(from_location)] = subschema
        elif not schemas:
            schemas[str(root_schema_location)] = subschema

        for protected_prop, map_to_prop in {'id': 'id', 'schema': 'version'}.items():
            prop_value = subschema.pop('$' + protected_prop, None)
            if prop_value and add_annotations:
                subschema['x-schema-' + map_to_prop] = prop_value

        # Process potential $ref's
        ref = subschema.get('$ref')

        if fix_schemas:
            # If we have a $ref plus other properties, move $ref into an allOf
            if isinstance(ref, str) and len(subschema.keys()) > 1:
                # If an allOf already exists, reuse that one
                subschema.setdefault('allOf', []).append({'$ref': ref})
                subschema.pop('$ref')
                ref = None

            if not is_properties:
                apply_oas30_property_fixes(subschema)

        if isinstance(ref, str):
            ref_base = ref.split('#', 1)[0]
            same_doc_ref = False

            if not ref_base:
                # same-document ref
                if from_location:
                    ref_base = str(from_location)
                    same_doc_ref = True
                else:
                    raise ValueError(f"Found same-document $ref to {ref}, but no base URL"
                                     f" can be found for resolution")
            elif not is_url(ref_base):
                # Relative ref -> resolve
                ref_base = str(from_location.resolve_ref(ref_base))

            existing_refs = refs.setdefault(ref_base, [])
            if not same_doc_ref and not existing_refs:
                pending_schemas.append(ref_base)
            existing_refs.append(subschema)

        # Apply recursively
        for k, v in subschema.items():
            if isinstance(v, list):
                for item in v:
                    if isinstance(item, dict):
                        walk(item, from_location)
            elif isinstance(v, dict):
                walk(v, from_location, is_properties=not is_properties and k == 'properties')

    while pending_schemas:
        walk(pending_schemas.popleft(), root_schema_location)

    return refs, schemas


def guess_def_name(ref: str | Path | PathOrUrl, bbr: BuildingBlockRegister):
    ref_str = re.sub(r'/*(#.*)?$', '', str(ref))

    def clean(s: str):
        return re.sub(r'[^a-zA-Z0-9_.-]+', '_', s)

    if bbr:
        bblock_id = bbr.local_bblock_files.get(ref_str)
        if not bblock_id:
            bblock_id = bbr.imported_bblock_files.get(ref_str)
        if bblock_id:
            return bblock_id

    if is_url(ref):
        parsed_url = urlparse(ref)
        if parsed_url.path:
            path = Path(parsed_url.path).with_suffix('')

            if path.stem.lower() not in ('schema', 'openapi',):
                return clean(path.stem)

            if len(path.parts) == 2:
                # File at root directory
                return parsed_url.hostname

            return '_'.join(clean(p) for p in path.parts[1:])

    return clean(ref)


def apply_oas30_property_fixes(parent: dict[str, Any]):
    parent.pop('unevaluatedProperties', None)
    const = parent.pop('const', None)
    if const:
        parent['enum'] = [const]
    if 'type' in parent:
        if parent['type'] == 'null':
            del parent['type']
            parent['nullable'] = True
        elif isinstance(parent['type'], list):
            prop_val = parent['type']
            if 'null' in prop_val:
                parent['nullable'] = True
                prop_val.remove('null')
            if not prop_val:
                del parent['type']
            elif len(prop_val) == 1:
                parent['type'] = prop_val[0]
            else:
                any_of_types = [{'type': v} for v in prop_val]
                if 'anyOf' in parent:
                    parent.setdefault('allOf', []).extend(({'anyOf': parent.pop('anyOf')}, any_of_types))
                else:
                    parent['anyOf'] = any_of_types
                parent.pop('type', None)

    if 'oneOf' in parent:
        add_nullable = False
        for_deletion = []
        for oo in parent['oneOf']:
            if oo.get('type') == 'null':
                del oo['type']
                add_nullable = True
                if not oo:
                    for_deletion.append(oo)
        for item in for_deletion:
            parent['oneOf'].remove(item)
        if not parent['oneOf']:
            del parent['oneOf']
        if add_nullable:
            parent['nullable'] = True


def apply_oas30_schema_fixes(schema: dict[str, Any]):

    def fn(subschema: dict[str, Any], is_properties=False, property=None, **kwargs):
        if isinstance(subschema, dict):
            ref = subschema.get('$ref')
            if isinstance(ref, str) and len(subschema.keys()) > 1:
                subschema.setdefault('allOf', []).append({'$ref': ref})
                subschema.pop('$ref')
            if is_properties:
                apply_oas30_property_fixes(subschema)
        return {
            'is_properties': not is_properties and property == 'properties'
        }

    walk_schema(schema, fn)
    return schema


def schema_to_oas30(schema_fn: Path, schema_url: str, bbr: BuildingBlockRegister | None,
                    defs_path: str = '/x-defs'):
    refs, schemas = deep_extract_refs(schema_fn, PathOrUrl(schema_url), bbr, fix_schemas=True)

    if not refs:
        return

    root_schema = schemas[schema_url]

    while not isinstance(root_schema, dict):
        root_schema = schemas[root_schema]
    defs = {}

    for ref, ref_objects in refs.items():

        schema = None
        def_key = None
        ref_to_root = ref == schema_url
        if not ref_to_root:
            schema = schemas[ref]
            # Potentially resolve internal references
            while not isinstance(schema, dict):
                ref_to_root = schema == schema_url
                if ref_to_root:
                    break
                schema = schemas[schema]

        if not ref_to_root:
            def_key = guess_def_name(ref, bbr)
            if def_key in defs:
                base_def_key = def_key
                i = 0
                while def_key in defs:
                    i += 1
                    def_key = f"{base_def_key}_{i}"

            defs[def_key] = schema

        for ref_object in ref_objects:
            fragment = ''
            if '#' in ref_object['$ref']:
                fragment = ref_object['$ref'].split('#', 1)[1]
            if ref_to_root:
                ref_object['$ref'] = f"{schema_url}{'#' + fragment if fragment else ''}"
            else:
                ref_object['$ref'] = f"{schema_url}#{defs_path}/{def_key}{fragment}"

    return {
        'x-defs': defs,
        **root_schema,
    }


def oas31_to_oas30(document: dict, document_location: PathOrUrl | str, bbr: BuildingBlockRegister | None,
                   x_defs_path='/x-defs', target_version='3.0.3'):

    # == 1. Bundle
    if x_defs_path[0] != '/':
        x_defs_path = '/' + x_defs_path
    if x_defs_path[-1] == '/':
        x_defs_path = x_defs_path[:-1]

    if not isinstance(document_location, PathOrUrl):
        document_location = PathOrUrl(document_location)

    all_refs, all_schemas = deep_extract_refs(document, document_location, bbr, fix_schemas=False,
                                              add_annotations=False)

    root_schema = all_schemas.pop(str(document_location))

    def sub_in_place(ref_object, schema):
        ref = ref_object.pop('$ref')
        if '#' in ref:
            # process fragment
            fragment = ref.split('#', 1)
            schema = resolve_pointer(schema, fragment)
        ref_object.update(schema)

    # refs to root document -> convert to local
    refs_to_root = all_refs.pop(str(document_location), None)
    if refs_to_root:
        for ref_to_root in refs_to_root:
            ref_to_root['$ref'] = re.sub(r'[^#]+', '', ref_to_root['$ref'])

    x_defs = {}
    refs_to_xdefs = {}
    for ref_doc, found_refs in all_refs.items():

        referenced_schema = all_schemas.pop(ref_doc)

        if len(found_refs) == 1:
            # only one reference -> substitute in place
            sub_in_place(found_refs[0], referenced_schema)

        else:
            # several references -> put in x-defs
            x_def_key = guess_def_name(ref_doc, bbr)
            if x_def_key in x_defs:
                base_def_key = x_def_key
                i = 0
                while x_def_key in x_defs:
                    i += 1
                    x_def_key = f"{base_def_key}_{i}"

            x_defs[x_def_key] = referenced_schema

            for ref_object in found_refs:
                parts = ref_object['$ref'].split('#', 1)
                fragment = parts[1] if len(parts) == 2 else ''
                if fragment and fragment[0] != '/':
                    raise ValueError(f'Found invalid fragment in $ref: {fragment}')
                ref_object['$ref'] = f"#{x_defs_path}/{x_def_key}{fragment}"
                refs_to_xdefs.setdefault(f"#{x_defs_path}/{x_def_key}", []).append(ref_object)

    if x_defs:
        root_schema['x-defs'] = x_defs

    # == 2. Fix schemas

    processed_schemas = []

    def resolve_parameter(p: dict):
        ref = p.pop('$ref', None)
        if ref:
            param = resolve_pointer(document, ref[1:])
            p.update(param)

    def process_schema_object(o: dict, raw_schema=False):
        if not o:
            return
        if not raw_schema:
            o = o.get('schema')
        if not o:
            return

        def update_ref_fn(r: dict[str, Any], old_ref, new_component_ref, **kwargs):
            if isinstance(r, dict):
                if '$ref' in r:
                    r['$ref'] = r['$ref'].replace(old_ref, new_component_ref)

        ref = o.get('$ref')
        if ref and ref.startswith(f"#{x_defs_path}"):
            # move x-defs object to #/components/schemas
            ref_parts = ref.split('/', 3)
            x_def_key = ref_parts[2]
            old_ref = f"#{x_defs_path}/{x_def_key}"
            new_component_ref = f"#/components/schemas/{x_def_key}"
            root_schema['components']['schemas'][x_def_key] = resolve_pointer(root_schema, x_defs_path).pop(x_def_key)
            for old_schema_ref in refs_to_xdefs[old_ref]:
                walk_schema(old_schema_ref, update_ref_fn, old_ref=old_ref, new_component_ref=new_component_ref)

        def fn(subschema: dict[str, Any], is_properties=False, property=None, **kwargs):
            if isinstance(subschema, dict):
                # remove $comment to avoid errors
                comment = subschema.pop('$comment', None)

                ref = subschema.get('$ref')
                if isinstance(ref, str):
                    pending_schemas.append(resolve_pointer(document, ref[1:]))
                    if len(subschema.keys()) > 1:
                        subschema.setdefault('allOf', []).append({'$ref': ref})
                        subschema.pop('$ref')
                if is_properties:
                    apply_oas30_property_fixes(subschema)
            return {
                'is_properties': not is_properties and property == 'properties'
            }

        pending_schemas = deque((o,))
        while pending_schemas:
            pending_schema = pending_schemas.pop()
            if pending_schema.get(DOWNCOMPILED_MARKER):
                continue
            walk_schema(pending_schema, fn)
            pending_schema[DOWNCOMPILED_MARKER] = True
            processed_schemas.append(pending_schema)

    def process_path_item_object(o: dict):
        if not o:
            return
        for op_key in OAS_OPERATION_KEYS:
            operation = o.get(op_key)
            if operation:
                process_operation_object(operation)
        for parameter in o.get('parameters', []):
            resolve_parameter(parameter)
            process_schema_object(parameter)
            process_content_object(parameter)

    def process_operation_object(o: dict):
        if not o:
            return
        for parameter in o.get('parameters', []):
            resolve_parameter(parameter)
            process_schema_object(parameter)
            process_content_object(parameter)

        process_content_object(o.get('requestBody'))

        for response in o.get('responses', {}).values():
            process_content_object(response)
            for header in response.get('headers', {}).values():
                process_schema_object(header)

        for callback in o.get('callbacks', {}).values():
            process_operation_object(callback)

    def process_content_object(o: dict):
        if not o:
            return
        content = o.get('content')
        if content:
            for schema_object in content.values():
                # remove description because 3.0 doesn't support it
                schema_object.pop('description', None)
                process_schema_object(schema_object)

    def process_document():
        components = root_schema.setdefault('components', {})
        components.setdefault('schemas', {})
        if components:
            for schema in components.get('schemas'):
                process_schema_object(schema, raw_schema=True)
            for parameter in components.get('parameters', {}).values():
                resolve_parameter(parameter)
                process_schema_object(parameter)
                process_content_object(parameter)
            for response in components.get('responses', {}).values():
                process_content_object(response)
                for header in response.get('headers', {}).values():
                    process_schema_object(header)
            for request_body in components.get('requestBodies', {}):
                process_content_object(request_body)
            for header in components.get('headers', {}).values():
                process_schema_object(header)
            for callback in components.get('callbacks', {}).values():
                process_operation_object(callback)
            for path in components.get('pathItems', {}).values():
                process_path_item_object(path)

        for prop in ('paths', 'webhooks'):
            entry = document.get(prop)
            if entry:
                for path_key, path_item in entry.items():
                    process_path_item_object(path_item)

    process_document()

    for processed_schema in processed_schemas:
        processed_schema.pop(DOWNCOMPILED_MARKER, None)

    if not root_schema['components']['schemas']:
        root_schema['components'].pop('schemas', None)

    if not root_schema['components']:
        root_schema.pop('components', None)

    root_schema['openapi'] = target_version

    return root_schema
