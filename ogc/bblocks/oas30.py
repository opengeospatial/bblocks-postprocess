#!/usr/bin/env python3
from __future__ import annotations

import os
import re
from collections import deque
from pathlib import Path
from urllib.parse import urljoin

from ogc.na.util import is_url, load_yaml
from ogc.bblocks.util import load_file_cached

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ogc.bblocks.models import BuildingBlockRegister


def apply_schema_fixes(parent):
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
                one_of = {'anyOf': [{'type': v} for v in prop_val]}
                if 'anyOf' in parent:
                    parent.setdefault('allOf', []).append(one_of)
                else:
                    parent['anyOf'] = one_of

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


def process_schema(schema_fn: Path, schema_url: str, bbr: BuildingBlockRegister,
                   components_path: str = 'x-defs') -> dict:
    mapped_refs: dict[str | Path, str] = {}
    used_refs: set[str] = set()
    pending_schemas: deque[str | Path] = deque()

    def get_ref_mapping(schema_id: str | Path, ref: str) -> str:
        ref_parts = ref.split('#', 1)
        ref_base = ref_parts[0]
        ref_fragment = ref_parts[1] if len(ref_parts) > 1 else ''

        if not ref_base:
            ref_base = schema_id
        if not is_url(ref):
            if isinstance(schema_id, Path):
                ref_base = schema_id.parent.joinpath(ref_base).resolve()
            else:
                ref_base = urljoin(schema_id, ref_base)

        existing = mapped_refs.get(ref_base)
        if existing:
            return f"{existing}{ref_fragment}"

        new_mapping = None
        if isinstance(ref_base, Path):
            new_mapping = bbr.local_bblock_files.get(str(ref_base))
            if not new_mapping:
                new_mapping = bbr.local_bblock_files.get(str(ref_base.resolve()))
        elif ref_base in bbr.imported_bblock_files:
            new_mapping = bbr.imported_bblock_files[ref_base]

        if not new_mapping:
            new_mapping = re.sub(r'^https?://', '', str(ref_base))
            new_mapping = re.sub(r'[^a-zA-Z0-9:_~@.-]+', '_', new_mapping)

        if not re.match(r'^[a-zA-Z_]', new_mapping):
            new_mapping = '_' + new_mapping

        while new_mapping in used_refs:
            new_mapping += '_'
        used_refs.add(new_mapping)
        pending_schemas.append(ref_base)
        mapped_refs[ref_base] = new_mapping
        return f"{new_mapping}{ref_fragment}"

    def walk(subschema: dict | list, schema_id: str | Path | None, is_properties: bool = False) \
            -> tuple[dict | list, str | None, str | Path]:
        schema_version = None
        if isinstance(subschema, list):
            for item in subschema:
                walk(item, schema_id)
        elif isinstance(subschema, dict):

            if not is_properties:
                apply_schema_fixes(subschema)

            schema_version = subschema.pop('$schema', None)
            schema_declared_id = subschema.pop('$id', None)
            if schema_declared_id:
                schema_id = schema_declared_id
            if isinstance(schema_id, (str, Path)) and isinstance(subschema.get('$ref'), str):

                ref = f"{schema_url}#/{components_path}/{get_ref_mapping(schema_id, subschema.pop('$ref'))}"

                if not subschema:
                    subschema['$ref'] = ref
                else:
                    all_of = subschema.setdefault('allOf', [])
                    moved = {}
                    for k in list(subschema.keys()):
                        moved[k] = subschema.pop(k)
                        walk(moved[k], schema_id, not is_properties and k == 'properties')
                    all_of.append(moved)
                    all_of.append({'$ref': ref})
            else:
                for k, v in subschema.items():
                    walk(v, schema_id, not is_properties and k == 'properties')
        return subschema, schema_version, schema_id

    schema_fn = schema_fn.resolve()
    root_ref_id = get_ref_mapping(schema_fn, '')
    mapped_refs[schema_url] = root_ref_id
    root_defs = {}

    while pending_schemas:
        cur_ref = pending_schemas.popleft()
        cur_ref_id = mapped_refs[cur_ref]
        if root_ref_id == cur_ref_id:
            ref_schema = load_yaml(content=load_file_cached(schema_fn))
        elif cur_ref in bbr.local_bblock_files:
            ref_schema = load_yaml(content=bbr.bblocks[bbr.local_bblock_files[cur_ref]].annotated_schema_contents)
        else:
            ref_schema = load_yaml(content=load_file_cached(cur_ref))
        ref_schema, ref_version, ref_id = walk(ref_schema, cur_ref)

        if ref_version:
            ref_schema['x-schema-version'] = ref_version

        if root_ref_id == cur_ref_id:
            ref_id = schema_url
        elif isinstance(ref_id, Path):
            ref_id = urljoin(schema_url, os.path.relpath(ref_id, schema_fn))
        ref_schema['x-schema-source'] = ref_id

        root_defs[cur_ref_id] = ref_schema

    return {
        components_path: root_defs,
        'allOf': [
            {'$ref': f"{schema_url}#/{components_path}/{root_ref_id}"}
        ]
    }
