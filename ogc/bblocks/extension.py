import copy
import logging
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)
from pathlib import Path
from typing import Callable, Any, cast
from urllib.parse import urljoin

from ogc.na.annotate_schema import ReferencedSchema
from ogc.na.util import is_url

from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister
from ogc.bblocks.oas30 import oas30_to_oas31
from ogc.bblocks.schema import resolve_all_schema_references

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

        self._check_extension_cycle(bblock.identifier, parent_id)

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

        extension_ref_mappings: dict[str, dict] = {}
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
                raise ValueError(f'No schema was found for extension source {extension_source_id}. '
                                 f'Only building blocks with schemas are supported for extensions.')

            extension_ref_mappings[source_bblock_schema] = {
                'extension_source_id': extension_source_id,
                'extension_target_id': extension_target_id,
                'extension_target_ref': target_bblock_schema,
            }

            source_bblock_resolved_schema = schema_resolver.resolve_schema(source_bblock_schema)
            extension_ref_mappings.update(self.extract_aliases(source_bblock_resolved_schema, extension_source_id,
                                                                  extension_target_id, target_bblock_schema))

        result = (self._process_openapi(bblock, root_schema, parent_id, extensions, extension_ref_mappings)
                  if parent_is_openapi
                  else self._process_schema(bblock, root_schema, parent_id, extensions, extension_ref_mappings))
        return result, parent_is_openapi

    def _check_extension_cycle(self, bblock_id: str, parent_id: str):
        """
        Walks the chain of baseBuildingBlock pointers starting at parent_id, raising
        immediately if bblock_id (or any other identifier) is revisited. Deliberately not
        delegated to BuildingBlockRegister's general dependsOn cycle handling, which
        tolerates cycles by logging a warning and silently dropping an edge - a
        baseBuildingBlock cycle is nonsensical and must fail loudly rather than leave
        one of the bblocks processed against a stale/nonexistent parent output.
        """
        register = self.register
        chain = [bblock_id]
        current_id = parent_id
        while True:
            if current_id in chain:
                cycle = ' -> '.join(chain + [current_id])
                raise ValueError(f'Extension point cycle detected: {cycle}')
            chain.append(current_id)
            current = register.bblocks.get(current_id) or register.imported_bblocks.get(current_id)
            if current is None:
                # Unknown parent - reported with a clearer message elsewhere in process_extensions
                return
            ep = current.get('extensionPoints')
            if not ep:
                return
            current_id = ep['baseBuildingBlock']

    # Top-level keys read from an extending bblock's own openapi.yaml when it is being
    # treated as an additions document (extensionPoints + OpenAPI base). Anything else in
    # that file is ignored, with a warning - see _merge_openapi_additions.
    _OPENAPI_ADDITIVE_TOP_LEVEL_KEYS = ('paths', 'webhooks')
    _OPENAPI_ADDITIVE_COMPONENT_KEYS = ('schemas', 'parameters', 'responses', 'requestBodies',
                                        'headers', 'securitySchemes', 'examples', 'links')
    # Whole-value overrides: unlike paths/webhooks/components (collections of independently
    # addressable entries, additive-only), these describe the document as a whole, so an
    # extending bblock that declares one of them replaces the base's value outright rather
    # than merging into it. The 'openapi' version itself is deliberately NOT overridable
    # here - it stays whatever the base declared (or was upconverted to), since the
    # substitution logic's correctness depends on that version, not on authoring intent.
    _OPENAPI_OVERRIDABLE_TOP_LEVEL_KEYS = ('info', 'servers', 'security', 'tags', 'externalDocs')
    _OPENAPI_OPERATION_KEYS = ('get', 'put', 'post', 'delete', 'options', 'head', 'patch', 'trace', 'query')

    def _process_openapi(self, bblock: BuildingBlock, root_schema: ReferencedSchema, parent_id: str,
                         extensions: dict[str, str], extension_ref_mappings: dict[str, dict[str, str]]):
        """
        Produce a real, merged/substituted OpenAPI document for an OpenAPI-typed
        extending bblock: start from a full copy of the base document, merge in the
        extending bblock's own openapi.yaml (if any) as an additions document (new
        paths/webhooks/components additively, info/servers/security/tags/externalDocs
        as whole-value overrides - see _merge_openapi_additions), then substitute every
        reference slot that
        (transitively) references a declared extension source - Schema Object slots via
        the allOf-wrapped substitute_extensions walk, and every other OAS reference-slot
        kind (Parameter, Header, RequestBody, Response, Example, Link, Path Item) via a
        pure-swap bare-$ref match (see _substitute_pure_ref_slot) - chased through any
        number of externally-$ref'd hops (_substitute_external_ref_and_splice) so a
        schema nested inside, say, an externally-$ref'd Response object is still reached.

        components.callbacks is out of scope entirely (recurses the whole document
        shape); securitySchemes aren't $ref-able per the OAS spec.

        A 3.0 base document is upconverted to 3.1 first (oas30_to_oas31, oas30.py) - see
        docs/openapi-30-upconvert.md - since the substitution walk below depends on 2020-12
        JSON Schema alignment, which 3.0 Schema Objects predate.
        """
        version = str(root_schema.full_contents.get('openapi', ''))
        if version.startswith('3.0'):
            logger.warning("Building block %s has an OpenAPI 3.0 document (%r) - upconverting "
                           "to 3.1 before applying extensionPoints for %s",
                           parent_id, version, bblock.identifier)
            # oas30_to_oas31 already returns a deep copy, so no further copying is needed
            document = oas30_to_oas31(root_schema.full_contents, context=parent_id)
        else:
            document = copy.deepcopy(root_schema.full_contents)

        if bblock.openapi.exists:
            additions = bblock.openapi.load_yaml()
            # bblock.openapi is loaded raw here (unlike parent_bblock.output_openapi, which
            # process_extensions() already resolved via resolve_all_schema_references before
            # writing it out) - its bblocks:// refs need the same translation to real paths
            # before anything in it can be walked/resolved by schema_resolver.
            additions = resolve_all_schema_references(additions, self.register, bblock, bblock.openapi,
                                                       self.base_url)
            self._merge_openapi_additions(document, additions, bblock.identifier)

        self._substitute_openapi_schemas(document, root_schema, extension_ref_mappings)

        document['x-bblocks-extends'] = parent_id
        document['x-bblocks-extensions'] = extensions
        return document

    def _merge_additive_map(self, target_map: dict, additions_map: dict, label: str, bblock_id: str) -> None:
        """
        Merge additions_map into target_map in place, entry by entry:
        - entry_value is boolean False: remove entry_key from target_map. Removing an
          entry that doesn't exist there is an authoring error (raises) - as unambiguous
          a mistake as declaring one that already does. False is an unambiguous "delete"
          sentinel because no real OAS value at this granularity (a Path/Response/
          Schema/etc. object) is ever a bare boolean.
        - any other entry_value: add entry_key to target_map. entry_key already existing
          there is an authoring error (raises), rather than a silent override - remove it
          first (with False) if the intent is to replace it; note a single YAML/JSON map
          can't repeat the same key twice, so "remove then reintroduce under the same
          key" isn't expressible in one additions document - only in a further extending
          bblock's own additions, layered on top of this one's output.
        """
        for entry_key, entry_value in additions_map.items():
            if entry_value is False:
                if entry_key not in target_map:
                    raise ValueError(f"{bblock_id}'s openapi.yaml declares {label} entry {entry_key!r} as "
                                     f"removed (false), but it does not exist in its base building "
                                     f"block's document")
                del target_map[entry_key]
                continue
            if entry_key in target_map:
                raise ValueError(f"{bblock_id}'s openapi.yaml redeclares {label} entry {entry_key!r}, "
                                 f"which already exists in its base building block's document - "
                                 f"extensionPoints only supports adding new {label} entries (or "
                                 f"removing an existing one first with `false`), not silently "
                                 f"overriding one in place")
            target_map[entry_key] = entry_value

    def _merge_openapi_additions(self, document: dict, additions: dict, bblock_id: str):
        """
        Merge an extending bblock's own openapi.yaml into the (already-copied) base
        document as an additions document. Two different behaviors, by key:
        - paths/webhooks/components.*: additive/subtractive - each entry is either added
          (authoring error if it already exists in the base) or, if declared as `false`,
          removed (authoring error if it doesn't exist) - see _merge_additive_map. This is
          how a downstream bblock replaces, say, the base's templated `/processes/{id}`
          path with its own fixed set of concrete paths: `/processes/{id}: false` plus
          whatever new concrete paths it wants. Removing something still referenced
          elsewhere in the document (by a $ref) is on the author to avoid - not checked.
        - info/servers/security/tags/externalDocs: whole-value overrides - if present in
          the additions document, wholesale replace the base's value (there's nothing to
          "add" to a single title/description/server list; declaring one means taking
          ownership of it for this extending bblock).
        Any other top-level key (in particular 'openapi' - the document's declared
        version is never overridable, see _OPENAPI_OVERRIDABLE_TOP_LEVEL_KEYS) is ignored,
        with a logged warning.
        """
        known_keys = (self._OPENAPI_ADDITIVE_TOP_LEVEL_KEYS + self._OPENAPI_OVERRIDABLE_TOP_LEVEL_KEYS
                     + ('components',))
        ignored_keys = [k for k in additions.keys() if k not in known_keys]
        if ignored_keys:
            logger.warning("Ignoring top-level key(s) %s in %s's openapi.yaml - when extensionPoints "
                           "is set, only paths/webhooks/components (additive/subtractive) and "
                           "info/servers/security/tags/externalDocs (overrides) are read from it",
                           ', '.join(ignored_keys), bblock_id)

        for override_key in self._OPENAPI_OVERRIDABLE_TOP_LEVEL_KEYS:
            if override_key in additions:
                document[override_key] = additions[override_key]

        for top_key in self._OPENAPI_ADDITIVE_TOP_LEVEL_KEYS:
            additions_map = additions.get(top_key)
            if not additions_map:
                continue
            target_map = document.setdefault(top_key, {})
            self._merge_additive_map(target_map, additions_map, top_key, bblock_id)

        additions_components = additions.get('components')
        if additions_components:
            target_components = document.setdefault('components', {})
            for comp_key in self._OPENAPI_ADDITIVE_COMPONENT_KEYS:
                additions_comp_map = additions_components.get(comp_key)
                if not additions_comp_map:
                    continue
                target_comp_map = target_components.setdefault(comp_key, {})
                self._merge_additive_map(target_comp_map, additions_comp_map, f'components.{comp_key}',
                                         bblock_id)

            ignored_component_keys = [k for k in additions_components.keys()
                                      if k not in self._OPENAPI_ADDITIVE_COMPONENT_KEYS]
            if ignored_component_keys:
                logger.warning("Ignoring components.%s in %s's openapi.yaml additions document",
                               ', components.'.join(ignored_component_keys), bblock_id)

    def _substitute_schema_slot(self, container: dict | list, key: str | int, from_schema: ReferencedSchema,
                                extension_ref_mappings: dict[str, dict]) -> bool:
        """
        Substitute extension-source references (if any) within container[key] (a Schema
        Object), splicing a local {'allOf': [original, overlay]} in place to keep the
        slot valid against both the original and the substituted content (OAS has no
        document-wide composition mechanism the way a JSON Schema allOf-wrapped document
        does, so this "valid as both" guarantee is per-slot here rather than whole-
        document). A bare $ref to a local components.schemas entry is left untouched:
        that entry gets substituted independently as its own slot, and this $ref
        transparently inherits whatever ends up there. Returns True if a substitution
        was applied, so callers several dereference hops up an externally-$ref'd chain
        (see _substitute_external_ref_and_splice) know whether they need to splice their
        own inlined copy back in.
        """
        schema_slot = container[key]
        if not isinstance(schema_slot, dict):
            return False
        if set(schema_slot.keys()) == {'$ref'} and schema_slot['$ref'].startswith('#/components/schemas/'):
            return False
        original = copy.deepcopy(schema_slot)
        overlay = self.substitute_extensions(schema_slot, from_schema, extension_ref_mappings)
        if overlay is not None:
            container[key] = {'allOf': [original, overlay]}
            return True
        return False

    def _substitute_pure_ref_slot(self, container: dict | list, key: str | int, from_schema: ReferencedSchema,
                                  extension_ref_mappings: dict[str, dict]) -> bool:
        """
        Substitute a non-Schema-kind reference slot in place - a Parameter, Header,
        RequestBody, Response, Example, Link or (OAS 3.1) Path Item Object slot that is
        a bare Reference Object ({'$ref': ...}, optionally with 3.1 summary/description
        siblings) pointing at a declared extension source. Unlike Schema Object slots
        (_substitute_schema_slot), OAS has no composition mechanism for these kinds, so
        a match is a pure swap: the target replaces the whole slot, with no "valid as
        both" guarantee - see the "Component substitution" warning in
        docs/openapi-extension-points.md. Inline (non-$ref) content is never a match:
        only bare Reference Object slots are chased for these kinds. Returns True if a
        substitution was applied, so callers can skip descending further into the slot.
        """
        slot = container[key]
        if not isinstance(slot, dict) or '$ref' not in slot:
            return False
        target_schema = self.schema_resolver.resolve_schema(slot['$ref'], from_schema, return_none_on_loop=False)
        substitution = self.substitute_ref(self._full_ref(target_schema), extension_ref_mappings)
        if substitution is None:
            return False
        container[key] = substitution
        return True

    def _substitute_external_ref_and_splice(self, container: dict | list, key: str | int,
                                            from_schema: ReferencedSchema,
                                            extension_ref_mappings: dict[str, dict],
                                            process_inline_fn: Callable[[dict, ReferencedSchema], bool]) -> bool:
        """
        If container[key] is a bare Reference Object pointing OUTSIDE this document's own
        #/components/* (a local ref is left alone - its own canonical entry is visited and
        mutated independently by _substitute_openapi_schemas, and this $ref transparently
        inherits whatever ends up there), dereference it, run process_inline_fn against a
        deep copy of the dereferenced content (with the target's own ReferencedSchema as
        the new from_schema context, so further nested refs inside it resolve correctly),
        and splice that copy in as container[key] only if process_inline_fn reports a
        change - otherwise leave the original $ref untouched. This is what lets a schema
        substitution reach *inside* an externally-$ref'd Response/Parameter/RequestBody/
        Header/Path Item object, rather than only matching it as a whole (see "Planned v2"
        in docs/openapi-extension-points.md). Loop-safe via schema_resolver's existing
        cycle handling (return_none_on_loop=True) - not new protection.
        """
        slot = container[key]
        if not isinstance(slot, dict) or '$ref' not in slot or slot['$ref'].startswith('#/components/'):
            return False
        target_schema = self.schema_resolver.resolve_schema(slot['$ref'], from_schema, return_none_on_loop=True)
        if target_schema is None:
            return False
        content_copy = copy.deepcopy(target_schema.subschema)
        if process_inline_fn(content_copy, target_schema):
            container[key] = content_copy
            return True
        return False

    def _substitute_parameter_or_header_slot(self, container: dict | list, key: str | int,
                                             from_schema: ReferencedSchema,
                                             extension_ref_mappings: dict[str, dict]) -> bool:
        if self._substitute_pure_ref_slot(container, key, from_schema, extension_ref_mappings):
            return True

        def process(obj, obj_from_schema) -> bool:
            changed = False
            if isinstance(obj, dict) and 'schema' in obj:
                changed |= self._substitute_schema_slot(obj, 'schema', obj_from_schema, extension_ref_mappings)
            changed |= self._substitute_content_object_schemas(obj, obj_from_schema, extension_ref_mappings)
            return changed

        if self._substitute_external_ref_and_splice(container, key, from_schema, extension_ref_mappings, process):
            return True
        return process(container[key], from_schema)

    def _substitute_content_object_schemas(self, obj: dict | None, from_schema: ReferencedSchema,
                                           extension_ref_mappings: dict[str, dict]) -> bool:
        if not isinstance(obj, dict):
            return False
        content = obj.get('content')
        if not content:
            return False
        changed = False
        for media_type_obj in content.values():
            if not isinstance(media_type_obj, dict):
                continue
            if 'schema' in media_type_obj:
                changed |= self._substitute_schema_slot(media_type_obj, 'schema', from_schema, extension_ref_mappings)
            for example_key in list((media_type_obj.get('examples') or {}).keys()):
                changed |= self._substitute_pure_ref_slot(media_type_obj['examples'], example_key, from_schema,
                                                          extension_ref_mappings)
        return changed

    def _substitute_request_body_slot(self, container: dict, key: str, from_schema: ReferencedSchema,
                                      extension_ref_mappings: dict[str, dict]) -> bool:
        if self._substitute_pure_ref_slot(container, key, from_schema, extension_ref_mappings):
            return True

        def process(request_body, request_body_from_schema) -> bool:
            return self._substitute_content_object_schemas(request_body, request_body_from_schema,
                                                            extension_ref_mappings)

        if self._substitute_external_ref_and_splice(container, key, from_schema, extension_ref_mappings, process):
            return True
        return process(container[key], from_schema)

    def _substitute_response_slot(self, container: dict, key: str, from_schema: ReferencedSchema,
                                  extension_ref_mappings: dict[str, dict]) -> bool:
        if self._substitute_pure_ref_slot(container, key, from_schema, extension_ref_mappings):
            return True

        def process(response, response_from_schema) -> bool:
            if not isinstance(response, dict):
                return False
            changed = self._substitute_content_object_schemas(response, response_from_schema, extension_ref_mappings)
            headers = response.get('headers') or {}
            for header_key in list(headers.keys()):
                changed |= self._substitute_parameter_or_header_slot(headers, header_key, response_from_schema,
                                                                      extension_ref_mappings)
            links = response.get('links') or {}
            for link_key in list(links.keys()):
                changed |= self._substitute_pure_ref_slot(links, link_key, response_from_schema,
                                                           extension_ref_mappings)
            return changed

        if self._substitute_external_ref_and_splice(container, key, from_schema, extension_ref_mappings, process):
            return True
        return process(container[key], from_schema)

    def _substitute_path_item_schemas(self, container: dict, key: str, from_schema: ReferencedSchema,
                                      extension_ref_mappings: dict[str, dict]) -> bool:
        # A path/webhook entry can itself be a bare Reference Object (OAS 3.1 Path Item
        # Object) - try that first, same as any other non-Schema slot.
        if self._substitute_pure_ref_slot(container, key, from_schema, extension_ref_mappings):
            return True

        def process(path_item, path_item_from_schema) -> bool:
            if not isinstance(path_item, dict):
                return False
            changed = False
            parameters = path_item.get('parameters') or []
            for i in range(len(parameters)):
                changed |= self._substitute_parameter_or_header_slot(parameters, i, path_item_from_schema,
                                                                      extension_ref_mappings)
            for method in self._OPENAPI_OPERATION_KEYS:
                operation = path_item.get(method)
                if not isinstance(operation, dict):
                    continue
                op_params = operation.get('parameters') or []
                for i in range(len(op_params)):
                    changed |= self._substitute_parameter_or_header_slot(op_params, i, path_item_from_schema,
                                                                          extension_ref_mappings)
                if 'requestBody' in operation:
                    changed |= self._substitute_request_body_slot(operation, 'requestBody', path_item_from_schema,
                                                                   extension_ref_mappings)
                responses = operation.get('responses') or {}
                for resp_key in list(responses.keys()):
                    changed |= self._substitute_response_slot(responses, resp_key, path_item_from_schema,
                                                               extension_ref_mappings)
            return changed

        if self._substitute_external_ref_and_splice(container, key, from_schema, extension_ref_mappings, process):
            return True
        return process(container[key], from_schema)

    def _substitute_openapi_schemas(self, document: dict, from_schema: ReferencedSchema,
                                    extension_ref_mappings: dict[str, dict]) -> None:
        """
        Walk every reference slot in document (components.* and paths/webhooks,
        including whatever the additions document contributed) and substitute extension
        sources in place - Schema Object slots via substitute_extensions/allOf-wrap,
        every other OAS reference-slot kind (Parameter, Header, RequestBody, Response,
        Example, Link, Path Item) via a pure-swap bare-$ref match. components.callbacks
        and securitySchemes are not visited (out of scope, per
        docs/openapi-extension-points.md - callbacks recurse the whole document shape,
        and security schemes aren't $ref-able at all).
        """
        components = document.get('components') or {}

        schemas = components.get('schemas') or {}
        for name in list(schemas.keys()):
            self._substitute_schema_slot(schemas, name, from_schema, extension_ref_mappings)

        parameters = components.get('parameters') or {}
        for name in list(parameters.keys()):
            self._substitute_parameter_or_header_slot(parameters, name, from_schema, extension_ref_mappings)

        headers = components.get('headers') or {}
        for name in list(headers.keys()):
            self._substitute_parameter_or_header_slot(headers, name, from_schema, extension_ref_mappings)

        request_bodies = components.get('requestBodies') or {}
        for name in list(request_bodies.keys()):
            self._substitute_request_body_slot(request_bodies, name, from_schema, extension_ref_mappings)

        responses = components.get('responses') or {}
        for name in list(responses.keys()):
            self._substitute_response_slot(responses, name, from_schema, extension_ref_mappings)

        examples = components.get('examples') or {}
        for name in list(examples.keys()):
            self._substitute_pure_ref_slot(examples, name, from_schema, extension_ref_mappings)

        links = components.get('links') or {}
        for name in list(links.keys()):
            self._substitute_pure_ref_slot(links, name, from_schema, extension_ref_mappings)

        paths = document.get('paths') or {}
        for path_key in list(paths.keys()):
            self._substitute_path_item_schemas(paths, path_key, from_schema, extension_ref_mappings)
        webhooks = document.get('webhooks') or {}
        for path_key in list(webhooks.keys()):
            self._substitute_path_item_schemas(webhooks, path_key, from_schema, extension_ref_mappings)

    def substitute_ref(self, ref: str, extension_ref_mappings: dict[str, dict]) -> dict | None:
        """
        Look up a resolved (already dereferenced) ref string against extension_ref_mappings.
        Returns the substitution subschema ({'$ref': target, 'x-bblocks-extension-source': ...,
        'x-bblocks-extension-target': ...}) to splice in place of the original $ref, or None if
        `ref` is not a declared extension source.
        """
        extension_target = extension_ref_mappings.get(ref)
        if not extension_target:
            return None
        return {
            '$ref': extension_target['extension_target_ref'],
            'x-bblocks-extension-source': extension_target['extension_source_id'],
            'x-bblocks-extension-target': extension_target['extension_target_id'],
        }

    def _full_ref(self, schema: ReferencedSchema) -> str:
        """
        Canonical full ref string for a resolved ReferencedSchema (location + fragment,
        with a local Path location made absolute/base_url-relative). Used both as the
        SchemaNode-tree walk's own ref identity and as the lookup key into
        extension_ref_mappings from any other reference-slot substitution site (see
        _substitute_pure_ref_slot).
        """
        full_ref = schema.location
        if isinstance(schema.location, Path):
            full_ref = schema.location.resolve()
            if self.base_url:
                full_ref = urljoin(self.base_url,
                                   os.path.relpath(full_ref))
        if schema.fragment:
            full_ref += '#' + schema.fragment
        return full_ref

    def substitute_extensions(self, schema_root: dict, from_schema: ReferencedSchema,
                              extension_ref_mappings: dict[str, dict]) -> dict | None:
        """
        Rewrite schema_root (with $refs interpreted relative to from_schema), substituting
        every branch that (transitively) references an extension source with the
        corresponding target, tagging substituted $refs with x-bblocks-extension-source/
        -target. Returns a partial "overlay" schema containing only the touched branches,
        or None if schema_root contains no extension-source references anywhere, so the
        caller can leave the original as-is.
        """
        schema_resolver = self.schema_resolver
        visited_refs = {}
        root_node: SchemaNode | None = None

        def create_schema_node(parent_node: SchemaNode | None, tag: str, from_schema: ReferencedSchema,
                               is_properties: bool = False, subschema: dict | list | None = None) -> SchemaNode:
            nonlocal root_node
            if parent_node is None:
                node = SchemaNode(tag=tag, from_schema=from_schema, is_properties=is_properties, subschema=subschema)
                node.root = node
                root_node = node
            else:
                node = SchemaNode(root=parent_node.root, parent=parent_node, tag=tag, from_schema=from_schema,
                                  is_properties=is_properties, subschema=subschema)
                parent_node.children.append(node)
            return node

        def walk_subschema(subschema, from_schema: ReferencedSchema, parent_node: SchemaNode | None):
            if not subschema or not isinstance(subschema, dict):
                return

            if '$ref' in subschema:
                ref = subschema.pop('$ref')
                if self.ref_mapper:
                    ref = self.ref_mapper(ref, subschema)
                target_schema = schema_resolver.resolve_schema(ref, from_schema, return_none_on_loop=False)
                target_schema_full_ref = self._full_ref(target_schema)

                substitution = self.substitute_ref(target_schema_full_ref, extension_ref_mappings)

                skip_node = False
                if substitution:
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
                                extension_ref_mappings[self._full_ref(undetected_alias)] = {
                                    'extension_source_id': substitution['x-bblocks-extension-source'],
                                    'extension_target_id': substitution['x-bblocks-extension-target'],
                                    'extension_target_ref': substitution['$ref'],
                                }
                        elif pn.tag != '[]' and (
                                pn.tag not in ('oneOf', 'allOf', 'anyOf', '[]') or len(pn.children) > 1):
                            break
                        pn = pn.parent

                if skip_node:
                    ref_node = parent_node
                else:
                    ref_node = create_schema_node(parent_node, '$ref', from_schema,
                                                  subschema=substitution if substitution else {'$ref': ref})
                    if substitution:
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

        walk_subschema(schema_root, from_schema, None)

        if root_node is None or not root_node.preserve_branch:
            return None

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

        overlay: dict = {}
        walk_branch(root_node, overlay)
        return overlay

    def _process_schema(self, bblock: BuildingBlock, root_schema: ReferencedSchema, parent_id: str,
                        extensions: dict[str, str], extension_ref_mappings: dict[str, dict[str, str]]):
        overlay = self.substitute_extensions(root_schema.full_contents, root_schema, extension_ref_mappings)

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
        if overlay is not None:
            output_schema['allOf'].append(overlay)

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
