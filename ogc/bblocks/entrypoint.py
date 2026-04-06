#!/usr/bin/env python3
## http_interceptor needs to be the first import
# to properly monkey-patch urllib and requests
from ogc.bblocks import http_interceptor
import datetime
import logging
import os
import shutil
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

from ogc.bblocks.log import setup_logging, log_indent

from ogc.bblocks.postprocess import postprocess
from ogc.na import ingest_json, update_vocabs

from ogc.bblocks.util import get_github_repo, load_yaml

MAIN_BBR = 'https://opengeospatial.github.io/bblocks/register.json'
DEFAULT_IMPORT_MARKER = 'default'

templates_dir = Path(__file__).parent / 'templates'
uplift_context_file = Path(__file__).parent / 'register-context.yaml'
version_file = Path(__file__).parent / '_VERSION'

if __name__ == '__main__':

    parser = ArgumentParser()

    parser.add_argument(
        '--register-file',
        default='build-local/register.json',
        help='Output JSON Building Blocks register document',
    )

    parser.add_argument(
        '--items-dir',
        default='_sources',
        help='Registered items directory',
    )

    parser.add_argument(
        '--generated-docs-path',
        default='build-local/generateddocs',
        help='Output directory for generated documentation',
    )

    parser.add_argument(
        '--base-url',
        help='Base URL for hyperlink generation',
    )

    parser.add_argument(
        '--fail-on-error',
        default='true',
        help='Fail run if an error is encountered',
    )

    parser.add_argument(
        '--annotated-path',
        default='build-local/annotated',
        help='Fail run if an error is encountered',
    )

    parser.add_argument(
        '--clean',
        default='false',
        help='Delete output directories and files before generating the new ones',
    )

    parser.add_argument(
        '--ref-root',
        default='https://raw.githubusercontent.com/opengeospatial/bblocks/master/build/',
        help='Value of $_ROOT_ for usage in $ref values inside JSON schemas'
    )

    parser.add_argument(
        '--config-file',
        default='bblocks-config.yaml',
        help='bblocks-config.yml file, if any'
    )

    parser.add_argument(
        '--test-outputs-path',
        default='build-local/tests',
        help='Directory for test output resources',
    )

    parser.add_argument(
        '--github-base-url',
        help='Base URL for linking to GitHub content',
    )

    parser.add_argument(
        '--filter',
        help='Filter by building block id or file. Forces --clean to false'
    )

    parser.add_argument(
        '--steps',
        help='Comma-separated list of postprocessing steps that will run (annotate,jsonld,'
             'tests,transforms,doc,register). Forces --clean to false'
    )

    parser.add_argument(
        '--deploy-viewer',
        help='Whether the javascript bblocks viewer will be deployed'
    )

    parser.add_argument(
        '--viewer-path',
        help='Path where the viewer will be deployed',
        default='.'
    )

    parser.add_argument(
        '--enable-sparql',
        help='Enable SPARQL push, if configured',
        action='store_true',
    )

    parser.add_argument(
        '--log-level',
        default='INFO',
        help='Logging level (DEBUG, INFO, WARNING, ERROR)',
    )

    parser.add_argument(
        '--log-file',
        default=None,
        help='Optional log file; if provided, all messages are also written there with full timestamps',
    )

    args = parser.parse_args()
    setup_logging(args.log_level, args.log_file)
    logger = logging.getLogger(__name__)

    fail_on_error = args.fail_on_error in ('true', 'on', 'yes', '1')
    clean = args.clean in ('true', 'on', 'yes', '1')
    deploy_viewer = args.deploy_viewer in ('true', 'on', 'yes', '1')
    bb_config_file = Path(args.config_file) if args.config_file else None

    if version_file.is_file():
        with open(version_file) as f:
            version = f.readline().strip() + ' '
    else:
        version = ''

    logger.info("Running %swith the following configuration:\n"
                "- register_file: %s\n"
                "- items_dir: %s\n"
                "- generated_docs_path: %s\n"
                "- base_url: %s\n"
                "- templates_dir: %s\n"
                "- annotated_path: %s\n"
                "- fail_on_error: %s\n"
                "- clean: %s\n"
                "- config_file: %s\n"
                "- test_outputs_path: %s\n"
                "- github_base_url: %s\n"
                "- filter: %s\n"
                "- steps: %s\n"
                "- deploy_viewer: %s\n"
                "- viewer_path: %s",
                version, args.register_file, args.items_dir, args.generated_docs_path,
                args.base_url, templates_dir, args.annotated_path, fail_on_error, clean,
                bb_config_file, args.test_outputs_path, args.github_base_url,
                args.filter, args.steps, deploy_viewer, args.viewer_path)

    register_file = Path(args.register_file)
    register_jsonld_fn = register_file.with_name('bblocks.jsonld')
    if register_file.suffix == '.jsonld':
        register_jsonld_fn = register_jsonld_fn.with_suffix('.jsonld.jsonld')
    register_ttl_fn = register_jsonld_fn.with_suffix('.ttl')
    items_dir = Path(args.items_dir)

    # Clean old output
    if clean and not args.filter and not args.steps:
        for old_file in register_file, register_jsonld_fn, register_ttl_fn:
            logger.info("Deleting %s", old_file)
            old_file.unlink(missing_ok=True)
        cwd = Path().resolve()
        for old_dir in args.generated_docs_path, args.annotated_path, args.test_outputs_path:
            # Only delete if not current path and not ancestor
            old_dir = Path(old_dir).resolve()
            if old_dir != cwd and old_dir not in cwd.parents:
                logger.info("Deleting %s recursively", old_dir)
                shutil.rmtree(old_dir, ignore_errors=True)

    # Fix git config
    try:
        subprocess.run(['git', 'config', '--global', '--add', 'safe.directory', '*'])
    except Exception as e:
        logger.warning("Error configuring git safe.directory: %s", e)

    # Read local bblocks-config.yaml, if present
    id_prefix = 'ogc.'
    annotated_path = Path(args.annotated_path)
    imported_registers = []
    register_additional_metadata = {}
    sparql_conf = {}
    schema_oas30_downcompile = False
    bb_config = {}
    if bb_config_file and bb_config_file.is_file():
        bb_config = load_yaml(filename=bb_config_file) or {}
    for override_name in ('bblocks-config-override.yml', 'bblocks-config-override.yaml'):
        bb_override_config_file = Path(override_name)
        if bb_override_config_file.is_file():
            bb_config.update(load_yaml(filename=bb_override_config_file) or {})
            break
    if bb_config:
        id_prefix = bb_config.get('identifier-prefix', id_prefix)
        if id_prefix and id_prefix[-1] != '.':
            id_prefix += '.'
        subdirs = id_prefix.split('.')[1:]
        imported_registers = bb_config.get('imports')
        if imported_registers is None:
            imported_registers = [MAIN_BBR]
        else:
            imported_registers = [ir if ir != DEFAULT_IMPORT_MARKER else MAIN_BBR for ir in imported_registers if ir]

        for p in ('name', 'abstract', 'description'):
            v = bb_config.get(p)
            if v:
                register_additional_metadata[p] = v

        sparql_conf = bb_config.get('sparql', {}) or {}
        if sparql_conf and sparql_conf.get('query'):
            register_additional_metadata['sparqlEndpoint'] = sparql_conf['query']
        schema_oas30_downcompile = bb_config.get('schema-oas30-downcompile', False)

    bb_local_config_file = Path('bblocks-config-local.yml')
    local_url_mappings = None
    if bb_local_config_file.is_file():
        bb_local_config = load_yaml(filename=bb_local_config_file)
        if bb_local_config.get('imports-local'):
            raise ValueError(
                'Local imports are deprecated, please use local URL mappings instead: '
                'https://ogcincubator.github.io/bblocks-docs/create/imports#local-url-mappings-for-testing'
            )
        local_url_mappings = bb_local_config.get('url-mappings')

    register_additional_metadata['modified'] = datetime.datetime.now().isoformat()

    if os.environ.get('BBP_GIT_INFO_FILE'):
        with open(os.environ['BBP_GIT_INFO_FILE']) as f:
            git_info = f.readline().strip()
        if git_info:
            commit_id, timestamp = git_info.split(' ', 1)
            tooling = register_additional_metadata.setdefault('tooling', {})
            tooling['bblocks-postprocess'] = {
                'commitId': commit_id,
                'shortCommitId': commit_id[0:7],
                'date': timestamp,
            }

    base_url = args.base_url
    github_base_url = args.github_base_url
    git_repo_path = None
    try:
        import git
        repo = git.Repo()
        git_repo_path = Path(repo.working_dir)
        remote_branch = repo.active_branch.tracking_branch()
        remote = repo.remote(remote_branch.remote_name)
        remote_url = next(remote.urls)
        if remote_url:
            register_additional_metadata['gitRepository'] = remote_url

        gh_repo = get_github_repo(remote_url)
        if gh_repo:
            if not base_url:
                base_url = f"https://{gh_repo[0]}.github.io/{gh_repo[1]}/"
            if not github_base_url:
                github_base_url = f"https://github.com/{gh_repo[0]}/{gh_repo[1]}/"
            logger.info("Autodetected GitHub repo %s/%s", gh_repo[0], gh_repo[1])

        if github_base_url:
            register_additional_metadata['gitHubRepository'] = github_base_url
    except Exception as e:
        logger.warning("Could not autodetect base_url / github_base_url: %s", e)

    steps = args.steps.split(',') if args.steps else None

    # 1. Postprocess BBs
    logger.info("Running postprocess...")
    try:
        if local_url_mappings:
            logger.info("Enabling local URL mappings:\n%s",
                        ' - ' + '\n - '.join(f"{k}: {v}" for k, v in local_url_mappings.items()))
            http_interceptor.enable(local_url_mappings)
        postprocess(registered_items_path=items_dir,
                    output_file=args.register_file,
                    base_url=base_url,
                    generated_docs_path=args.generated_docs_path,
                    templates_dir=templates_dir,
                    fail_on_error=fail_on_error,
                    id_prefix=id_prefix,
                    annotated_path=annotated_path,
                    test_outputs_path=args.test_outputs_path,
                    github_base_url=github_base_url,
                    imported_registers=imported_registers,
                    bb_filter=args.filter,
                    steps=steps,
                    git_repo_path=git_repo_path,
                    viewer_path=(args.viewer_path or '.') if deploy_viewer else None,
                    additional_metadata=register_additional_metadata,
                    schemas_oas30_downcompile=schema_oas30_downcompile,
                    local_url_mappings=local_url_mappings,
                    links=[
                        {
                            'rel': 'self',
                            'href': register_ttl_fn,
                            'type': 'text/turtle',
                            'title': 'This Building Blocks Register in RDF Turtle format',
                        },{
                            'rel': 'self',
                            'href': register_jsonld_fn,
                            'type': 'application/ld+json',
                            'title': 'This Building Blocks Register in JSON-LD format',
                        }
                    ])
    finally:
        http_interceptor.disable()

    # 2. Uplift register.json
    logger.info("Running semantic uplift of %s", register_file)
    with log_indent():
        logger.info("- %s", register_jsonld_fn)
        logger.info("- %s", register_ttl_fn)
    # TODO: Entailments
    uplift_args = register_additional_metadata.copy()
    uplift_args.setdefault('baseUrl', base_url or 'https://www.opengis.net/def/bblocks/')
    ingest_json.process_file(register_file,
                             context_fn=uplift_context_file,
                             jsonld_fn=register_jsonld_fn,
                             ttl_fn=register_ttl_fn,
                             provenance_base_uri=args.base_url,
                             transform_args=uplift_args)

    # 3. Push to triplestore
    if args.enable_sparql:
        sparql_gsp = sparql_conf.get('push')
        if sparql_gsp:
            if os.environ.get('SPARQL_USERNAME'):
                auth = (os.environ['SPARQL_USERNAME'], os.environ.get('SPARQL_PASSWORD'))
                logger.info("Pushing %s to SPARQL GSP at %s (user %s)", register_ttl_fn, sparql_gsp, auth[0])
            else:
                auth = None
                logger.info("Pushing %s to SPARQL GSP at %s", register_ttl_fn, sparql_gsp)
            sparql_graph = sparql_conf.get('graph') or base_url
            try:
                update_vocabs.load_vocab(register_ttl_fn,
                                         graph_store=sparql_gsp,
                                         graph_uri=sparql_graph,
                                         auth_details=auth)
            except Exception as e:
                logger.error("Error uploading to SPARQL GSP: %s", e)

    logger.info("Finished Building Blocks postprocessing")
