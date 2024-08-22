from __future__ import annotations

import itertools
import json
import os.path
import re
import subprocess
import sys
from argparse import ArgumentParser
import datetime
from pathlib import Path
import traceback
from urllib.parse import urljoin

from ogc.na.util import is_url, dump_yaml

from ogc.bblocks.generate_docs import DocGenerator
from ogc.bblocks.oas30 import oas31_to_oas30
from ogc.bblocks.util import write_jsonld_context, CustomJSONEncoder, \
    PathOrUrl
from ogc.bblocks.schema import annotate_schema, resolve_all_schema_references
from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister, ImportedBuildingBlocks, BuildingBlockError
from ogc.bblocks.validate import validate_test_resources, report_to_html
from ogc.bblocks.transform import apply_transforms, transformers


def postprocess(registered_items_path: str | Path = 'registereditems',
                output_file: str | Path | None = 'register.json',
                base_url: str | None = None,
                generated_docs_path: str | Path = 'generateddocs',
                templates_dir: str | Path = 'templates',
                fail_on_error: bool = False,
                id_prefix: str = '',
                annotated_path: str | Path = 'annotated',
                test_outputs_path: str | Path = 'build/tests',
                github_base_url: str | None = None,
                imported_registers: list[str] | None = None,
                bb_filter: str | None = None,
                steps: list[str] | None = None,
                bbr_config: dict | None = None,
                git_repo_path: Path | None = None,
                viewer_path: str | Path | None = None,
                additional_metadata: dict | None = None,
                import_local_mappings: dict[str, str] | None = None) -> list[dict]:

    cwd = Path().resolve()

    if bbr_config is None:
        bbr_config = {}

    if not isinstance(test_outputs_path, Path):
        test_outputs_path = Path(test_outputs_path)
    if not steps or 'tests' in steps:
        test_outputs_path.mkdir(parents=True, exist_ok=True)

    if base_url and base_url[-1] != '/':
        base_url += '/'

    test_outputs_base_url = None
    if github_base_url:
        if github_base_url[-1] != '/':
            github_base_url += '/'
        test_outputs_base_url = f"{github_base_url}{os.path.relpath(Path(test_outputs_path).resolve(), cwd)}/"

    if not isinstance(registered_items_path, Path):
        registered_items_path = Path(registered_items_path)

    child_bblocks = []
    super_bblocks = {}
    imported_bblocks = ImportedBuildingBlocks(imported_registers, local_mappings=import_local_mappings)
    bbr = BuildingBlockRegister(registered_items_path,
                                fail_on_error=fail_on_error,
                                prefix=id_prefix,
                                annotated_path=annotated_path,
                                imported_bblocks=imported_bblocks,
                                base_url=base_url)

    doc_generator = DocGenerator(base_url=base_url,
                                 output_dir=generated_docs_path,
                                 templates_dir=templates_dir,
                                 id_prefix=id_prefix,
                                 bblocks_register=bbr)

    validation_reports = []

    def do_postprocess(bblock: BuildingBlock, light: bool = False) -> bool:

        try:
            last_git_modified = datetime.datetime.fromisoformat(subprocess.run([
                'git',
                'log',
                '-1',
                '--pretty=format:%cI',
                str(bblock.files_path),
            ], capture_output=True).stdout.decode()).astimezone(datetime.timezone.utc).date()
        except (ValueError, OSError):
            last_git_modified = None

        last_update = bblock.metadata.get('dateOfLastChange')
        if last_update:
            try:
                last_update = datetime.date.fromisoformat(last_update)
                if last_git_modified and last_git_modified > last_update:
                    last_update = last_git_modified
            except ValueError:
                last_update = last_git_modified

        if last_update:
            bblock.metadata['dateOfLastChange'] = last_update.isoformat()
        else:
            bblock.metadata.pop('dateOfLastChange', None)

        output_file_root = Path(output_file).resolve().parent
        if bblock.annotated_schema.is_file():
            schema_url_yaml = PathOrUrl(bblock.annotated_schema).with_base_url(
                base_url, cwd if base_url else output_file_root
            )
            schema_url_json = re.sub(r'\.yaml$', '.json', schema_url_yaml)
            bblock.metadata['schema'] = {
                'application/yaml': schema_url_yaml,
                'application/json': schema_url_json,
            }
            bblock.metadata['sourceSchema'] = bblock.schema.with_base_url(
                base_url, cwd if base_url else output_file_root
            )
        if bblock.metadata.get('ldContext'):
            bblock.metadata['sourceLdContext'] = PathOrUrl(bblock.metadata['ldContext']).with_base_url(
                base_url, cwd if base_url else output_file_root
            )
        if bblock.jsonld_context.is_file():
            bblock.metadata['ldContext'] = PathOrUrl(bblock.jsonld_context).with_base_url(
                base_url, cwd if base_url else output_file_root
            )
        elif bblock.metadata.get('ldContext') and not is_url(bblock.metadata['ldContext']):
            # Unprocessed JSON-LD context instead of generated from annotations
            ld_context_path = bblock.files_path / bblock.metadata['ldContext']
            bblock.metadata['ldContext'] = PathOrUrl(ld_context_path).with_base_url(
                base_url, cwd if base_url else output_file_root
            )
        if bblock.output_openapi.is_file():
            bblock.metadata['sourceOpenAPIDocument'] = bblock.openapi.with_base_url(
                base_url, cwd if base_url else output_file_root
            )
            bblock.metadata['openAPIDocument'] = PathOrUrl(bblock.output_openapi).with_base_url(
                base_url, cwd if base_url else output_file_root
            )
            if bblock.output_openapi_30.is_file():
                bblock.metadata['openAPI30DowncompiledDocument'] = PathOrUrl(bblock.output_openapi_30).with_base_url(
                    base_url, cwd if base_url else output_file_root
                )

        bblock.metadata['sourceFiles'] = PathOrUrl(bblock.files_path).with_base_url(
            base_url, cwd if base_url else output_file_root
        ) + '/'

        if not light:
            if not steps or 'tests' in steps:
                print(f"  > Running tests for {bblock.identifier}", file=sys.stderr)
                validation_passed, test_count, json_report = validate_test_resources(
                    bblock,
                    bblocks_register=bbr,
                    outputs_path=test_outputs_path,
                    base_url=base_url)
                validation_reports.append(json_report)

                bblock.metadata['validationPassed'] = validation_passed
                # if not validation_passed:
                #     bblock.metadata['status'] = 'invalid'
                if test_count and test_outputs_base_url:
                    bblock.metadata['testOutputs'] = f"{test_outputs_base_url}{bblock.subdirs}/"

        if not light and (not steps or 'transforms' in steps):
            print(f"  > Running transforms for {bblock.identifier}", file=sys.stderr)
            apply_transforms(bblock, outputs_path=test_outputs_path)

        if bblock.examples:
            for example in bblock.examples:
                for snippet in example.get('snippets', ()):
                    path = snippet.pop('path', None)
                    if base_url and path:
                        snippet['url'] = f"{base_url}{path}"

        if base_url:
            if bblock.shaclRules:
                if isinstance(bblock.shaclRules, list):
                    bblock.metadata['shaclRules'] = {bblock.identifier: bblock.shacl_rules}
                bblock.metadata['shaclRules'] = {k: [urljoin(base_url, str(s)) for s in v]
                                                 for k, v in bblock.shaclRules.items()}
            if bblock.transforms:
                bblock.metadata['transforms'] = []
                for transform in bblock.transforms:
                    transform['ref'] = urljoin(bblock.metadata['sourceFiles'], transform['ref'])
                    bblock.metadata['transforms'].append({k: v for k, v in transform.items() if k != 'code'})

            for step in bblock.semantic_uplift.get('additionalSteps', ()):
                if step.get('ref'):
                    step['ref'] = PathOrUrl(bblock.files_path).resolve_ref(step['ref']).with_base_url(
                        base_url, cwd if base_url else output_file_root
                    )

        if not light and (not steps or 'doc' in steps):
            print(f"  > Generating documentation for {bblock.identifier}", file=sys.stderr)
            doc_generator.generate_doc(bblock)

        if base_url:
            if viewer_path:
                bblock.metadata.setdefault('documentation', {})['bblocks-viewer'] = {
                    'mediatype': 'text/html',
                    'url': urljoin(base_url, f"{viewer_path}/bblock/{bblock.identifier}"),
                }

        return True

    filter_id = None
    if bb_filter:
        filter_id = False
        filter_p = Path(bb_filter)
        if filter_p.exists():
            # Find closest bblocks.json
            for p in itertools.chain((filter_p,), filter_p.parents):
                p = p.resolve()
                for bb in bbr.bblocks.values():
                    if p in (bb.files_path, bb.tests_dir.resolve(), bb.annotated_path.resolve()):
                        filter_id = bb.identifier
                        break
                if filter_id:
                    break
        else:
            filter_id = bb_filter

    if transformers.transform_modules:
        print("Available transformers:", file=sys.stderr)
        for t in transformers.transform_modules:
            print(f"  - {t.transform_type}", file=sys.stderr)
    else:
        print("No transformers found", file=sys.stderr)

    for building_block in bbr.bblocks.values():
        if building_block.super_bblock:
            super_bblocks[building_block.files_path] = building_block
            continue

        if filter_id is None or building_block.identifier == filter_id:
            if not steps or 'annotate' in steps:

                if building_block.schema.exists:

                    if building_block.schema.is_url:
                        # Force caching remote file
                        building_block.schema_contents

                    # Annotate schema
                    print(f"Annotating schema for {building_block.identifier}", file=sys.stderr)

                    if building_block.ldContext:
                        if is_url(building_block.ldContext):
                            # Use URL directly
                            default_jsonld_context = building_block.ldContext
                            # Force caching remote file
                            building_block.jsonld_context_contents
                        else:
                            # Use path relative to bblock.json
                            default_jsonld_context = building_block.files_path / building_block.ldContext
                    else:
                        # Try local context.jsonld
                        default_jsonld_context = building_block.files_path / 'context.jsonld'
                        if not default_jsonld_context.is_file():
                            default_jsonld_context = None

                    if default_jsonld_context:
                        building_block.metadata['ldContext'] = str(default_jsonld_context)

                    try:
                        for annotated in annotate_schema(building_block,
                                                         bblocks_register=bbr,
                                                         context=default_jsonld_context,
                                                         base_url=base_url):
                            print(f"  - {annotated}", file=sys.stderr)
                    except Exception as e:
                        if fail_on_error:
                            raise
                        traceback.print_exception(e, file=sys.stderr)

                if building_block.openapi.exists:
                    print(f"Annotating OpenAPI document for {building_block.identifier}", file=sys.stderr)
                    try:
                        openapi_resolved = resolve_all_schema_references(building_block.openapi.load_yaml(), bbr,
                                                                         building_block, building_block.openapi, base_url)
                        building_block.output_openapi.parent.mkdir(parents=True, exist_ok=True)
                        dump_yaml(openapi_resolved, building_block.output_openapi)
                        print(f"  - {os.path.relpath(building_block.output_openapi)}", file=sys.stderr)

                        if openapi_resolved.get('openapi', '').startswith('3.1'):
                            print(f"Downcompiling OpenAPI document to 3.0 for {building_block.identifier}", file=sys.stderr)
                            oas30_doc_fn = building_block.output_openapi_30
                            oas30_doc = oas31_to_oas30(openapi_resolved,
                                                       PathOrUrl(oas30_doc_fn).with_base_url(base_url),
                                                       bbr)
                            dump_yaml(oas30_doc, oas30_doc_fn)
                            print(f"  - {os.path.relpath(oas30_doc_fn)}", file=sys.stderr)
                    except Exception as e:
                        print(f"WARNING: {type(e).__name__} while downcompiling OpenAPI to 3.0:", e)

            if building_block.ontology.exists:
                building_block.metadata.pop('ontology', None)
                try:
                    if building_block.ontology.is_path and building_block.ontology_graph:
                        building_block.output_ontology.parent.mkdir(parents=True, exist_ok=True)
                        building_block.ontology_graph.serialize(building_block.output_ontology, 'ttl')
                        building_block.metadata['ontology'] = PathOrUrl(building_block.output_ontology)\
                            .with_base_url(base_url)
                    elif building_block.ontology.is_url:
                        # Force cache
                        building_block.ontology_graph
                        building_block.metadata['ontology'] = building_block.ontology.value
                except Exception as e:
                    if fail_on_error:
                        raise BuildingBlockError(f'Error processing ontology for {building_block.identifier}')\
                            from e
                    print("Exception when processing ontology for", building_block.identifier, file=sys.stderr)
                    traceback.print_exception(e, file=sys.stderr)

        if base_url and building_block.remote_cache_dir.is_dir():
            building_block.metadata['remoteCacheDir'] = (
                    base_url + os.path.relpath(building_block.remote_cache_dir.resolve(), cwd) + '/'
            )

        child_bblocks.append(building_block)

    if not steps or 'jsonld' in steps:
        print(f"Writing JSON-LD contexts", file=sys.stderr)
        # Create JSON-lD contexts
        for building_block in child_bblocks:
            if filter_id is not None and building_block.identifier != filter_id:
                continue
            if building_block.annotated_schema.is_file():
                try:
                    written_context = write_jsonld_context(building_block.annotated_schema, bbr)
                    if written_context:
                        try:
                            nodejsrun = subprocess.run([
                                'node',
                                str(Path(__file__).parent.joinpath('validation/validate-jsonld.js')),
                                str(written_context),
                            ], capture_output=True)
                            if nodejsrun.returncode == 26:  # validation error
                                raise ValueError(nodejsrun.stdout.decode())
                            elif nodejsrun.returncode == 0:
                                written_context = f"{written_context} (validated)"
                        except FileNotFoundError:
                            # node not installed
                            pass
                        print(f"  - {os.path.relpath(written_context)}", file=sys.stderr)
                except Exception as e:
                    if fail_on_error:
                        raise e
                    print(f"[Error] Writing context for {building_block.identifier}: {type(e).__name__}: {e}")

    output_bblocks = []
    for building_block in itertools.chain(child_bblocks, super_bblocks.values()):
        light = filter_id is not None and building_block.identifier != filter_id
        lightmsg = ' (light)' if light else ''
        print(f"Postprocessing building block {building_block.identifier}{lightmsg}", file=sys.stderr)
        if do_postprocess(building_block, light=light):
            output_bblocks.append(building_block.metadata)
        else:
            print(f"{building_block.identifier} failed postprocessing, skipping...", file=sys.stderr)

    full_validation_report_url = None
    if not steps or 'tests' in steps:
        print(f"Writing full validation report to {test_outputs_path / 'report.html'}", file=sys.stderr)
        if base_url:
            full_validation_report_url = (f"{base_url}{os.path.relpath(Path(test_outputs_path).resolve(), cwd)}"
                                          f"/report.html")
        report_to_html(json_reports=validation_reports,
                       report_fn=test_outputs_path / 'report.html',
                       base_url=base_url)

    if output_file and (not steps or 'register' in steps):

        output_register_json = {}

        if 'name' not in additional_metadata and git_repo_path:
            output_register_json['name'] = git_repo_path.name

        if base_url:
            output_register_json['baseURL'] = base_url
            if viewer_path:
                output_register_json['viewerURL'] = urljoin(base_url, viewer_path)

        if full_validation_report_url:
            output_register_json['validationReport'] = full_validation_report_url

        if additional_metadata:
            output_register_json = {
                **additional_metadata,
                **output_register_json,
            }

        if imported_bblocks.real_metadata_urls:
            output_register_json['imports'] = list(imported_bblocks.real_metadata_urls.values())
        output_register_json['bblocks'] = output_bblocks

        if output_file == '-':
            print(json.dumps(output_register_json, indent=2, cls=CustomJSONEncoder))
        else:
            with open(output_file, 'w') as f:
                json.dump(output_register_json, f, indent=2, cls=CustomJSONEncoder)

    print(f"Finished processing {len(output_bblocks)} building blocks", file=sys.stderr)
    return output_bblocks


def _main():
    parser = ArgumentParser()

    parser.add_argument(
        'output_register',
        default='register.json',
        help='Output JSON Building Blocks register document',
    )

    parser.add_argument(
        '-u',
        '--base-url',
        help='Base URL for hyperlink generation',
    )

    parser.add_argument(
        'registered_items',
        default='registereditems',
        help='Registered items directory',
    )

    parser.add_argument(
        '-N',
        '--no-output',
        action='store_true',
        help='Do not generate output JSON register document',
    )

    parser.add_argument(
        '-s',
        '--metadata-schema',
        help="JSON schema for Building Block metadata validation",
    )

    parser.add_argument(
        '-t',
        '--templates-dir',
        help='Templates directory',
        default='templates'
    )

    parser.add_argument(
        '--fail-on-error',
        action='store_true',
        help='Fail run if an error is encountered',
    )

    parser.add_argument(
        '-p',
        '--identifier-prefix',
        default='ogc.',
        help='Building blocks identifier prefix',
    )

    args = parser.parse_args()

    postprocess(args.registered_items,
                output_file=None if args.no_output else args.output_register,
                base_url=args.base_url,
                templates_dir=args.templates_dir,
                fail_on_error=args.fail_on_error,
                id_prefix=args.identifier_prefix)


if __name__ == '__main__':
    _main()
