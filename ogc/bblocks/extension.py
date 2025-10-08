import os
from dataclasses import dataclass, field
from typing import Callable, Any

from ogc.na.annotate_schema import SchemaResolver, ReferencedSchema

from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister


@dataclass
class SchemaNode:
    tag: str | None
    root: 'SchemaNode | None' = None
    preserve_branch = False
    parent: 'SchemaNode | None' = None
    is_properties: bool = False
    subschema: dict | list | None = None
    children: list['SchemaNode'] = field(default_factory=list)

    def mark_preserve_branch(self):
        n = self
        while n is not None:
            if n.preserve_branch:
                break
            n.preserve_branch = True
            n = n.parent


def process_extension(bblock: BuildingBlock, register: BuildingBlockRegister,
                      parent_id: str, extensions: dict[str, str],
                      ref_mapper: Callable[[str, Any], str] | None = None):

    schema_resolver = register.schema_resolver
    all_register_files = {
        **register.imported_bblock_files,
        **register.local_bblock_files,
    }

    if '#' in parent_id or any('#' in k or '#' in v for k, v in extensions.items()):
        raise ValueError('Extension points can only be declared for building blocks, not for fragments. '
                         'Please check that your extension point declarations contain no fragment identifiers ("#")')

    extension_target_schemas = {}
    for extension_bblock_id in extensions.values():
        local_bblock = register.bblocks.get(extension_bblock_id)
        if local_bblock:
            if local_bblock.annotated_schema:
                extension_target_schemas[extension_bblock_id] = os.path.relpath(
                    local_bblock.annotated_schema.resolve(),
                    bblock.annotated_path.resolve()
                )
            # TODO: OpenAPI?
        else:
            imported_bblock = register.imported_bblocks[extension_bblock_id]
            imported_bblock_schema = imported_bblock.get('schema', {}).get('application/yaml')
            if imported_bblock_schema:
                extension_target_schemas[extension_bblock_id] = imported_bblock_schema

            # TODO: OpenAPI?

    visited_refs = set()
    schema_branches: list[SchemaNode] = []

    def create_schema_node(parent_node: SchemaNode | None, tag: str, is_properties: bool = False,
                           subschema: dict | list | None = None) -> SchemaNode:
        if parent_node is None:
            node = SchemaNode(tag=tag, is_properties=is_properties, subschema=subschema)
            node.root = node
            schema_branches.append(node)
        else:
            node = SchemaNode(root=parent_node.root, parent=parent_node, tag=tag,
                              is_properties=is_properties, subschema=subschema)
            parent_node.children.append(node)
        return node

    def walk_subschema(subschema, from_schema: ReferencedSchema, parent_node: SchemaNode | None):
        if not subschema or not isinstance(subschema, dict):
            return

        if '$ref' in subschema:
            if ref_mapper:
                subschema['$ref'] = ref_mapper(subschema['$ref'], subschema)
            target_schema = schema_resolver.resolve_schema(subschema['$ref'], from_schema)

            extension_target: str | None = None
            extension_source: str | None = None
            extension_target_id : str | None = None
            if (target_schema.location in all_register_files
                and all_register_files[target_schema.location] in extensions):
                extension_source = all_register_files[target_schema.location]
                extension_target_id = extensions[extension_source]
                extension_target = extension_target_schemas.get(extension_target_id)
                if not extension_target:
                    raise ValueError(f'No schema could be found for extension target {extension_target_id}')

            if extension_target:
                ref_node = create_schema_node(parent_node, '$ref')
                ref_node.subschema = {
                    '$ref': str(extension_target),
                    'x-bblocks-extension-source': extension_source,
                    'x-bblocks-extension-target': extension_target_id,
                }
                ref_node.mark_preserve_branch()
                return
            else:
                # Avoid infinite loops
                target_schema_full_ref = (f"{target_schema.location}#{target_schema.fragment}"
                                          if target_schema.fragment
                                          else target_schema.location)
                if target_schema_full_ref in visited_refs:
                    return
                visited_refs.add(target_schema_full_ref)

                if target_schema:
                    walk_subschema(target_schema.subschema, target_schema, parent_node)

        for p in ('oneOf', 'allOf', 'anyOf'):
            collection = subschema.get(p)
            if collection and isinstance(collection, list):
                col_node = (create_schema_node(parent_node, p, subschema=collection)
                            if p != 'allOf' else parent_node)
                for entry in collection:
                    walk_subschema(entry, from_schema, col_node)

        for i in ('prefixItems', 'items', 'contains', 'then', 'else', 'additionalProperties'):
            l = subschema.get(i)
            if isinstance(l, dict):
                entry_node = create_schema_node(parent_node, i, subschema=l)
                walk_subschema(l, from_schema, entry_node)

        if 'properties' in subschema:
            properties_node = create_schema_node(parent_node, tag='properties')
            for prop_name, prop_schema in subschema['properties'].items():
                prop_node = create_schema_node(parent_node=properties_node, tag=prop_name,
                                               is_properties=True, subschema=prop_schema)
                walk_subschema(prop_schema, from_schema, prop_node)

        pattern_properties = subschema.get('patternProperties')
        if pattern_properties:
            pps_node = create_schema_node(parent_node, tag='patternProperties')
            for pp_k, pp in pattern_properties.items():
                if isinstance(pp, dict):
                    pp_node = create_schema_node(pps_node, pp_k, is_properties=True)
                    walk_subschema(pp, from_schema, pp_node)

    parent_bblock = register.bblocks.get(parent_id)
    if parent_bblock:
        root_schema = schema_resolver.resolve_schema(parent_bblock.annotated_schema)
    else:
        imp_bblock = register.imported_bblocks.get(parent_id)
        if not imp_bblock:
            raise ValueError(f"Could not find building block with id {parent_id} in register or imports.")
        bblock_schemas = imp_bblock.get('schema', {})
        bblock_schema = bblock_schemas.get('application/yaml', bblock_schemas.get('application/json'))
        # TODO: OpenAPI?
        if not bblock_schema:
            raise ValueError(f"Could not find schema for building block with id {parent_id}"
                             f" in register or imports.")
        root_schema = schema_resolver.resolve_schema(bblock_schema)

    walk_subschema(root_schema.full_contents, root_schema, None)

    output_schema = {
        '$schema': 'https://json-schema.org/draft/2020-12/schema',
        'x-bblocks-extends': parent_id,
        'x-bblocks-extensions': extensions,
        'allOf': [],
    }
    for branch in schema_branches:
        if not branch.preserve_branch:
            continue

        def walk_branch(node: SchemaNode, parent_schema: dict, force_preserve_branch: bool = False):
            if not force_preserve_branch and not node.preserve_branch:
                return
            if node.tag == '$ref' and node.subschema:
                parent_schema.setdefault('allOf', []).append(node.subschema)
            elif node.tag in ('oneOf', 'anyOf', 'allOf'):
                col_schema = parent_schema.setdefault(node.tag, [])
                for child in node.children:
                    child_schema = {}
                    col_schema.append(child_schema)
                    walk_branch(child, child_schema, force_preserve_branch=node.tag in ('oneOf', 'anyOf'))
            else:
                if not node.children:
                    # End of the line, we append the full subschema
                    parent_schema[node.tag] = node.subschema
                else:
                    parent_schema[node.tag] = {}
                    for child in node.children:
                        walk_branch(child, parent_schema[node.tag], force_preserve_branch=force_preserve_branch)

        branch_entry = {}
        output_schema['allOf'].append(branch_entry)
        walk_branch(branch, branch_entry)

    return output_schema
