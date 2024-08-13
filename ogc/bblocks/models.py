#!/usr/bin/env python3
from __future__ import annotations

import dataclasses
import json
import os
import sys
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any, Generator, cast, AnyStr
from urllib.parse import urlparse, urljoin

import jsonschema
import networkx as nx
import requests
from ogc.na.util import is_url, load_yaml
from rdflib import Graph

from ogc.bblocks.util import get_schema, PathOrUrl, load_file, find_references_yaml, \
    find_references_xml
from ogc.bblocks.schema import RegisterSchemaResolver

BBLOCK_METADATA_FILE = 'bblock.json'


def get_bblock_subdirs(identifier: str) -> Path:
    return Path(*(identifier.split('.')[1:]))


def get_bblock_identifier(metadata_file: Path, root_path: Path = Path(),
                          prefix: str = '') -> tuple[str, Path]:
    rel_parts = Path(os.path.relpath(metadata_file.parent, root_path)).parts
    identifier = f"{prefix}{'.'.join(p for p in rel_parts if not p.startswith('_'))}"
    if identifier[-1] == '.':
        identifier = identifier[:-1]
    return identifier, Path(*rel_parts)


class BuildingBlockError(Exception):
    pass


class BuildingBlock:

    def __init__(self, identifier: str, metadata_file: Path,
                 rel_path: Path,
                 annotated_path: Path = Path()):
        self.identifier = identifier
        metadata_file = metadata_file.resolve()
        self.metadata_file = metadata_file

        with open(metadata_file) as f:
            self.metadata = json.load(f)

            try:
                jsonschema.validate(self.metadata, get_schema('metadata'))
            except Exception as e:
                raise BuildingBlockError('Error validating building block metadata') from e

            self.metadata.pop('itemIdentifier', None)
            self.metadata: dict[str, Any] = {
                'itemIdentifier': identifier,
                **self.metadata,
            }

        self._lazy_properties = {}

        self.subdirs = rel_path
        if '.' in self.identifier:
            self.subdirs = get_bblock_subdirs(identifier)

        self.super_bblock = self.metadata.get('superBBlock', False)

        fp = metadata_file.parent
        self.files_path = fp

        self.schema = self._find_path_or_url('schema', ('schema.yaml', 'schema.json'))
        self.openapi = self._find_path_or_url('openAPIDocument', ('openapi.yaml',))

        ap = fp / 'assets'
        self.assets_path = ap if ap.is_dir() else None

        self.examples_file = fp / 'examples.yaml'
        self._load_examples()

        self.tests_dir = fp / 'tests'

        self.annotated_path = annotated_path / self.subdirs
        self.annotated_schema = self.annotated_path / 'schema.yaml'
        self.jsonld_context = self.annotated_path / 'context.jsonld'
        self.output_openapi = self.annotated_path / 'openapi.yaml'
        self.output_openapi_30 = self.output_openapi.with_stem(f"{self.output_openapi.stem}-oas30")

        shacl_rules = self.metadata.setdefault('shaclRules', [])
        default_shacl_rules = fp / 'rules.shacl'
        if default_shacl_rules.is_file():
            shacl_rules.append('rules.shacl')
        self.shacl_rules = set(r if is_url(r) else fp / r for r in shacl_rules)

        self.ontology = self._find_path_or_url('ontology',
                                               ('ontology.ttl', 'ontology.owl'))
        self.output_ontology = self.annotated_path / 'ontology.ttl'

        self.remote_cache_dir = self.annotated_path / 'remote_cache'

    def _find_path_or_url(self, metadata_property: str, default_filenames: tuple[str, ...]):
        ref = self.metadata.get(metadata_property)
        if ref:
            if is_url(ref):
                result = ref
            else:
                result = self.files_path.joinpath(ref).resolve()
        else:
            result = default_filenames[0]
            for fn in default_filenames:
                f = self.files_path / fn
                if f.is_file():
                    result = f
                    break

        return PathOrUrl(result)

    def __getattr__(self, item):
        return self.metadata.get(item)

    def __getitem__(self, item):
        return self.metadata.get(item)

    def get(self, item, default=None):
        return self.metadata.get(item, default)

    def _load_examples(self):
        examples = None
        prefixes = {}
        if self.examples_file.is_file():
            examples = load_yaml(self.examples_file)
            if not examples:
                return None
            try:
                jsonschema.validate(examples, get_schema('examples'))
            except Exception as e:
                raise BuildingBlockError('Error validating building block examples (examples.yaml)') from e

            if isinstance(examples, dict):
                prefixes = examples.get('prefixes', {})
                examples = examples['examples']

            for example in examples:
                for snippet in example.get('snippets', ()):
                    if 'ref' in snippet:
                        # Load snippet code from "ref"
                        ref = snippet['ref'] if is_url(snippet['ref']) else self.files_path / snippet['ref']
                        snippet['code'] = load_file(ref)
                for transform in example.get('transforms', ()):
                    if 'ref' in transform:
                        # Load transform code from "ref"
                        ref = transform['ref'] if is_url(transform['ref']) else self.files_path / transform['ref']
                        transform['code'] = load_file(ref)
                if prefixes:
                    example['prefixes'] = {**prefixes, **example.get('prefixes', {})}

        self.example_prefixes = prefixes
        self.examples = examples

    @property
    def schema_contents(self):
        if 'schema_contents' not in self._lazy_properties:
            if not self.schema.exists:
                return None
            self._lazy_properties['schema_contents'] = load_file(self.schema.value, self.remote_cache_dir)
        return self._lazy_properties['schema_contents']

    @property
    def description(self):
        if 'description' not in self._lazy_properties:
            desc_file = self.files_path / 'description.md'
            self._lazy_properties['description'] = load_file(desc_file) if desc_file.is_file() else None
        return self._lazy_properties['description']

    @property
    def annotated_schema_contents(self):
        # We try to read it each time until we succeed, since it could
        # be created later during the postprocessing
        if 'annotated_schema_contents' not in self._lazy_properties:
            if not self.annotated_schema.is_file():
                return None
            self._lazy_properties['annotated_schema_contents'] = load_file(self.annotated_schema, self.remote_cache_dir)
        return self._lazy_properties['annotated_schema_contents']

    @property
    def jsonld_context_contents(self):
        # We try to read it each time until we succeed, since it could
        # be created later during the postprocessing
        if 'jsonld_context_contents' not in self._lazy_properties:
            if not self.jsonld_context.is_file():
                return None
            self._lazy_properties['jsonld_context_contents'] = load_file(self.jsonld_context, self.remote_cache_dir)
        return self._lazy_properties['jsonld_context_contents']

    @property
    def ontology_graph(self) -> Graph | None:
        if 'ontology_graph' not in self._lazy_properties:
            if not self.ontology.exists:
                return None
            contents = load_file(self.ontology.value, self.remote_cache_dir)
            self._lazy_properties['ontology_graph'] = Graph().parse(data=contents)
        return self._lazy_properties['ontology_graph']

    @property
    def output_openapi_contents(self):
        # We try to read it each time until we succeed, since it could
        # be created later during the postprocessing
        if 'output_openapi_contents' not in self._lazy_properties:
            if not self.output_openapi.is_file():
                return None
            self._lazy_properties['output_openapi_contents'] = load_file(self.output_openapi, self.remote_cache_dir)
        return self._lazy_properties['output_openapi_contents']

    def get_extra_test_resources(self) -> Generator[dict, None, None]:
        extra_tests_file = self.files_path / 'tests.yaml'
        if extra_tests_file.is_file():
            extra_tests: list[dict] = cast(list[dict], load_yaml(extra_tests_file))
            if not extra_tests:
                return
            try:
                jsonschema.validate(extra_tests, get_schema('extra-tests'))
            except Exception as e:
                raise BuildingBlockError('Error validating extra tests (tests.yaml)') from e

            for test in extra_tests:
                ref = self.resolve_file(test['ref'])
                test['ref'] = ref
                test['contents'] = load_file(ref)
                if not test.get('output-filename'):
                    if isinstance(ref, Path):
                        test['output-filename'] = ref.name
                    else:
                        test['output-filename'] = os.path.basename(urlparse(ref).path)
                yield test

    def resolve_file(self, fn_or_url):
        if isinstance(fn_or_url, Path) or (isinstance(fn_or_url, str) and not is_url(fn_or_url)):
            # assume file
            return self.files_path / fn_or_url
        else:
            return fn_or_url

    def get_files_with_references(self) -> list[PathOrUrl]:
        result: list[PathOrUrl] = []
        if self.schema.exists:
            result.append(self.schema)
            result.append(PathOrUrl(self.annotated_schema))
            result.append(PathOrUrl(self.annotated_schema.with_suffix('.json')))
            oas30_fn = self.annotated_schema.with_stem('schema-oas3.0')
            result.append(PathOrUrl(oas30_fn))
            result.append(PathOrUrl(oas30_fn.with_suffix('.json')))
        if self.openapi.exists:
            result.append(self.openapi)
            result.append(PathOrUrl(self.output_openapi))
        return result


class ImportedBuildingBlocks:

    def __init__(self, metadata_urls: list[str] | None):
        self.bblocks: dict[str, dict] = {}
        self.imported_registers: dict[str, list[str]] = {}
        if metadata_urls:
            pending_urls = deque(metadata_urls)
            while pending_urls:
                metadata_url = pending_urls.popleft()
                new_pending = self.load(metadata_url)
                pending_urls.extend(u for u in new_pending if u not in self.imported_registers)

    def load(self, metadata_url: str) -> list[str]:
        r = requests.get(metadata_url)
        r.raise_for_status()
        imported = r.json()
        if isinstance(imported, list):
            bblock_list = imported
            dependencies = []
        else:
            bblock_list = imported['bblocks']
            dependencies = imported.get('imports', [])
        self.imported_registers[metadata_url] = []
        for bblock in bblock_list:
            bblock['register'] = self
            self.bblocks[bblock['itemIdentifier']] = bblock
            self.imported_registers[metadata_url].append(bblock['itemIdentifier'])
        return dependencies


class BuildingBlockRegister:

    def __init__(self,
                 registered_items_path: Path,
                 annotated_path: Path = Path(),
                 fail_on_error: bool = False,
                 prefix: str = 'ogc.',
                 imported_bblocks: ImportedBuildingBlocks | None = None,
                 base_url: str | None = None):

        self.registered_items_path = registered_items_path
        self.annotated_path = annotated_path
        self.prefix = prefix
        self.bblocks: dict[str, BuildingBlock] = {}
        self.imported_bblocks = imported_bblocks.bblocks if imported_bblocks else {}
        self.base_url = base_url
        self._cwd = Path().resolve()

        # Map of file paths and URLs for local bblocks (source and annotated schemas, OpenAPI documents, etc.)
        # that can contain references or be referenced from other files
        self.local_bblock_files: dict[str, str] = {}
        # Map of document URLs for imported bblocks (source and annotated schemas, OpenAPI documents, etc.)
        # that can contain references or be referenced from other files
        self.imported_bblock_files: dict[str, str] = {}

        self.bblock_paths: dict[Path, BuildingBlock] = {}

        for metadata_file in sorted(registered_items_path.glob(f"**/{BBLOCK_METADATA_FILE}")):
            bblock_id, bblock_rel_path = get_bblock_identifier(metadata_file, registered_items_path, prefix)
            if bblock_id in self.bblocks:
                raise ValueError(f"Found duplicate bblock id: {bblock_id}")
            try:
                bblock = BuildingBlock(bblock_id, metadata_file,
                                       rel_path=bblock_rel_path,
                                       annotated_path=annotated_path)
                self.bblocks[bblock_id] = bblock
                self.bblock_paths[bblock.files_path] = bblock
            except Exception as e:
                if fail_on_error:
                    raise
                print('==== Exception encountered while processing', bblock_id, '====', file=sys.stderr)
                import traceback
                traceback.print_exception(e, file=sys.stderr)
                print('=========', file=sys.stderr)

        dep_graph = nx.DiGraph()

        for bblock_id, bblock in self.bblocks.items():
            for fn in bblock.get_files_with_references():
                if fn.is_url:
                    self.local_bblock_files[fn.url] = bblock_id
                else:
                    rel = os.path.relpath(fn.path)
                    self.local_bblock_files[rel] = bblock_id
                    if base_url:
                        self.local_bblock_files[f"{base_url}{rel}"] = bblock_id

        for identifier, imported_bblock in self.imported_bblocks.items():
            dep_graph.add_node(identifier)
            dep_graph.add_edges_from([(d, identifier) for d in imported_bblock.get('dependsOn', ())])
            imported_bblock.get('dependsOn', [])
            for schema_url in imported_bblock.get('schema', {}).values():
                self.imported_bblock_files[schema_url] = identifier
            source_schema = imported_bblock.get('sourceSchema')
            if source_schema:
                self.imported_bblock_files[source_schema] = identifier
            openapi_doc = imported_bblock.get('openAPIDocument')
            if isinstance(openapi_doc, str):
                self.imported_bblock_files[openapi_doc] = identifier
            elif isinstance(openapi_doc, list):
                for d in openapi_doc:
                    self.imported_bblock_files[d] = identifier
            elif isinstance(openapi_doc, dict):
                for d in openapi_doc.values():
                    self.imported_bblock_files[d] = identifier

        for bblock in self.bblocks.values():
            found_deps = self._resolve_bblock_deps(bblock)
            deps = bblock.metadata.get('dependsOn')
            if isinstance(deps, str):
                found_deps.add(deps)
            elif isinstance(deps, list):
                found_deps.update(deps)
            if found_deps:
                bblock.metadata['dependsOn'] = list(found_deps)
            dep_graph.add_node(bblock.identifier)
            dep_graph.add_edges_from([(d, bblock.identifier) for d in bblock.metadata.get('dependsOn', ())])

        cycles = list(nx.simple_cycles(dep_graph))
        if cycles:
            cycles_str = '\n - '.join(' -> '.join(reversed(c)) + ' -> ' + c[-1] for c in cycles)
            raise BuildingBlockError(f"Circular dependencies found: \n - {cycles_str}")
        self.bblocks: dict[str, BuildingBlock] = {b: self.bblocks[b]
                                                  for b in nx.topological_sort(dep_graph)
                                                  if b in self.bblocks}
        self.dep_graph = dep_graph
        self.schema_resolver = RegisterSchemaResolver(self)

    def _resolve_bblock_deps(self, bblock: BuildingBlock) -> set[str]:
        """
        Walks this bblock's files to find dependencies to other bblocks.

        :param bblock: the bblock
        :return: a `set` of dependencies (bblock identifiers)
        """

        # Map of ref -> source file
        references: dict[str, PathOrUrl] = {}

        for yamlfn in (bblock.schema, bblock.openapi):
            for ref in find_references_yaml(yamlfn):
                references.setdefault(ref, yamlfn)

        for xmlfn in ():
            for ref in find_references_xml(xmlfn):
                references.setdefault(ref, xmlfn)

        dependencies = set()

        for ref, source_fn in references.items():
            if not ref:
                continue
            if ref.startswith('bblocks://'):
                bblock_ref = ref[len('bblocks://'):]
                if bblock_ref == bblock.identifier:
                    continue
                elif bblock_ref in self.bblocks or bblock_ref in self.imported_bblocks:
                    dependencies.add(bblock_ref)
                else:
                    source_rel = source_fn.url if source_fn.is_url else os.path.relpath(source_fn.resolve())
                    raise ValueError(f'Invalid reference to bblock {bblock_ref}'
                                     f' from {bblock.identifier} ({source_rel}) - the bblock does not exist'
                                     f' - perhaps an import is missing?')
            elif ref in self.imported_bblock_files:
                # Imported bblock schema URL
                dependencies.add(self.imported_bblock_files[ref])
            elif ref in self.local_bblock_files:
                # Local bblock schema URL, most likely
                dependencies.add(self.local_bblock_files[ref])
            elif not is_url(ref):
                if source_fn.is_path:
                    # Check if target path in local bblock schemas
                    rel_ref = str(os.path.relpath(source_fn.resolve_ref(ref).resolve()))
                    if not Path(rel_ref).is_file():
                        raise ValueError(f"Invalid reference to {rel_ref}"
                                         f" from {bblock.identifier} ({source_fn}) - target file does not exist"
                                         f" - check that the file exists (maybe schema.yaml instead of schema.json?)")
                else:
                    # Check if target URL in local bblock schemas
                    rel_ref = urljoin(source_fn.url, ref)
                if rel_ref in self.local_bblock_files:
                    dependencies.add(self.local_bblock_files[rel_ref])

        dependencies.discard(bblock.identifier)
        return dependencies

    @lru_cache
    def find_dependencies(self, identifier: str) -> list[dict | BuildingBlock]:
        if identifier in self.bblocks:
            bblock = self.bblocks[identifier]
            metadata = bblock.metadata
        elif identifier in self.imported_bblocks:
            bblock = None
            metadata = self.imported_bblocks[identifier]
        else:
            return []

        dependencies = [bblock or metadata]
        for d in metadata.get('dependsOn', ()):
            dependencies.extend(self.find_dependencies(d))

        return dependencies

    def get_inherited_shacl_rules(self, identifier: str) -> dict[str, set[str | Path]]:
        rules: dict[str, set[str | Path]] = {}
        for dep in self.find_dependencies(identifier):
            if isinstance(dep, BuildingBlock):
                if dep.shacl_rules:
                    rules[dep.identifier] = dep.shacl_rules
            else:
                dep_rules = dep.get('shaclRules')
                if dep_rules:
                    if isinstance(dep_rules, list):
                        rules.setdefault(dep.get('itemIdentifier'), set()).update(dep_rules)
                    elif isinstance(dep_rules, dict):
                        for inh_id, inh_rules in dep_rules.items():
                            rules.setdefault(inh_id, set()).update(inh_rules)
        return rules

    def get(self, identifier: str):
        return self.bblocks.get(identifier, self.imported_bblocks.get(identifier))

    def get_url(self, path: str | Path) -> str:
        if not isinstance(path, Path):
            path = Path(path)
        return f"{self.base_url}{os.path.relpath(Path(path).resolve(), self._cwd)}"


@dataclasses.dataclass
class TransformMetadata:
    type: str
    source_mime_type: str
    target_mime_type: str
    transform_content: AnyStr
    input_data: AnyStr
    metadata: Any | None = None
