from __future__ import annotations

import json
import os
import re
import shutil
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence
from urllib.parse import urljoin

from mako import exceptions as mako_exceptions, template as mako_template
from ogc.na.util import is_url

from ogc.bblocks import mimetypes
from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister
from ogc.bblocks.util import sanitize_filename
from ogc.bblocks.validation import Validator, ValidationItemSourceType, ValidationReportSection, ValidationItemSource, \
    ValidationReportEntry, ValidationReportItem
from ogc.bblocks.validation.json_ import JsonValidator
from ogc.bblocks.validation.plugin import PluginValidator
from ogc.bblocks.validation.rdf import RdfValidator

import logging

logger = logging.getLogger(__name__)

OUTPUT_SUBDIR = 'output'
FORMAT_ALIASES = {
    'turtle': 'ttl',
    'json-ld': 'jsonld',
    'application/ld+json': 'jsonld',
    'text/turtle': 'ttl',
}
DEFAULT_UPLIFT_FORMATS = ['jsonld', 'ttl']


def load_validation_plugins(sandbox_dir: Path,
                            allowed_modules: set[str] | None = None,
                            ) -> tuple[list[PluginValidator], list[dict]]:
    """Read validator plugin config, create per-plugin venvs, and return PluginValidator instances.

    Reads from ``plugins.validators`` in bblocks-config.yaml.
    allowed_modules: if provided, only install/register modules in this set. Pass None to allow all.

    Returns a tuple of (plugin_validators, register_entries) where register_entries is the enriched
    plugin list suitable for inclusion in register.json under 'validatorPlugins'.
    """
    from ogc.bblocks.transform import read_plugin_entries, _pip_to_url

    plugin_entries = read_plugin_entries('validators')
    if not plugin_entries:
        return [], []

    result: list[PluginValidator] = []
    output_plugins: list[dict] = []

    for plugin in plugin_entries:
        pip_deps = plugin.get('pip', [])
        if isinstance(pip_deps, str):
            pip_deps = [pip_deps]

        modules = plugin.get('modules', [])
        if isinstance(modules, str):
            modules = [modules]

        output_modules = []

        for module_path in modules:
            if allowed_modules is not None and module_path not in allowed_modules:
                logger.info("Skipping validator plugin '%s': not permitted by user", module_path)
                continue

            if pip_deps:
                logger.info("Installing validator plugin pip dependencies for '%s': %s",
                            module_path, pip_deps)
            else:
                logger.info("Setting up validator plugin venv for '%s'", module_path)
            venv_dir = PluginValidator.ensure_venv_for(pip_deps, sandbox_dir)

            discovered = PluginValidator.discover(venv_dir, module_path)
            if discovered is None:
                raise RuntimeError(
                    f"Validator plugin '{module_path}' could not be loaded — "
                    "check that the module path is correct and all pip dependencies are declared"
                )
            if not discovered:
                logger.warning("No validator classes found in plugin '%s'", module_path)
                continue

            output_validators = []
            for entry in discovered:
                mime_types = entry.get('mime_types', [])
                file_extensions = entry.get('file_extensions', [])
                if not mime_types and not file_extensions:
                    continue
                pv = PluginValidator(
                    module_path=module_path,
                    class_name=entry['class'],
                    pip_deps=pip_deps,
                    sandbox_dir=sandbox_dir,
                    mime_types=mime_types,
                    file_extensions=file_extensions,
                )
                logger.info("Registered validator plugin '%s' (%s) for mime_types=%s extensions=%s",
                            module_path, entry['class'], mime_types, file_extensions)
                result.append(pv)
                output_validators.append({
                    'class': entry['class'],
                    'mimeTypes': mime_types,
                    'fileExtensions': file_extensions,
                })

            if output_validators:
                output_modules.append({'module': module_path, 'validators': output_validators})

        if output_modules:
            output_entry: dict = {'modules': output_modules}
            original_pip = plugin.get('pip')
            if original_pip:
                output_entry['pip'] = original_pip
                if explicit_url := plugin.get('url'):
                    output_entry['urls'] = [explicit_url]
                else:
                    urls = [u for s in pip_deps for u in [_pip_to_url(s)] if u]
                    if urls:
                        output_entry['urls'] = urls
            output_plugins.append(output_entry)

    return result, output_plugins


def report_to_dict(bblock: BuildingBlock,
                   items: Sequence[ValidationReportItem] | None,
                   base_url: str | None = None) -> dict:
    result = {
        'title': f"Validation report for {bblock.identifier} - {bblock.name}",
        'bblockName': bblock.name,
        'bblockId': bblock.identifier,
        'generated': datetime.now(timezone.utc).astimezone().isoformat(),
        'result': True,
        'items': [],
    }

    global_errors = {}
    cwd = Path().resolve()

    failed_count = 0
    if items:
        for item in items:
            source = {
                'type': item.source.type.name,
                'requireFail': item.source.require_fail,
            }
            if item.failed:
                result['result'] = False
                failed_count += 1
            if item.source.filename:
                source['filename'] = str(os.path.relpath(item.source.filename, cwd))
                if base_url:
                    source['url'] = urljoin(base_url, source['filename'])
            if item.source.example_index:
                source['exampleIndex'] = item.source.example_index
                if item.source.snippet_index:
                    source['snippetIndex'] = item.source.snippet_index
            if item.source.language:
                source['language'] = item.source.language
            if item.source.source_url:
                source['sourceUrl'] = item.source.source_url
            if item.source.transform_id:
                source['transformId'] = item.source.transform_id

            sections = []
            for section_enum, entries in item.sections.items():
                if not entries:
                    continue
                section = {
                    'name': section_enum.name,
                    'title': section_enum.value,
                    'entries': [],
                }
                sections.append(section)
                for entry in entries:
                    entry_dict = {}
                    if entry.payload:
                        for k, v in entry.payload.items():
                            if isinstance(v, Path):
                                v = str(os.path.relpath(v.resolve(), cwd))
                                if base_url:
                                    v = urljoin(base_url, v)
                            elif k == 'files' and isinstance(v, list):
                                fv = []
                                for f in v:
                                    if isinstance(f, Path):
                                        f = str(os.path.relpath(f.resolve(), cwd))
                                    if base_url:
                                        f = urljoin(base_url, f)
                                    fv.append(f)
                                v = fv
                            entry_dict[k] = v
                    entry_dict['isError'] = entry.is_error
                    entry_dict['message'] = entry.message
                    if not entry.is_global:
                        section['entries'].append(entry_dict)
                    elif entry.is_error:
                        global_errors.setdefault(section_enum.name, entry_dict)

            res_item = {
                'source': source,
                'result': not item.failed,
                'sections': sections,
            }
            result['items'].append(res_item)

    result['globalErrors'] = global_errors
    result['counts'] = {
        'total': len(result['items']),
        'passed': len(result['items']) - failed_count,
        'failed': failed_count,
    }

    return result


def write_report(json_reports: list[dict],
                 base_url: str | None = None,
                 report_fn: Path | None = None,
                 json_report_fn: Path | None = None) -> str | None:
    pass_count = sum(r['result'] for r in json_reports)
    counts = {
        'total': len(json_reports),
        'passed': pass_count,
        'failed': len(json_reports) - pass_count,
    }

    if json_report_fn:
        output_report = {
            'summary': {**counts, 'result': counts['failed'] == 0},
            'bblocks': {report['bblockId']: report for report in json_reports}
        }
        with open(json_report_fn, 'w') as f:
            json.dump(output_report, f, indent=2)

    template = mako_template.Template(filename=str(Path(__file__).parent / 'validation/report.html.mako'))
    try:
        result = template.render(reports=json_reports, counts=counts, report_fn=report_fn, base_url=base_url)
    except:
        raise ValueError(mako_exceptions.text_error_template().render())

    if report_fn:
        with open(report_fn, 'w') as f:
            f.write(result)
    else:
        return result


def _validate_resource(bblock: BuildingBlock,
                       filename: Path,
                       output_filename: Path,
                       validators: list,
                       resource_contents: str | None = None,
                       additional_shacl_closures: list[str | Path] | None = None,
                       base_uri: str | None = None,
                       schema_ref: str | None = None,
                       require_fail: bool | None = None,
                       resource_url: str | None = None,
                       example_index: tuple[int, int] | None = None,
                       prefixes: dict[str, str] | None = None,
                       file_format: str | None = None,
                       bblocks_register: BuildingBlockRegister | None = None,
                       validation_resources: list[dict] | None = None) -> ValidationReportItem | None:
    if require_fail is None:
        require_fail = filename.stem.endswith('-fail') and not example_index

    resource_url_for_item_source = resource_url if resource_url and is_url(resource_url) else None

    if example_index:
        example_idx, snippet_idx = example_index
        source = ValidationItemSource(
            type=ValidationItemSourceType.EXAMPLE,
            filename=output_filename,
            example_index=int(example_idx),
            snippet_index=int(snippet_idx),
            language=file_format,
            source_url=resource_url_for_item_source,
        )
    else:
        source = ValidationItemSource(
            type=ValidationItemSourceType.TEST_RESOURCE,
            filename=filename,
            language=filename.suffix[1:],
            require_fail=require_fail,
            source_url=resource_url_for_item_source,
        )
    report = ValidationReportItem(source)

    any_validator_run = False
    try:
        for validator in validators:
            result = validator.validate(filename, output_filename, report,
                                        contents=resource_contents,
                                        base_uri=base_uri,
                                        additional_shacl_closures=additional_shacl_closures,
                                        schema_ref=schema_ref,
                                        prefixes=prefixes,
                                        file_format=file_format,
                                        resource_url=resource_url,
                                        bblock=bblock,
                                        bblocks_register=bblocks_register,
                                        validation_resources=validation_resources)
            any_validator_run = any_validator_run or (result is not False)

    except Exception as unknown_exc:
        report.add_entry(ValidationReportEntry(
            section=ValidationReportSection.UNKNOWN,
            message=','.join(traceback.format_exception(unknown_exc)),
            is_error=True,
            is_global=False,
            payload={
                'exception': unknown_exc.__class__.__qualname__,
            }
        ))

    if not any_validator_run and not report.failed:
        return None

    failed = report.failed
    if require_fail and not report.general_errors:
        msg = 'but it did not.' if failed else 'and it did.'
        report.add_entry(ValidationReportEntry(
            section=ValidationReportSection.GENERAL,
            message=f"Test was expected to fail {msg}",
            is_error=failed,
            payload={
                'op': 'require-fail',
            }
        ))

    status = 'failed' if failed else 'passed'
    report_fn = output_filename.with_suffix(f'.validation_{status}.txt')
    report.write_text(bblock, report_fn)

    return report


def validate_transform_output(
        profile_bblock: BuildingBlock,
        bblocks_register: BuildingBlockRegister,
        transform_id: str,
        output_file: Path,
        profile_output_base: Path,
        plugin_validators: list[PluginValidator] = (),
) -> ValidationReportItem:
    """Validate a transform output file against a profile building block.

    profile_output_base is the path (with extension) inside the per-profile
    subdirectory, used as the base for side-output naming: rdf.py calls
    .with_suffix('.ttl') / '.jsonld' on it, so the extension must be present
    to avoid stripping part of the stem.

    Returns the ValidationReportItem so the caller can collect items across
    snippets and write a consolidated _report.json per profile.
    """
    validators = [
        JsonValidator(profile_bblock, bblocks_register),
        RdfValidator(profile_bblock, bblocks_register),
        *plugin_validators,
    ]

    mime_type = mimetypes.from_extension(output_file.suffix[1:]) if output_file.suffix else None

    source = ValidationItemSource(
        type=ValidationItemSourceType.TRANSFORM_OUTPUT,
        filename=output_file,
        transform_id=transform_id,
        language=mime_type,
    )
    report = ValidationReportItem(source)

    try:
        for validator in validators:
            validator.validate(
                output_file, profile_output_base, report,
                file_format=mime_type,
                bblock=profile_bblock,
                bblocks_register=bblocks_register,
                validation_resources=profile_bblock.validation_resources,
            )
    except Exception as unknown_exc:
        report.add_entry(ValidationReportEntry(
            section=ValidationReportSection.UNKNOWN,
            message=','.join(traceback.format_exception(unknown_exc)),
            is_error=True,
            is_global=False,
            payload={'exception': unknown_exc.__class__.__qualname__}
        ))

    status = 'failed' if report.failed else 'passed'
    report.write_text(profile_bblock, profile_output_base.with_suffix(f'.validation_{status}.txt'))

    return report


def validate_test_resources(bblock: BuildingBlock,
                            bblocks_register: BuildingBlockRegister,
                            outputs_path: Path | None = None,
                            base_url: str | None = None,
                            plugin_validators: list[PluginValidator] = ()) -> tuple[bool, int, dict]:
    final_result = True
    test_count = 0

    if outputs_path:
        output_dir = outputs_path / bblock.subdirs
    else:
        output_dir = bblock.tests_dir.resolve() / OUTPUT_SUBDIR
    shutil.rmtree(output_dir, ignore_errors=True)
    output_dir.mkdir(parents=True, exist_ok=True)

    all_results: list[ValidationReportItem] = []

    output_base_filenames = set()

    validators = [
        JsonValidator(bblock, bblocks_register),
        RdfValidator(bblock, bblocks_register),
        *plugin_validators,
    ]

    # Test resources
    if bblock.tests_dir.is_dir():
        for fn in sorted(bblock.tests_dir.resolve().iterdir()):
            if not fn.is_file():
                continue
            output_fn = output_dir / fn.name
            output_base_filenames.add(fn.stem)

            test_result = _validate_resource(
                bblock=bblock,
                filename=fn,
                output_filename=output_fn,
                validators=validators,
                bblocks_register=bblocks_register,
                validation_resources=bblock.validation_resources,
            )
            if test_result:
                all_results.append(test_result)
                final_result = not test_result.failed and final_result
                test_count += 1

    for extra_test_resource in bblock.get_extra_test_resources():
        fn = bblock.files_path / 'tests' / extra_test_resource['output-filename']
        output_fn = output_dir / fn.name
        output_base_filenames.add(fn.stem)

        declared_media_type = extra_test_resource.get('media-type')
        file_format = declared_media_type or (mimetypes.from_extension(fn.suffix[1:]) if fn.suffix else None)

        test_result = _validate_resource(
            bblock=bblock,
            filename=fn,
            output_filename=output_fn,
            validators=validators,
            resource_contents=extra_test_resource['contents'],
            require_fail=extra_test_resource.get('require-fail', False),
            resource_url=extra_test_resource['ref'] if isinstance(extra_test_resource['ref'], str) else None,
            file_format=file_format,
            bblocks_register=bblocks_register,
            validation_resources=bblock.validation_resources,
        )
        if test_result:
            all_results.append(test_result)
            final_result = not test_result.failed and final_result
            test_count += 1

    # Examples
    if bblock.examples:
        for example_id, example in enumerate(bblock.examples):
            example_base_uri = example.get('base-uri')
            snippets = example.get('snippets', ())
            snippet_langs = set(snippet.get('language') for snippet in snippets)
            snippet_langs.update({FORMAT_ALIASES[l] for l in snippet_langs if l in FORMAT_ALIASES})
            add_snippets = {}
            for snippet_id, snippet in enumerate(snippets):
                code, lang = snippet.get('code'), snippet.get('language')
                add_snippets_formats = snippet.get('doc-uplift-formats', DEFAULT_UPLIFT_FORMATS)

                if isinstance(add_snippets_formats, str):
                    add_snippets_formats = [add_snippets_formats]
                elif not add_snippets_formats:
                    add_snippets_formats = []

                extension = FORMAT_ALIASES.get(snippet['language'], snippet['language']).replace('/', '.')
                fn = bblock.files_path / (f"example_{example_id + 1}_{snippet_id + 1}"
                                          f".{extension}")

                output_fn = (output_dir.joinpath(sanitize_filename(example.get('base-output-filename', fn.name)))
                             .with_suffix(f'.{extension}'))
                i = 0
                while output_fn.stem in output_base_filenames:
                    i += 1
                    output_fn = output_fn.with_stem(f"{output_fn.stem}-{i}")

                with open(output_fn, 'wb' if isinstance(code, bytes) else 'w') as f:
                    f.write(code)

                snippet['path'] = output_fn
                snippet_language = snippet.get('language')
                if snippet_language:
                    snippet_language = mimetypes.normalize(snippet_language)

                example_result = _validate_resource(
                    bblock=bblock,
                    filename=fn,
                    output_filename=output_fn,
                    validators=validators,
                    resource_contents=code,
                    example_index=(example_id + 1, snippet_id + 1),
                    base_uri=snippet.get('base-uri', example_base_uri),
                    schema_ref=snippet.get('schema-ref'),
                    resource_url=snippet.get('ref'),
                    require_fail=False,
                    prefixes=example.get('prefixes'),
                    file_format=snippet_language,
                    additional_shacl_closures=snippet.get('shacl-closure'),
                    bblocks_register=bblocks_register,
                    validation_resources=bblock.validation_resources,
                )
                if example_result:
                    all_results.append(example_result)
                    final_result = final_result and not example_result.failed
                    for file_format, file_data in example_result.uplifted_files.items():
                        if file_format not in snippet_langs and file_format in add_snippets_formats:
                            add_snippets[file_format] = file_data
                    test_count += 1

            if add_snippets:
                snippets = example.setdefault('snippets', [])
                for lang, file_data in add_snippets.items():
                    fn, code = file_data
                    snippets.append({
                        'language': lang,
                        'code': code,
                        'path': fn,
                    })

    json_report = report_to_dict(bblock=bblock, items=all_results, base_url=base_url)
    with open(output_dir / '_report.json', 'w') as f:
        json.dump(json_report, f, indent=2)

    return final_result, test_count, json_report
