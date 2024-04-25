from __future__ import annotations

import csv
import functools
import json
import os.path
import re
from collections import deque
from pathlib import Path
from typing import Any, Sequence, Callable
from urllib.parse import urljoin, urlparse, urlunparse

import pathvalidate
import requests
from ogc.na.annotate_schema import ContextBuilder
from ogc.na.util import load_yaml, is_url

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from ogc.bblocks.models import BuildingBlockRegister

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


def pathify(v: str | Path):
    if not v:
        return Path(v)
    if isinstance(v, Path):
        return v
    return v if is_url(v) else Path(v)


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
            return PathOrUrl(str(urlunparse(self.parsed_url[0:2] + (str(parsed_path.parent),) + self.parsed_url[3:])))
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

    @property
    def exists(self):
        return self.is_url or self.path.is_file()

    def with_base_url(self, base_url: str | None, from_dir: Path | str | None = None) -> str:
        if self.is_url:
            return self.url
        if not from_dir:
            from_dir = '.'
        relpath = os.path.relpath(self.path.resolve(), from_dir)
        if base_url:
            return f"{base_url}{relpath}"
        else:
            return f"./{relpath}"

    def __repr__(self):
        t = 'url' if self.is_url else 'path'
        return f"PathOrUrl[{t}={self.value}]"


def write_jsonld_context(annotated_schema: Path | str, bblocks_register: BuildingBlockRegister) -> Path | None:
    if not isinstance(annotated_schema, Path):
        annotated_schema = Path(annotated_schema)
    ctx_builder = ContextBuilder(annotated_schema, schema_resolver=bblocks_register.schema_resolver)
    if not ctx_builder.context.get('@context'):
        return None
    context_fn = annotated_schema.resolve().parent / 'context.jsonld'
    with open(context_fn, 'w') as f:
        json.dump(ctx_builder.context, f, indent=2)
    with open(context_fn.parent / '_visited_properties.tsv', 'w', newline='') as f:
        writer = csv.writer(f, delimiter='\t')
        writer.writerow(['path', '@id'])
        for e in ctx_builder.visited_properties.items():
            writer.writerow(e)
    with open(context_fn.parent / '_missed_properties.tsv', 'w', newline='') as fm:
        fm.write('path\n')
        for mp in ctx_builder.missed_properties:
            fm.write(f"{mp}\n")
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


def get_git_submodules(repo_path=Path()) -> list[tuple[str, str]]:
    # Workaround to avoid git errors when using git.Repo.submodules directly
    from git.objects.submodule.util import SubmoduleConfigParser
    parser = SubmoduleConfigParser(repo_path / '.gitmodules', read_only=True)
    return [(parser.get(sms, "path"), str(parser.get(sms, "url"))) for sms in parser.sections()]


def sanitize_filename(fn: str):
    return pathvalidate.sanitize_filename(fn)


def find_references_yaml(fn: PathOrUrl) -> set[str]:
    """
    Finds references to other JSON/YAML documents.

    :param fn: JSON/YAML document
    :return: a set of the referenced documents
    """
    if not fn.exists:
        return set()

    document = fn.load_yaml()

    deps = set()

    def walk(branch):
        if isinstance(branch, dict):
            ref = branch.get(BBLOCKS_REF_ANNOTATION, branch.get('$ref'))
            if isinstance(ref, str):
                # Remove fragment
                ref = re.sub(r'#.*$', '', ref)
                deps.add(ref)

            for prop, val in branch.items():
                if prop not in (BBLOCKS_REF_ANNOTATION, '$ref') or not isinstance(val, str):
                    walk(val)
        elif isinstance(branch, list):
            for item in branch:
                walk(item)

    walk(document)
    return deps


def find_references_xml(contents) -> set[str]:
    """
    Finds references to other XML documents.
    :param contents: XML contents (raw str)
    :return: a set of the referenced documents
    """
    # TODO: Implement for XML schemas
    pass
