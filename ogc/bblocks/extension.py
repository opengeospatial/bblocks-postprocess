import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Any, cast
from urllib.parse import urljoin

from ogc.na.annotate_schema import ReferencedSchema
from ogc.na.util import is_url

from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister

# All JSON Schema keywords
JSON_SCHEMA_ALL_KEYWORDS = {'$anchor', '$comment', '$defs', '$dynamicAnchor', '$dynamicRef', '$id', '$ref', '$schema',
                            '$vocabulary', 'additionalProperties', 'allOf', 'anyOf', 'const', 'contains',
                            'contentEncoding', 'contentMediaType', 'contentSchema', 'default', 'dependentRequired',
                            'dependentSchemas', 'deprecated', 'description', 'else', 'enum', 'examples',
                            'exclusiveMaximum', 'exclusiveMinimum', 'format', 'format', 'if', 'items', 'maxContains',
                            'maximum', 'maxItems', 'maxLength', 'maxProperties', 'minContains', 'minimum', 'minItems',
                            'minLength', 'minProperties', 'multipleOf', 'not', 'oneOf', 'pattern', 'patternProperties',
                            'prefixItems', 'properties', 'propertyNames', 'readOnly', 'required', 'then', 'title',
                            'type', 'unevaluatedItems', 'unevaluatedProperties', 'uniqueItems', 'writeOnly'}
# Metadata annotations
JSON_SCHEMA_METADATA_KEYWORDS = {'$anchor', '$comment', '$defs', '$dynamicAnchor', '$dynamicRef', '$id', '$schema',
                                 '$vocabulary', 'description', 'else', 'examples', 'readOnly', 'title', 'writeOnly'}
# Keywords used for alias detection
JSON_SCHEMA_ALIAS_KEYWORDS = {'$ref', 'oneOf', 'allOf', 'anyOf'}
# Keywords that abort alias detection
JSON_SCHEMA_ALIAS_ABORT = JSON_SCHEMA_ALL_KEYWORDS - JSON_SCHEMA_METADATA_KEYWORDS - JSON_SCHEMA_ALIAS_KEYWORDS

logger = logging.getLogger(__name__)


@dataclass
class SchemaNode:
    tag: str | None
    from_schema: ReferencedSchema
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

    def __str__(self):
        return (f"<{self.tag}{' ref=' + self.subschema.get('$ref') if self.tag == '$ref' else ''}"
                f" schema={self.from_schema.location}"
                f"{'#' + self.from_schema.fragment if self.from_schema.fragment else ''}>"
                f" preserve={self.preserve_branch}{' properties ' if self.is_properties else ''}>")


class Extender:

    def __init__(self, register: BuildingBlockRegister,
                 ref_mapper: Callable[[str, Any], str] | None = None):
        self.register = register
        self.base_url = register.base_url
        self.ref_mapper = ref_mapper
        self.schema_resolver = register.schema_resolver

    def process_extensions(self, bblock: BuildingBlock):

        parent_id = bblock.extensionPoints['baseBuildingBlock']
        extensions = bblock.extensionPoints['extensions']

        register = self.register
        schema_resolver = self.schema_resolver

        if '#' in parent_id or any('#' in k or '#' in v for k, v in extensions.items()):
            raise ValueError('Extension points can only be declared for building blocks, not for fragments. '
                             'Please check that your extension point declarations contain no fragment '
                             'identifiers ("#")')

        parent_bblock = register.bblocks.get(parent_id)

        parent_is_openapi = False
        if parent_bblock:
            bblock.metadata['itemClass'] = parent_bblock.itemClass
            if parent_bblock.annotated_schema.is_file():
                root_schema = schema_resolver.resolve_schema(parent_bblock.annotated_schema)
            elif parent_bblock.output_openapi.is_file():
                root_schema = schema_resolver.resolve_schema(parent_bblock.output_openapi)
                parent_is_openapi = True
            else:
                raise ValueError(f'Could not find schema or OpenAPI document for '
                                 f'parent building block {parent_bblock.identifier}')
        else:
            imp_bblock = register.imported_bblocks.get(parent_id)
            if not imp_bblock:
                raise ValueError(f"Could not find building block with id {parent_id} in register or imports.")
            bblock.metadata['itemClass'] = imp_bblock['itemClass']
            bblock_schemas = imp_bblock.get('schema', {})
            bblock_schema = bblock_schemas.get('application/yaml', bblock_schemas.get('application/json'))
            if not bblock_schema and (bblock_openapi := imp_bblock.get('openAPIDocument')):
                bblock_schema = bblock_openapi
                parent_is_openapi = True
            if not bblock_schema:
                raise ValueError(f"Could not find schema for building block with id {parent_id}"
                                 f" in register or imports.")
            root_schema = schema_resolver.resolve_schema(bblock_schema)

        extension_schema_mappings: dict[str, dict] = {}
        for extension_source_id, extension_target_id in extensions.items():
            source_bblock = register.bblocks.get(extension_source_id)
            target_bblock = register.bblocks.get(extension_target_id)

            target_bblock_schema = None
            if target_bblock:
                # local
                if target_bblock.annotated_schema.is_file():
                    if register.base_url:
                        target_bblock_schema = urljoin(register.base_url,
                                                       str(os.path.relpath(target_bblock.annotated_schema)))
                    else:
                        target_bblock_schema = os.path.relpath(
                            target_bblock.annotated_schema.resolve(),
                            bblock.annotated_path.resolve()
                        )
            else:
                # remote
                target_bblock = register.imported_bblocks[extension_target_id]
                target_bblock_schema = target_bblock.get('schema', {}).get('application/yaml')

            if not target_bblock_schema:
                raise ValueError(f'No schema was found for extension target {extension_target_id}. '
                                 f'Only building blocks with schemas are supported for extensions.')

            source_bblock_schema = None
            if source_bblock:
                # local
                if source_bblock.annotated_schema.exists():
                    if register.base_url:
                        source_bblock_schema = urljoin(register.base_url,
                                                       str(os.path.relpath(source_bblock.annotated_schema)))
                    else:
                        source_bblock_schema = source_bblock.annotated_schema.resolve()
            else:
                # remote
                source_bblock = register.imported_bblocks[extension_source_id]
                source_bblock_schema = source_bblock.get('schema', {}).get('application/yaml')

            if not source_bblock_schema:
                raise ValueError(f'No schema was found for extension source {extension_target_id}. '
                                 f'Only building blocks with schemas are supported for extensions.')

            extension_schema_mappings[source_bblock_schema] = {
                'extension_source_id': extension_source_id,
                'extension_target_id': extension_target_id,
                'extension_target_ref': target_bblock_schema,
            }

            source_bblock_resolved_schema = schema_resolver.resolve_schema(source_bblock_schema)
            extension_schema_mappings.update(self.extract_aliases(source_bblock_resolved_schema, extension_source_id,
                                                                  extension_target_id, target_bblock_schema))

        result = (self._process_openapi()
                  if parent_is_openapi
                  else self._process_schema(bblock, root_schema, parent_id, extensions, extension_schema_mappings))
        return result, parent_is_openapi

    def _process_openapi(self):
        raise ValueError('Support for extending OpenAPI documents is not yet implemented.')

    def _process_schema(self, bblock: BuildingBlock, root_schema: ReferencedSchema, parent_id: str,
                        extensions: dict[str, str], extension_schema_mappings: dict[str, dict[str, str]]):
        schema_resolver = self.schema_resolver
        visited_refs = {}
        schema_branches: list[SchemaNode] = []

        def create_schema_node(parent_node: SchemaNode | None, tag: str, from_schema: ReferencedSchema,
                               is_properties: bool = False, subschema: dict | list | None = None) -> SchemaNode:
            if parent_node is None:
                node = SchemaNode(tag=tag, from_schema=from_schema, is_properties=is_properties, subschema=subschema)
                node.root = node
                schema_branches.append(node)
            else:
                node = SchemaNode(root=parent_node.root, parent=parent_node, tag=tag, from_schema=from_schema,
                                  is_properties=is_properties, subschema=subschema)
                parent_node.children.append(node)
            return node

        def get_ref(schema: ReferencedSchema):
            full_ref = schema.location
            if isinstance(schema.location, Path):
                full_ref = schema.location.resolve()
                if self.base_url:
                    full_ref = urljoin(self.base_url,
                                       os.path.relpath(full_ref))
            if schema.fragment:
                full_ref += '#' + schema.fragment
            return full_ref

        def walk_subschema(subschema, from_schema: ReferencedSchema, parent_node: SchemaNode | None):
            if not subschema or not isinstance(subschema, dict):
                return

            if '$ref' in subschema:
                ref = subschema.pop('$ref')
                if self.ref_mapper:
                    ref = self.ref_mapper(ref, subschema)
                target_schema = schema_resolver.resolve_schema(ref, from_schema, return_none_on_loop=False)
                target_schema_full_ref = get_ref(target_schema)

                extension_target: dict | None = extension_schema_mappings.get(target_schema_full_ref)

                skip_node = False
                if extension_target:
                    # Search up the chain of allOf/anyOf/oneOf and see if there's a reference to the same
                    # schema. This can happen when there is a top-level single-entry allOf/anyOf/oneOf in
                    # the schema.
                    pn = parent_node
                    while pn:
                        if pn.tag == '$ref':
                            if pn.subschema.get('x-bblocks-extension-source'):
                                skip_node = True
                            else:
                                # undetected alias found in another schema
                                undetected_alias = schema_resolver.resolve_schema(cast(dict, pn.subschema)['$ref'],
                                                                                  pn.from_schema)
                                extension_schema_mappings[get_ref(undetected_alias)] = extension_target
                        elif pn.tag != '[]' and (
                                pn.tag not in ('oneOf', 'allOf', 'anyOf', '[]') or len(pn.children) > 1):
                            break
                        pn = pn.parent

                if skip_node:
                    ref_node = parent_node
                else:
                    ref_node = create_schema_node(parent_node, '$ref', from_schema,
                                                  subschema={'$ref': extension_target['extension_target_ref']
                                                  if extension_target else ref})
                    if extension_target:
                        ref_node.subschema.update({
                            'x-bblocks-extension-source': extension_target['extension_source_id'],
                            'x-bblocks-extension-target': extension_target['extension_target_id'],
                        })
                        ref_node.mark_preserve_branch()

                # Avoid infinite loops
                target_schema_full_ref = (f"{target_schema.location}#{target_schema.fragment}"
                                          if target_schema.fragment
                                          else target_schema.location)
                if target_schema_full_ref in visited_refs:
                    return

                visited_refs[target_schema_full_ref] = ref_node

                if target_schema:
                    walk_subschema(target_schema.subschema, target_schema, ref_node)

            for p in ('oneOf', 'allOf', 'anyOf'):
                collection = subschema.pop(p, None)
                if collection and isinstance(collection, list):
                    # if len(collection) == 1:
                    #    walk_subschema(collection[0], from_schema, parent_node)
                    # else:
                    col_node = create_schema_node(parent_node, p, from_schema, subschema=collection)
                    for entry in collection:
                        entry_node = create_schema_node(col_node, '[]', from_schema, subschema=entry)
                        walk_subschema(entry, from_schema, entry_node)

            for i in ('prefixItems', 'items', 'contains', 'then', 'else', 'additionalProperties'):
                l = subschema.pop(i, None)
                if isinstance(l, dict):
                    entry_node = create_schema_node(parent_node, i, from_schema, subschema=l)
                    walk_subschema(l, from_schema, entry_node)

            if 'properties' in subschema:
                properties_node = create_schema_node(parent_node, tag='properties', from_schema=from_schema)
                for prop_name, prop_schema in subschema.pop('properties').items():
                    prop_node = create_schema_node(parent_node=properties_node, tag=prop_name,
                                                   from_schema=from_schema,
                                                   is_properties=True, subschema=prop_schema)
                    walk_subschema(prop_schema, from_schema, prop_node)

            pattern_properties: dict | None = subschema.pop('patternProperties', None)
            if pattern_properties:
                pps_node = create_schema_node(parent_node, tag='patternProperties', from_schema=from_schema)
                for pp_k, pp in pattern_properties.items():
                    if isinstance(pp, dict):
                        pp_node = create_schema_node(pps_node, pp_k, from_schema=from_schema, is_properties=True)
                        walk_subschema(pp, from_schema, pp_node)

        walk_subschema(root_schema.full_contents, root_schema, None)

        root_schema_location = root_schema.location
        if isinstance(root_schema_location, Path):
            root_schema_location = os.path.relpath(root_schema_location.resolve(), bblock.annotated_path.resolve())

        output_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'x-bblocks-extends': parent_id,
            'x-bblocks-extensions': extensions,
            'allOf': [
                {'$ref': root_schema_location}
            ],
        }

        for branch in schema_branches:
            if not branch.preserve_branch:
                continue

            def update_refs(subschema: Any, from_schema: ReferencedSchema, is_properties=False):
                if isinstance(subschema, dict):
                    if not is_properties and 'x-bblocks-extension-source' in subschema:
                        # Extension point
                        return subschema
                    for k in list(subschema.keys()):
                        if not is_properties and k == '$ref':
                            ref = subschema[k]
                            if is_url(ref):
                                # Leave as is
                                pass
                            else:
                                target = schema_resolver.resolve_schema(subschema['$ref'], from_schema,
                                                                        return_none_on_loop=False)
                                subschema[k] = target.location + f"#{target.fragment}" if target.fragment else ''
                        else:
                            subschema[k] = update_refs(subschema[k], from_schema,
                                                       not is_properties and k == 'properties')
                elif isinstance(subschema, list):
                    return list(map(lambda x: update_refs(x, from_schema), subschema))

                return subschema

            def walk_branch(node: SchemaNode, parent_schema: dict, force_preserve_branch: bool = False):
                if not force_preserve_branch and not node.preserve_branch:
                    return
                if node.tag == '$ref' and node.subschema and not node.children:
                    if parent_schema:
                        parent_schema.setdefault('allOf', []).append(update_refs(node.subschema, node.from_schema))
                    else:
                        parent_schema.update(update_refs(node.subschema, node.from_schema))
                elif node.tag in ('oneOf', 'anyOf', 'allOf'):
                    col_schema = parent_schema.setdefault(node.tag, [])
                    for child in node.children:
                        child_schema = {}
                        col_schema.append(child_schema)
                        walk_branch(child, child_schema,
                                    force_preserve_branch=force_preserve_branch or node.tag in ('oneOf', 'anyOf'))
                else:
                    if node.tag not in ('[]', '$ref') and not node.children:
                        # End of the line, we append the full subschema
                        parent_schema[node.tag] = update_refs(node.subschema, node.from_schema)
                    else:
                        if node.tag in ('[]', '$ref'):
                            if node.tag == '[]' or 'x-bblocks-extension-target' in node.subschema:
                                parent_schema.update(update_refs(node.subschema, node.from_schema))
                            walk_parent = parent_schema
                        else:
                            parent_schema[node.tag] = {}
                            walk_parent = parent_schema[node.tag]
                        for child in node.children:
                            walk_branch(child, walk_parent, force_preserve_branch=force_preserve_branch)

            branch_entry = {}
            output_schema['allOf'].append(branch_entry)
            walk_branch(branch, branch_entry)

        return output_schema

    def extract_aliases(self, ref_schema: ReferencedSchema, extension_source_id: str,
                        extension_target_id: str, target_bblock_schema: str) -> dict[str, dict[str, str]]:
        subschema = ref_schema.subschema
        new_mappings = {}
        if any(k in JSON_SCHEMA_ALIAS_ABORT for k in subschema.keys()):
            return new_mappings
        alias_subschema = {k: v for k, v in subschema.items() if k in ('$ref', 'allOf', 'anyOf', 'oneOf')}
        if len(alias_subschema) != 1:
            return new_mappings
        if '$ref' in alias_subschema:
            ref = alias_subschema['$ref']
        else:
            col: list = next(iter(alias_subschema.values()), None)
            if len(col) != 1 or '$ref' not in col[0]:
                return new_mappings
            ref = col[0]['$ref']
        if ref:
            resolved_schema = self.schema_resolver.resolve_schema(ref, ref_schema, return_none_on_loop=False)
            full_ref = resolved_schema.location + (
                f'#{resolved_schema.fragment}' if resolved_schema.fragment else '')
            new_mappings[full_ref] = {
                'extension_source_id': extension_source_id,
                'extension_target_id': extension_target_id,
                'extension_target_ref': target_bblock_schema
            }
            new_mappings.update(self.extract_aliases(resolved_schema,
                                                     extension_source_id,
                                                     extension_target_id,
                                                     target_bblock_schema))
        return new_mappings
