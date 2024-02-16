from __future__ import annotations

import csv
import dataclasses
import functools
import hashlib
import json
import os.path
import re
import sys
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence, Callable, AnyStr, Generator, cast
from urllib.parse import urljoin, urlparse, urlunparse

import jsonschema
import networkx as nx
import pathvalidate
import requests
from ogc.na.annotate_schema import SchemaAnnotator, ContextBuilder, SchemaResolver
from ogc.na.util import load_yaml, dump_yaml, is_url

BBLOCK_METADATA_FILE = 'bblock.json'
BBLOCKS_REF_ANNOTATION = 'x-bblocks-ref'

loaded_schemas: dict[str, dict] = {}


class CustomJSONEncoder(json.JSONEncoder):

    def default(self, obj):
        if isinstance(obj, set):
            return list(obj)
        elif isinstance(obj, Path):
            return os.path.relpath(obj.resolve())
        elif isinstance(obj, PathOrUrl):
            return obj.url if obj.is_url else os.path.relpath(obj.resolve())
        else:
            return json.JSONEncoder.default(self, obj)


@functools.lru_cache
def load_file_cached(fn):
    return load_file(fn)


def load_file(fn):
    if isinstance(fn, PathOrUrl):
        fn = fn.value
    if isinstance(fn, str) and is_url(fn):
        r = requests.get(fn)
        r.raise_for_status()
        return r.text
    with open(fn) as f:
        return f.read()


def get_schema(t: str) -> dict:
    if t not in loaded_schemas:
        loaded_schemas[t] = load_yaml(Path(__file__).parent / f'{t}-schema.yaml')
    return loaded_schemas[t]


def get_bblock_identifier(metadata_file: Path, root_path: Path = Path(),
                          prefix: str = '') -> tuple[str, Path]:
    rel_parts = Path(os.path.relpath(metadata_file.parent, root_path)).parts
    identifier = f"{prefix}{'.'.join(p for p in rel_parts if not p.startswith('_'))}"
    if identifier[-1] == '.':
        identifier = identifier[:-1]
    return identifier, Path(*rel_parts)


def pathify(v: str | Path):
    if not v:
        return Path(v)
    if isinstance(v, Path):
        return v
    return v if is_url(v) else Path(v)


def get_bblock_subdirs(identifier: str) -> Path:
    return Path(*(identifier.split('.')[1:]))


class BuildingBlockError(Exception):
    pass


class PathOrUrl:

    def __init__(self, value: str | Path):
        if not value:
            raise ValueError('Empty value provided')
        self.value: [str | Path] = value
        self.is_path = isinstance(value, Path) or not is_url(value)
        if self.is_path:
            self.value = Path(value).resolve()
            self.path = self.value
        else:
            self.url = self.value
        self.is_url = not self.is_path
        self.exists = self.is_url or value.is_file()
        self.parsed_url = None if self.is_path else urlparse(self.value)

    def __str__(self) -> str:
        return str(self.value)

    def resolve(self) -> str | Path:
        """
        Emulates Path.resolve()
        :return: the wrapped value if this is a URL, or the resolved path
        """
        if self.is_url:
            return self.url
        return self.path.resolve()

    def resolve_ref(self, ref: str | Path) -> PathOrUrl | None:
        """
        Resolves a (relative, absolute or full URL) reference from the wrapped value
        :param ref:
        :return:
        """
        ref = pathify(ref)
        if isinstance(ref, str):
            return PathOrUrl(ref)
        if self.is_url:
            return PathOrUrl(urljoin(self.url, str(ref)))
        else:
            return PathOrUrl(self.path / ref)

    def as_uri(self):
        if self.is_path:
            return self.value.as_uri()
        return self.value

    def with_name(self, name: str):
        if self.is_path:
            return self.value.with_name(name)
        if self.parsed_url.path:
            newpath = Path(self.parsed_url.path).with_name(name)
        else:
            newpath = name
        return urlunparse(self.parsed_url[0:2] + (str(newpath),) + self.parsed_url[3:])

    @functools.cache
    def to_url(self, base: str):
        if self.is_url:
            return self.value
        return urljoin(base, os.path.relpath(self.value))

    @property
    def parent(self):
        """
        Emulates Path.parent
        :return:
        """
        if self.is_path:
            return PathOrUrl(self.value.parent)
        elif self.parsed_url.path:
            parsed_path = Path(self.parsed_url.path)
            return PathOrUrl(urlunparse(self.parsed_url[0:2] + (str(parsed_path.parent),) + self.parsed_url[3:]))
        else:
            return self

    def is_file(self):
        return self.exists

    @functools.cache
    def load_yaml(self):
        if self.is_url:
            return load_yaml(url=self.url)
        else:
            return load_yaml(filename=self.path)


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
        if s in self.register.local_bblock_schemas:
            bblock_id = self.register.local_bblock_schemas[s]
            bblock = self.register.bblocks[bblock_id]
            if bblock.annotated_schema.is_file():
                # NOTE: Only change it if it exists, otherwise the first time reading
                # the source schema (to begin the annotation) will fail
                s = bblock.annotated_schema
        elif s in self.register.imported_bblock_schemas:
            bblock_id = self.register.imported_bblock_schemas[s]
            bblock = self.register.imported_bblocks[bblock_id]
            s = bblock['schema']['application/yaml']
        return s

    def load_contents(self, s: str | Path) -> tuple[dict, bool]:
        return super().load_contents(self.find_schema(s))


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

        metadata_schema = self.metadata.get('schema')
        if metadata_schema:
            if not is_url(metadata_schema):
                schema = self.files_path.joinpath(metadata_schema).resolve()
            else:
                schema = metadata_schema
        else:
            schema = fp / 'schema.yaml'
            if not schema.is_file():
                schema = fp / 'schema.json'
        self.schema = PathOrUrl(schema)

        ap = fp / 'assets'
        self.assets_path = ap if ap.is_dir() else None

        self.examples_file = fp / 'examples.yaml'
        self.examples = self._load_examples()

        self.tests_dir = fp / 'tests'

        self.annotated_path = annotated_path / self.subdirs
        self.annotated_schema = self.annotated_path / 'schema.yaml'
        self.jsonld_context = self.annotated_path / 'context.jsonld'

        shacl_rules = self.metadata.setdefault('shaclRules', [])
        default_shacl_rules = fp / 'rules.shacl'
        if default_shacl_rules.is_file():
            shacl_rules.append('rules.shacl')
        self.shacl_rules = set(r if is_url(r) else fp / r for r in shacl_rules)

    def __getattr__(self, item):
        return self.metadata.get(item)

    def __getitem__(self, item):
        return self.metadata.get(item)

    def get(self, item, default=None):
        return self.metadata.get(item, default)

    def _load_examples(self):
        examples = None
        if self.examples_file.is_file():
            examples = load_yaml(self.examples_file)
            if not examples:
                return None
            try:
                jsonschema.validate(examples, get_schema('examples'))
            except Exception as e:
                raise BuildingBlockError('Error validating building block examples (examples.yaml)') from e

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
        return examples

    @property
    def schema_contents(self):
        if 'schema_contents' not in self._lazy_properties:
            if not self.schema.exists:
                return None
            self._lazy_properties['schema_contents'] = load_file(self.schema.value)
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
            self._lazy_properties['annotated_schema_contents'] = load_file(self.annotated_schema)
        return self._lazy_properties['annotated_schema_contents']

    @property
    def jsonld_context_contents(self):
        # We try to read it each time until we succeed, since it could
        # be created later during the postprocessing
        if 'jsonld_context_contents' not in self._lazy_properties:
            if not self.jsonld_context.is_file():
                return None
            self._lazy_properties['jsonld_context_contents'] = load_file(self.jsonld_context)
        return self._lazy_properties['jsonld_context_contents']

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

        # Map of schema paths and URLs for local bblocks, including source and annotated ones
        self.local_bblock_schemas: dict[str, str] = {}

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

                if bblock.schema.exists:
                    if bblock.schema.is_url:
                        self.local_bblock_schemas[bblock.schema.url] = bblock_id
                    else:
                        rel = os.path.relpath(bblock.schema.path)
                        self.local_bblock_schemas[rel] = bblock_id
                        if base_url:
                            self.local_bblock_schemas[f"{base_url}{rel}"] = bblock_id

                    for s in (bblock.annotated_schema, bblock.annotated_schema.with_suffix('.json')):
                        rel = os.path.relpath(s)
                        self.local_bblock_schemas[rel] = bblock_id
                        if base_url:
                            self.local_bblock_schemas[f"{base_url}{rel}"] = bblock_id

            except Exception as e:
                if fail_on_error:
                    raise
                print('==== Exception encountered while processing', bblock_id, '====', file=sys.stderr)
                import traceback
                traceback.print_exception(e, file=sys.stderr)
                print('=========', file=sys.stderr)

        # Map of schema URLs for imported bblocks, including source and annotated ones
        self.imported_bblock_schemas: dict[str, str] = {}
        dep_graph = nx.DiGraph()
        for identifier, imported_bblock in self.imported_bblocks.items():
            dep_graph.add_node(identifier)
            dep_graph.add_edges_from([(d, identifier) for d in imported_bblock.get('dependsOn', ())])
            imported_bblock.get('dependsOn', [])
            for schema_url in imported_bblock.get('schema', {}).values():
                self.imported_bblock_schemas[schema_url] = identifier
            source_schema = imported_bblock.get('sourceSchema')
            if source_schema:
                self.imported_bblock_schemas[source_schema] = identifier

        for bblock in self.bblocks.values():
            found_deps = self._resolve_bblock_schema_deps(bblock)
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

    def _resolve_bblock_schema_deps(self, bblock: BuildingBlock) -> set[str]:
        """
        Walks this bblock's schema to find dependencies to other bblocks.

        :param bblock: the bblock
        :return: a `set` of dependencies (bblock identifiers)
        """
        if not bblock.schema.exists:
            return set()
        bblock_schema = bblock.schema.load_yaml()

        deps = set()

        def walk_schema(schema):
            if isinstance(schema, dict):
                ref = schema.get(BBLOCKS_REF_ANNOTATION, schema.get('$ref'))
                if isinstance(ref, str):
                    # Remove fragment
                    ref = re.sub(r'#.*$', '', ref)
                    if ref == f"bblocks://{bblock.identifier}":
                        # A self-reference is not a dependency
                        pass
                    elif ref.startswith('bblocks://'):
                        # Get id directly from bblocks:// URI
                        deps.add(ref[len('bblocks://'):])
                    elif ref in self.imported_bblock_schemas:
                        # Import bblock schema URL
                        deps.add(self.imported_bblock_schemas[ref])
                    elif ref in self.local_bblock_schemas:
                        # Local bblock schema URL, most likely
                        deps.add(self.local_bblock_schemas[ref])
                    else:
                        if bblock.schema.is_path:
                            # Check if target path in local bblock schemas
                            rel_ref = os.path.relpath(bblock.schema.parent.resolve_ref(ref).resolve())
                        else:
                            # Check if target URL in local bblock schemas
                            rel_ref = urljoin(bblock.schema.url, ref)
                        if rel_ref in self.local_bblock_schemas:
                            deps.add(self.local_bblock_schemas[rel_ref])

                for prop, val in schema.items():
                    if prop not in (BBLOCKS_REF_ANNOTATION, '$ref') or not isinstance(val, str):
                        walk_schema(val)
            elif isinstance(schema, list):
                for item in schema:
                    walk_schema(item)

        walk_schema(bblock_schema)

        extends = bblock.metadata.get('extends')
        if extends:
            if isinstance(extends, str):
                deps.add(extends)
            elif isinstance(extends, dict):
                deps.add(extends['itemIdentifier'])

        deps.discard(bblock.identifier)
        return deps

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


@dataclasses.dataclass
class TransformMetadata:
    type: str
    source_mime_type: str
    target_mime_type: str
    transform_content: AnyStr
    input_data: AnyStr
    metadata: Any | None = None


def write_superbblocks_schemas(super_bblocks: dict[Path, BuildingBlock],
                               bblocks_register: BuildingBlockRegister,
                               annotated_path: Path | None = None) -> list[Path]:
    def process_sbb(sbb_dir: Path, sbb: BuildingBlock, skip_dirs) -> dict:
        any_of = []
        parsed = set()
        for schema_fn in ('schema.yaml', 'schema.json'):
            for schema_file in sorted(sbb_dir.glob(f"**/{schema_fn}")):
                # Skip schemas in superbblock directory, avoid double parsing
                # (schema.yaml and schema.json) and in child superbblock directories
                if schema_file.parent == sbb_dir \
                        or schema_file.with_suffix('') in parsed \
                        or schema_file.parents in skip_dirs:
                    continue

                schema = load_yaml(schema_file)
                if not isinstance(schema, dict):
                    continue

                if 'schema' in schema:
                    # OpenAPI sub spec - skip
                    continue

                schema_file = schema_file.resolve()
                parent_dir = schema_file.parent.resolve()
                sbb_dir = sbb_dir.resolve()

                def ref_updater(ref):
                    if not is_url(ref):
                        # update
                        if ref[0] == '#':
                            ref = schema_file / ref
                        else:
                            ref = parent_dir / ref
                        return os.path.relpath(ref, sbb_dir)
                    return ref

                # update relative $ref's
                update_refs(schema, ref_updater)

                any_of.append(schema)

                parsed.add(schema_file.with_suffix(''))

        output_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'description': sbb.name,
        }
        if any_of:
            output_schema['anyOf'] = any_of
        return output_schema

    annotated_super_bblock_dirs = set(annotated_path / b.subdirs for b in super_bblocks.values())
    result = []

    for super_bblock_dir, super_bblock in super_bblocks.items():
        # Should we generate the schema in the source directory? Let's not for now...
        # super_schema = process_sbb(super_bblock_dir, super_bblock, super_bblocks.keys())
        # dump_yaml(super_schema, super_bblock_dir / 'schema.yaml')
        # result.append(super_bblock_dir / 'schema.yaml')

        annotated_output_file = annotated_path / super_bblock.subdirs / 'schema.yaml'
        annotated_output_file.parent.mkdir(parents=True, exist_ok=True)
        super_schema_annotated = process_sbb(annotated_output_file.parent, super_bblock, annotated_super_bblock_dirs)
        dump_yaml(super_schema_annotated, annotated_output_file)
        result.append(annotated_output_file)
        with open(annotated_output_file.with_suffix('.json'), 'w') as f:
            json.dump(super_schema_annotated, f, indent=2)

        jsonld_context = write_jsonld_context(annotated_output_file, bblocks_register)
        if jsonld_context:
            result.append(jsonld_context)

    return result


def write_jsonld_context(annotated_schema: Path | str, bblocks_register: BuildingBlockRegister) -> Path | None:
    if not isinstance(annotated_schema, Path):
        annotated_schema = Path(annotated_schema)
    ctx_builder = ContextBuilder(annotated_schema, schema_resolver=bblocks_register.schema_resolver)
    if not ctx_builder.context.get('@context'):
        return None
    context_fn = annotated_schema.resolve().parent / 'context.jsonld'
    with open(context_fn, 'w') as f:
        json.dump(ctx_builder.context, f, indent=2)
    with open(context_fn.parent / '_visited_properties.tsv', 'w', newline='') as f, \
            open(context_fn.parent / '_empty_properties.tsv', 'w', newline='') as fe:
        writer = csv.writer(f, delimiter='\t')
        writer.writerow(['path', '@id'])
        fe.write('path\n')

        for e in ctx_builder.visited_properties.items():
            writer.writerow(e)
            if not e[1]:
                fe.write(f"{e[0]}\n")
    return context_fn


def update_refs(schema: Any, updater: Callable[[str], str]):
    pending = deque()
    pending.append(schema)

    while pending:
        sub_schema = pending.popleft()
        if isinstance(sub_schema, dict):
            for k in list(sub_schema.keys()):
                if k == '$ref' and isinstance(sub_schema[k], str):
                    sub_schema[k] = updater(sub_schema[k])
                else:
                    pending.append(sub_schema[k])
        elif isinstance(sub_schema, Sequence) and not isinstance(sub_schema, str):
            pending.extend(sub_schema)

    return schema


def annotate_schema(bblock: BuildingBlock,
                    bblocks_register: BuildingBlockRegister,
                    context: Path | dict | None = None,
                    base_url: str | None = None) -> list[Path]:
    result = []
    schema_fn = None
    schema_url = None
    metadata_schemas = bblock.metadata.get('schema')

    if isinstance(metadata_schemas, Sequence):
        # Take only first, if more than one
        ref_schema = metadata_schemas if isinstance(metadata_schemas, str) else metadata_schemas[0]
        if is_url(ref_schema, http_only=True):
            schema_url = ref_schema
        else:
            schema_fn = bblock.files_path.joinpath(ref_schema).resolve()
    elif bblock.schema.is_url:
        schema_url = bblock.schema.value
    elif bblock.schema.exists:
        schema_fn = bblock.schema.value

    if not schema_fn and not schema_url:
        return result

    override_schema = load_yaml(filename=schema_fn, url=schema_url)
    override_schema = resolve_all_schema_references(override_schema, bblocks_register, bblock, base_url)

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

    def update_json_ref(ref):
        if ref[0] == '#' or not is_url(ref):
            return ref
        if '#' in ref:
            ref, fragment = ref.split('#', 1)
            fragment = '#' + fragment
        else:
            fragment = ''
        if ref in bblocks_register.local_bblock_schemas or ref in bblocks_register.imported_bblock_schemas:
            return re.sub(r'\.yaml$', r'.json', ref) + fragment
        return ref

    # JSON
    update_refs(annotated_schema, update_json_ref)
    annotated_schema_json_fn = annotated_schema_fn.with_suffix('.json')
    with open(annotated_schema_json_fn, 'w') as f:
        json.dump(annotated_schema, f, indent=2)
    result.append(annotated_schema_json_fn)

    # OAS 3.0
    if base_url:
        oas30_schema_fn = annotated_schema_fn.with_stem('schema-oas3.0')
        dump_yaml(to_oas30(annotated_schema_fn, urljoin(base_url, os.path.relpath(oas30_schema_fn)), bblocks_register),
                  oas30_schema_fn)
        result.append(oas30_schema_fn)

        oas30_schema_json_fn = annotated_schema_json_fn.with_stem('schema-oas3.0')
        with open(oas30_schema_json_fn, 'w') as f:
            json.dump(to_oas30(annotated_schema_json_fn,
                               urljoin(base_url, os.path.relpath(oas30_schema_json_fn)), bblocks_register), f, indent=2)
        result.append(oas30_schema_json_fn)

    return result


def resolve_all_schema_references(schema: Any,
                                  bblocks_register: BuildingBlockRegister,
                                  from_bblock: BuildingBlock,
                                  base_url: str | None = None) -> Any:
    def walk(subschema):
        if isinstance(subschema, dict):
            if isinstance(subschema.get('$ref'), str):
                subschema['$ref'] = resolve_schema_reference(subschema['$ref'],
                                                             subschema,
                                                             bblocks_register,
                                                             from_bblock,
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
                             base_url: str | None = None) -> str:
    ref = schema.pop(BBLOCKS_REF_ANNOTATION, ref)

    if ref[0] == '#':
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

        if from_bblock.schema.is_url:
            # Reference to a remote schema
            check_refs = {from_bblock.schema.resolve_ref(ref).value}
        else:
            # Reference to a local schema (same repo)
            # First make ref relative to cwd
            rel_ref = os.path.relpath(from_bblock.files_path.joinpath(ref))

            # Then check json/yaml variants
            check_refs = {rel_ref,
                          re.sub(r'\.json$', '.yaml', rel_ref),
                          re.sub(r'\.ya?ml', '.json', rel_ref)}
        for check_ref in check_refs:
            if check_ref in bblocks_register.local_bblock_schemas:
                target_bblock_id = bblocks_register.local_bblock_schemas[check_ref]
        if not target_bblock_id:
            if base_url:
                # Return the URL to the $ref
                return f"{base_url}{os.path.relpath(str(from_bblock.schema.parent.resolve_ref(ref).value))}{fragment}"
            else:
                # Relativize from annotated schema path
                return os.path.relpath(from_bblock.schema.parent.resolve_ref(ref).resolve(),
                                       from_bblock.annotated_schema.parent) + fragment
    else:
        # URL -> search in both local and imported bblock schemas
        target_bblock_id = bblocks_register.local_bblock_schemas.get(
            ref,
            bblocks_register.imported_bblock_schemas.get(ref)
        )

    if target_bblock_id:
        # Search local
        target_bblock = bblocks_register.bblocks.get(target_bblock_id)
        if target_bblock:
            if base_url:
                # If we have a base_url, we return the full URL
                return f"{base_url}{os.path.relpath(target_bblock.annotated_schema)}{fragment}"
            else:
                # Otherwise, the local relative path
                return os.path.relpath(target_bblock.annotated_schema, from_bblock.annotated_path) + fragment
        else:
            target_bblock = bblocks_register.imported_bblocks.get(target_bblock_id)
            if target_bblock and target_bblock.get('schema'):
                return f"{target_bblock['schema']['application/yaml']}{fragment}"

        raise ValueError(f'Error replacing dependency {target_bblock_id}. Is an import missing?')

    # If we're here, ref is unknown -> return the original value
    return f"{ref}{fragment}"


def get_github_repo(url: str) -> tuple[str, str] | None:
    if not url:
        return None
    m = re.match(r'^(?:git@|https?://(?:www)?)github.com[:/](.+)/(.+).git$', url)
    if m:
        groups = m.groups()
        return groups[0], groups[1]
    return None


def get_git_repo_url(url: str) -> str:
    gh_repo = get_github_repo(url)
    if gh_repo:
        return f"https://github.com/{gh_repo[0]}/{gh_repo[1]}"
    return url


def get_git_submodules(repo_path=Path()) -> list[list[str, str]]:
    # Workaround to avoid git errors when using git.Repo.submodules directly
    from git.objects.submodule.util import SubmoduleConfigParser
    parser = SubmoduleConfigParser(repo_path / '.gitmodules', read_only=True)
    return [[parser.get(sms, "path"), parser.get(sms, "url")] for sms in parser.sections()]


def to_oas30(schema_fn: Path, schema_url: str, bbr: BuildingBlockRegister) -> dict:
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
            for bb_id, bb in bbr.bblocks.items():
                if bb.annotated_schema.resolve().with_suffix(ref_base.suffix) == ref_base:
                    new_mapping = bb_id
                    break
        elif ref_base in bbr.imported_bblock_schemas:
            new_mapping = bbr.imported_bblock_schemas[ref_base]

        if not new_mapping:
            # new_mapping = hashlib.sha1(str(ref_base).encode()).hexdigest()
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

    def apply_fixes(parent):
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

    def walk(subschema: dict | list, schema_id: str | Path, is_properties: bool = False) \
            -> tuple[dict | list, str | None, str | Path]:
        schema_version = None
        if isinstance(subschema, list):
            for item in subschema:
                walk(item, schema_id)
        elif isinstance(subschema, dict):

            if not is_properties:
                apply_fixes(subschema)

            schema_version = subschema.pop('$schema', None)
            schema_declared_id = subschema.pop('$id', None)
            if schema_declared_id:
                schema_id = schema_declared_id
            if isinstance(schema_id, (str, Path)) and isinstance(subschema.get('$ref'), str):

                ref = f"{schema_url}#/x-defs/{get_ref_mapping(schema_id, subschema.pop('$ref'))}"

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
        elif cur_ref in bbr.local_bblock_schemas:
            ref_schema = load_yaml(content=bbr.bblocks[bbr.local_bblock_schemas[cur_ref]].annotated_schema_contents)
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
        'x-defs': root_defs,
        'allOf': [
            {'$ref': f"{schema_url}#/x-defs/{root_ref_id}"}
        ]
    }


def sanitize_filename(fn: str):
    return pathvalidate.sanitize_filename(fn)
