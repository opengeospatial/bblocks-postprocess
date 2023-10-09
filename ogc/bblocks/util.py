from __future__ import annotations

import dataclasses
import functools
import json
import os.path
import re
import sys
from collections import deque
from functools import lru_cache
from pathlib import Path
from typing import Any, Sequence, Callable, AnyStr

import jsonschema
import networkx as nx
import requests
from ogc.na.annotate_schema import SchemaAnnotator, ContextBuilder
from ogc.na.util import load_yaml, dump_yaml, is_url

BBLOCK_METADATA_FILE = 'bblock.json'
BBLOCKS_REF_ANNOTATION = 'x-bblocks-ref'

loaded_schemas: dict[str, dict] = {}


def load_file(fn):
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


def get_bblock_subdirs(identifier: str) -> Path:
    return Path(*(identifier.split('.')[1:]))


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

            self.metadata['itemIdentifier'] = identifier

        self._lazy_properties = {}

        self.subdirs = rel_path
        if '.' in self.identifier:
            self.subdirs = get_bblock_subdirs(identifier)

        self.super_bblock = self.metadata.get('superBBlock', False)

        fp = metadata_file.parent
        self.files_path = fp

        schema = fp / 'schema.yaml'
        if not schema.is_file():
            schema = fp / 'schema.json'
        self.schema = schema

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
        self.shacl_rules = [r if is_url(r) else fp / r for r in shacl_rules]

    def _load_examples(self):
        examples = None
        if self.examples_file.is_file():
            examples = load_yaml(self.examples_file)
            try:
                jsonschema.validate(examples, get_schema('examples'))
            except Exception as e:
                raise BuildingBlockError('Error validating building block examples') from e

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
            if not self.schema.exists():
                return None
            self._lazy_properties['schema_contents'] = load_file(self.schema)
        return self._lazy_properties['schema_contents']

    @property
    def description(self):
        if 'description' not in self._lazy_properties:
            desc_file = self.files_path / 'description.md'
            self._lazy_properties['description'] = load_file(desc_file) if desc_file.is_file() else None
        return self._lazy_properties['description']

    def __getattr__(self, item):
        return self.metadata.get(item)

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
                 find_dependencies=True,
                 imported_bblocks: ImportedBuildingBlocks | None = None):

        self.registered_items_path = registered_items_path
        self.annotated_path = annotated_path
        self.prefix = prefix
        self.bblocks: dict[str, BuildingBlock] = {}
        self.imported_bblocks = imported_bblocks.bblocks if imported_bblocks else {}

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

        self.imported_bblock_schemas: dict[str, str] = {}
        if find_dependencies:
            dep_graph = nx.DiGraph()

            for identifier, imported_bblock in self.imported_bblocks.items():
                dep_graph.add_node(identifier)
                dep_graph.add_edges_from([(d, identifier) for d in imported_bblock.get('dependsOn', ())])
                imported_bblock.get('dependsOn', [])
                for schema_url in imported_bblock.get('schema', {}).values():
                    self.imported_bblock_schemas[schema_url] = identifier

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

    def _resolve_bblock_deps(self, bblock: BuildingBlock) -> set[str]:
        if not bblock.schema.is_file():
            return set()
        bblock_schema = load_yaml(filename=bblock.schema)

        deps = set()

        def walk_schema(schema):
            if isinstance(schema, dict):
                ref = schema.get(BBLOCKS_REF_ANNOTATION, schema.get('$ref'))
                if isinstance(ref, str):
                    ref = re.sub(r'#.*$', '', ref)
                    if ref.startswith('bblocks://'):
                        # Get id directly from bblocks:// URI
                        deps.add(ref[len('bblocks://'):])
                    elif ref in self.imported_bblock_schemas:
                        deps.add(self.imported_bblock_schemas[ref])
                    else:
                        ref_parent_path = bblock.files_path.joinpath(ref).resolve().parent
                        ref_bblock = self.bblock_paths.get(ref_parent_path)
                        if ref_bblock:
                            deps.add(ref_bblock.identifier)

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

    def get_inherited_shacl_rules(self, identifier: str) -> set[str | Path]:
        rules = set()
        for dep in self.find_dependencies(identifier):
            if isinstance(dep, BuildingBlock):
                rules.update(dep.shacl_rules or ())
            else:
                rules.update(dep.get('shaclRules', ()))
        return rules


@dataclasses.dataclass
class TransformMetadata:
    type: str
    source_mime_type: str
    target_mime_type: str
    transform_content: AnyStr
    input_data: AnyStr
    metadata: Any | None = None


def write_superbblocks_schemas(super_bblocks: dict[Path, BuildingBlock],
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

        jsonld_context = write_jsonld_context(annotated_output_file)
        if jsonld_context:
            result.append(jsonld_context)

    return result


def write_jsonld_context(annotated_schema: Path) -> Path | None:
    ctx_builder = ContextBuilder(annotated_schema)
    if not ctx_builder.context.get('@context'):
        return None
    context_fn = annotated_schema.parent / 'context.jsonld'
    with open(context_fn, 'w') as f:
        json.dump(ctx_builder.context, f, indent=2)
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
                    context: Path | dict | None = None) -> list[Path]:
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
            schema_fn = ref_schema
    elif bblock.schema.is_file():
        schema_fn = bblock.schema

    if not schema_fn and not schema_url:
        return result

    ref_mapper = functools.partial(resolve_schema_reference,
                                   bblocks_register=bblocks_register,
                                   from_bblock=bblock)

    annotator = SchemaAnnotator(
        ref_mapper=ref_mapper,
    )

    bb_extends = bblock.extends
    override_schema = None
    if bb_extends:
        bb_path = None
        if isinstance(bb_extends, dict):
            bb_path = bb_extends.get('path')
            bb_extends = bb_extends['itemIdentifier']

        original_schema = load_yaml(filename=schema_fn, url=schema_url)
        original_schema.pop('$schema', None)
        if bb_path in (None, '', '.', '$'):
            inserted_schema = original_schema
        else:
            bb_path = re.split(r'\.(?=(?:[^"]*"[^"]*")*[^"]*$)',
                               re.sub(r'^[\.\$]', '', bb_path.strip()))
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
            for k, v in original_schema.items():
                if k != '$schema' and not k.startswith('x-jsonld-'):
                    inner_schema[k] = v

        override_schema = {
            '$schema': 'https://json-schema.org/draft/2020-12/schema',
            'allOf': [
                {'$ref': f"bblocks://{bb_extends}"},
                inserted_schema,
            ],
            **{k: v for k, v in original_schema.items() if k.startswith('x-jsonld-')}
        }

    annotated_schema = annotator.process_schema(schema_url or schema_fn, context, override_schema)

    if not annotated_schema:
        return result

    annotated_schema = annotated_schema.schema
    if schema_url and '$id' not in annotated_schema:
        annotated_schema['$id'] = schema_url

    result = []

    # YAML
    annotated_schema_fn = bblock.annotated_path / 'schema.yaml'
    annotated_schema_fn.parent.mkdir(parents=True, exist_ok=True)
    dump_yaml(annotated_schema, annotated_schema_fn)
    result.append(annotated_schema_fn)

    def update_json_ref(ref):
        if ref in bblocks_register.imported_bblock_schemas or not is_url(ref):
            return re.sub(r'\.yaml(#.*)?$', r'.json\1', ref)
        return ref

    # JSON
    update_refs(annotated_schema, update_json_ref)
    annotated_schema_json_fn = annotated_schema_fn.with_suffix('.json')
    with open(annotated_schema_json_fn, 'w') as f:
        json.dump(annotated_schema, f, indent=2)
    result.append(annotated_schema_json_fn)
    return result


def resolve_schema_reference(ref: str,
                             schema: Any,
                             bblocks_register: BuildingBlockRegister,
                             from_bblock: BuildingBlock | None = None) -> str:

    ref = schema.pop(BBLOCKS_REF_ANNOTATION, ref)

    if not ref.startswith('bblocks://'):
        return ref

    target_id = ref[len('bblocks://'):]
    fragment = ''
    if '#' in target_id:
        target_id, fragment = target_id.split('#', 1)
        if fragment:
            fragment = '#' + fragment

    target_bb = bblocks_register.bblocks.get(target_id)
    if target_bb:
        if from_bblock:
            return os.path.relpath(target_bb.annotated_schema, from_bblock.annotated_path)
        else:
            return ref
    else:
        target_bb = bblocks_register.imported_bblocks.get(target_id)
        if not target_bb or not target_bb.get('schema'):
            raise ValueError(f'Error replacing dependency {target_id}. Is an import missing?')
        return f"{target_bb['schema']['application/yaml']}{fragment}"


def get_git_repo_url(url: str) -> str:
    if not url:
        return url
    m = re.match(r'^(?:git@|https?://(?:www)?)github.com[:/](.+)/(.+).git$', url)
    if m:
        groups = m.groups()
        return f"https://github.com/{groups[0]}/{groups[1]}"
    return url


def get_git_submodules(repo_path=Path()) -> list[list[str, str]]:
    # Workaround to avoid git errors when using git.Repo.submodules directly
    from git.objects.submodule.util import SubmoduleConfigParser
    parser = SubmoduleConfigParser(repo_path / '.gitmodules', read_only=True)
    return [[parser.get(sms, "path"), parser.get(sms, "url")] for sms in parser.sections()]
