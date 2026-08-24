#!/usr/bin/env python3
## http_interceptor needs to be the first import
# to properly monkey-patch urllib and requests
from ogc.bblocks import http_interceptor
import datetime
import json
import logging
import os
import shutil
import subprocess
import sys
from argparse import ArgumentParser
from pathlib import Path

from ogc.bblocks.log import setup_logging, log_indent

from ogc.bblocks import postprocess as postprocess_module
from ogc.bblocks.postprocess import postprocess
from ogc.bblocks.permissions import check_build_plugin_permissions
from ogc.bblocks.hooks.plugin import load_build_plugins, dispatch_after_uplift, dispatch_after_run, \
    dispatch_on_error
from ogc.bblocks.sandbox import SANDBOX_DIR_NAME
from ogc.bblocks.template_sync import check_template_files, ensure_build_script_interactive, sync_lineage_files
from ogc.na import ingest_json, update_vocabs

import jsonschema

from ogc.bblocks.util import get_github_repo, load_yaml, get_schema

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
        '--skip-permissions',
        default='false',
        help='Skip interactive permission prompts for transforms and plugins (set to true in CI)',
    )

    parser.add_argument(
        '--update-template-files',
        default='true',
        help="Whether to update outdated scaffolding files (build.sh, view.sh, ...) inherited "
             "from bblocks-template, and add -it to build.sh's docker run command if missing. "
             "Only ever applied to files that are still an unmodified (if outdated) copy of the "
             "template, so it's never overwriting customizations. Always skipped when "
             "skip_permissions is set, since it's meaningless in CI.",
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
        default='false',
        help='Enable SPARQL push, if configured (set to false to skip pushing entirely, e.g. for PR checks)',
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
    logger = logging.getLogger('ogc.bblocks.entrypoint')

    fail_on_error = args.fail_on_error in ('true', 'on', 'yes', '1')
    skip_permissions = args.skip_permissions in ('true', 'on', 'yes', '1')
    clean = args.clean in ('true', 'on', 'yes', '1')
    deploy_viewer = args.deploy_viewer in ('true', 'on', 'yes', '1')
    enable_sparql = args.enable_sparql in ('true', 'on', 'yes', '1')
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
                "- skip_permissions: %s\n"
                "- clean: %s\n"
                "- config_file: %s\n"
                "- test_outputs_path: %s\n"
                "- github_base_url: %s\n"
                "- filter: %s\n"
                "- steps: %s\n"
                "- deploy_viewer: %s\n"
                "- viewer_path: %s",
                version, args.register_file, args.items_dir, args.generated_docs_path,
                args.base_url, templates_dir, args.annotated_path, fail_on_error, skip_permissions, clean,
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
        try:
            jsonschema.validate(bb_config, get_schema('bblocks-config'))
        except jsonschema.ValidationError as e:
            raise ValueError(f"Invalid bblocks-config.yaml: {e.message} (at {' > '.join(str(p) for p in e.absolute_path)})") from e

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

    viewer_config = {}
    if bb_config:
        raw_viewer = bb_config.get('viewer', {}) or {}
        if 'show-imported-depth' in raw_viewer:
            viewer_config['showImported'] = raw_viewer['show-imported-depth']
        if raw_viewer.get('view-plugins'):
            viewer_config['viewPlugins'] = raw_viewer['view-plugins']

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

    standards_file = Path('standards.yaml')
    if standards_file.is_file():
        standards_config = load_yaml(filename=standards_file) or {}
        if standards := standards_config.get('standards'):
            register_additional_metadata['standards'] = standards

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
    gh_repo = None
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

    update_template_files = args.update_template_files in ('true', 'on', 'yes', '1')
    if not skip_permissions and update_template_files:
        sync_status = check_template_files(Path.cwd(), enabled=True)
        if not sync_status.get('build.sh') and ensure_build_script_interactive(Path.cwd()):
            sys.exit(1)

    # Unlike check_template_files above, this always runs, including in CI:
    # it creates/updates files such as SECURITY.md, which we want committed
    # by the workflow's own "Add & Commit" step, not just fixed up locally.
    sync_lineage_files(Path.cwd(), owner=gh_repo[0] if gh_repo else None, enabled=True)

    steps = args.steps.split(',') if args.steps else None

    sandbox_dir = Path(SANDBOX_DIR_NAME)
    hook_context = {
        'itemsDir': str(items_dir),
        'baseUrl': base_url,
        'registerFile': str(register_file),
        'steps': steps,
        'filter': args.filter,
        'failOnError': fail_on_error,
    }
    build_plugins = []
    postprocess_done = False

    def _load_hook_plugins():
        allowed_build_classes = None if skip_permissions else check_build_plugin_permissions(sandbox_dir)
        plugins, _entries = load_build_plugins(sandbox_dir, allowed_classes=allowed_build_classes)
        return plugins

    def _hook_register():
        # after_uplift/after_run/on_error pass register.json's on-disk form -
        # postprocess() doesn't return the register dict to this caller directly.
        if register_file.exists():
            try:
                return json.loads(register_file.read_text())
            except Exception:
                logger.warning("Could not read %s for build plugin dispatch", register_file, exc_info=True)
        return None

    try:
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
                        skip_permissions=skip_permissions,
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
                        viewer_config=viewer_config,
                        default_license=bb_config.get('license') if bb_config else None,
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

        postprocess_done = True

        # check_build_plugin_permissions()/load_build_plugins() were already called once
        # inside postprocess() - this is the second, entrypoint-side call the design doc
        # describes (a guaranteed cache/memoization hit in the common case), needed
        # because after_uplift/after_run/on_error fire here, after postprocess() returns.
        build_plugins = _load_hook_plugins()

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

        if build_plugins:
            dispatch_after_uplift(build_plugins, sandbox_dir, _hook_register(), hook_context)

        # 3. Push to triplestore
        if enable_sparql:
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

        if build_plugins:
            dispatch_after_run(build_plugins, sandbox_dir, _hook_register(), hook_context)

        logger.info("Finished Building Blocks postprocessing")
    except Exception as e:
        # on_error: mutually exclusive with the after_run dispatch above - this
        # except covers the whole run, from postprocess() through the SPARQL
        # push, so at most one of the two ever fires for a given run.
        if not build_plugins:
            try:
                build_plugins = _load_hook_plugins()
            except Exception:
                logger.exception("Error loading build plugins for on_error dispatch")
        if build_plugins:
            # 'uplift' covers everything from here through the SPARQL push - the
            # design doc's phase enum has no more granular name for that region.
            # Otherwise, postprocess() failed and left a breadcrumb of which of
            # its own internal stages it was in.
            phase = 'uplift' if postprocess_done else getattr(postprocess_module, '_hook_phase', 'before_run')
            dispatch_on_error(build_plugins, sandbox_dir, e, _hook_register(), hook_context, phase=phase)
        raise
