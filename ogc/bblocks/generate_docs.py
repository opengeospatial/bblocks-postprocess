#!/usr/bin/env python3
from __future__ import annotations

import os.path
import sys
from os.path import relpath
from pathlib import Path
from argparse import ArgumentParser
from typing import Sequence
from urllib.parse import urljoin

from mako.template import Template as MakoTemplate
from mako.lookup import TemplateLookup
from mako import exceptions

from ogc.bblocks import util
from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister
from ogc.na.util import load_yaml

import git


class DocTemplate:

    def __init__(self, metadata_fn: Path):
        metadata = load_yaml(filename=metadata_fn)
        self.metadata_fn = metadata_fn
        self.dir_name = metadata_fn.parent.name

        self.id = metadata.get('id')
        self.mediatype = metadata.get('mediatype')
        self.template_file = metadata_fn.parent / metadata.get('template-file')

        self._lookup = TemplateLookup(directories=[metadata_fn.parent])
        self._template = MakoTemplate(filename=str(self.template_file), lookup=self._lookup)

    def render(self, **kwargs) -> str:
        try:
            return self._template.render(**kwargs)
        except:
            print(exceptions.text_error_template().render(), file=sys.stderr)
            raise


def find_templates(root: Path) -> list[DocTemplate]:
    return [DocTemplate(p) for p in root.glob('*/metadata.yaml') if not p.name.startswith("_")]


class DocGenerator:

    def __init__(self,
                 base_url: str | None = None,
                 output_dir: str | Path = 'generateddocs',
                 templates_dir: str | Path = 'templates',
                 id_prefix: str = '',
                 bblocks_register: BuildingBlockRegister | None = None):
        self.base_url = base_url
        self.output_dir = output_dir if isinstance(output_dir, Path) else Path(output_dir)
        self.templates_dir = templates_dir if isinstance(templates_dir, Path) else Path(templates_dir)
        self.id_prefix = id_prefix or ''
        self.bblocks_register = bblocks_register

        self.templates = find_templates(self.templates_dir)

        for template in self.templates:
            self.output_dir.joinpath(template.dir_name).mkdir(parents=True, exist_ok=True)

        try:
            print("Current path:", Path().resolve(), file=sys.stderr)
            git_repo = git.Repo()
            self.git_repos = {None: util.get_git_repo_url(git_repo.remotes[0].url)}
            for submodule_path, submodule_url in util.get_git_submodules():
                self.git_repos[Path(submodule_path).resolve()] = util.get_git_repo_url(submodule_url)
            print("Found git repos:\n -",
                  '\n - '.join(f"{os.path.relpath(k) if k else 'Default'}: {v}" for k, v in self.git_repos.items()),
                  file=sys.stderr)
        except Exception as e:
            print(f"Error obtaining git information", file=sys.stderr)
            import traceback
            traceback.print_exception(e)
            self.git_repos = None

    def generate_doc(self, bblock: BuildingBlock):
        all_docs = {}

        git_repo = None
        git_path = None
        if self.git_repos:
            git_repo = self.git_repos[None]
            git_path = os.path.relpath(bblock.files_path)
            for repo_path, repo_url in self.git_repos.items():
                if repo_path and repo_path in bblock.files_path.parents:
                    git_repo = repo_url
                    git_path = os.path.relpath(bblock.files_path, repo_path)
                    break

        for template in self.templates:
            tpl_out = self.output_dir / template.dir_name / bblock.subdirs / template.template_file.name
            tpl_out.parent.mkdir(parents=True, exist_ok=True)
            bblock_rel = str(relpath(bblock.files_path, tpl_out.parent))
            assets_rel = str(relpath(bblock.assets_path, tpl_out.parent)) if bblock.assets_path else None
            if self.base_url:
                tpl_out_url = urljoin(self.base_url, relpath(tpl_out))
                bblock_rel = urljoin(tpl_out_url, bblock_rel)
                if assets_rel:
                    assets_rel = urljoin(tpl_out_url, assets_rel)
            with open(tpl_out, 'w') as f:
                f.write(template.render(bblock=bblock,
                                        bblock_rel=bblock_rel,
                                        tplfile=template.template_file,
                                        outfile=tpl_out,
                                        assets_rel=assets_rel,
                                        root_dir=Path(),
                                        base_url=self.base_url,
                                        git_repo=git_repo,
                                        git_path=git_path,
                                        bblocks_register=self.bblocks_register,
                                        ))
                if template.id and template.mediatype:
                    doc_url = f"{self.base_url}{self.output_dir}/" \
                              f"{template.dir_name}/{bblock.subdirs}/{template.template_file.name}"
                    all_docs[template.id] = {
                        'mediatype': template.mediatype,
                        'url': doc_url,
                    }

        bblock.metadata['documentation'] = all_docs


def generate_docs(regs: str | Path | Sequence[str | Path],
                  filter_ids: str | list[str] | None = None,
                  output_dir: str | Path = 'generateddocs',
                  templates_dir: str | Path = 'templates'):
    bblocks_register = BuildingBlockRegister(regs)
    doc_generator = DocGenerator(output_dir, templates_dir)

    for bblock in bblocks_register.bblocks.values():
        if not filter_ids or bblock.identifier in filter_ids:
            doc_generator.generate_doc(bblock)


def _main():
    parser = ArgumentParser()

    parser.add_argument(
        'register_doc',
        nargs='+',
        help='JSON Building Blocks register document(s)',
    )

    parser.add_argument(
        '-i',
        '--filter-id',
        nargs='+',
        help='Only process building blocks matching these ids',
    )

    parser.add_argument(
        '-o',
        '--output-dir',
        help='Output directory',
        default='generateddocs'
    )

    parser.add_argument(
        '-t',
        '--templates-dir',
        help='Templates directory',
        default='templates'
    )

    args = parser.parse_args()

    generate_docs(args.register_doc, filter_ids=args.filter_id, output_dir=args.output_dir,
                  templates_dir=args.templates_dir)


if __name__ == '__main__':
    _main()
