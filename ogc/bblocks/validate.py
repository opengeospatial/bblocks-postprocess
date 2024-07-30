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

from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister
from ogc.bblocks.util import sanitize_filename
from ogc.bblocks.validation import Validator, ValidationItemSourceType, ValidationReportSection, ValidationItemSource, \
    ValidationReportEntry, ValidationReportItem
from ogc.bblocks.validation.json_ import JsonValidator
from ogc.bblocks.validation.rdf import RdfValidator

OUTPUT_SUBDIR = 'output'
FORMAT_ALIASES = {
    'turtle': 'ttl',
    'json-ld': 'jsonld',
}
DEFAULT_UPLIFT_FORMATS = ['jsonld', 'ttl']


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


def report_to_html(json_reports: list[dict],
                   base_url: str | None = None,
                   report_fn: Path | None = None) -> str | None:
    pass_count = sum(r['result'] for r in json_reports)
    counts = {
        'total': len(json_reports),
        'passed': pass_count,
        'failed': len(json_reports) - pass_count,
    }
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
                       validators: list[Validator],
                       resource_contents: str | None = None,
                       additional_shacl_closures: list[str | Path] | None = None,
                       base_uri: str | None = None,
                       schema_ref: str | None = None,
                       require_fail: bool | None = None,
                       resource_url: str | None = None,
                       example_index: tuple[int, int] | None = None) -> ValidationReportItem | None:
    if require_fail is None:
        require_fail = filename.stem.endswith('-fail') and not example_index

    if resource_url and not is_url(resource_url):
        resource_url = None

    if example_index:
        example_idx, snippet_idx = re.match(r'example_(\d+)_(\d+).*', filename.name).groups()
        source = ValidationItemSource(
            type=ValidationItemSourceType.EXAMPLE,
            filename=output_filename,
            example_index=int(example_idx),
            snippet_index=int(snippet_idx),
            language=filename.suffix[1:],
            source_url=resource_url,
        )
    else:
        source = ValidationItemSource(
            type=ValidationItemSourceType.TEST_RESOURCE,
            filename=filename,
            language=filename.suffix[1:],
            require_fail=require_fail,
            source_url=resource_url,
        )
    report = ValidationReportItem(source)

    any_validator_run = False
    try:
        for validator in validators:
            result = validator.validate(filename, output_filename, report,
                                        contents=resource_contents,
                                        base_uri=base_uri,
                                        additional_shacl_closures=additional_shacl_closures,
                                        schema_ref=schema_ref)
            any_validator_run = any_validator_run or (result is not None)

    except Exception as unknown_exc:
        report.add_entry(ValidationReportEntry(
            section=ValidationReportSection.UNKNOWN,
            message=','.join(traceback.format_exception(unknown_exc)),
            is_error=True,
            is_global=True,
            payload={
                'exception': unknown_exc.__class__.__qualname__,
            }
        ))

    if not any_validator_run:
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


def validate_test_resources(bblock: BuildingBlock,
                            bblocks_register: BuildingBlockRegister,
                            outputs_path: Path | None = None,
                            base_url: str | None = None) -> tuple[bool, int, dict]:
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
    ]

    # Test resources
    if bblock.tests_dir.is_dir():
        for fn in sorted(bblock.tests_dir.resolve().iterdir()):
            if fn.suffix not in ('.json', '.jsonld', '.ttl'):
                continue
            output_fn = output_dir / fn.name
            output_base_filenames.add(fn.stem)

            test_result = _validate_resource(
                bblock=bblock,
                filename=fn,
                output_filename=output_fn,
                validators=validators,
            )
            if test_result:
                all_results.append(test_result)
                final_result = not test_result.failed and final_result
                test_count += 1

    for extra_test_resource in bblock.get_extra_test_resources():
        if not re.search(r'\.(json(ld)?|ttl)$', extra_test_resource['output-filename']):
            continue
        fn = bblock.files_path / 'tests' / extra_test_resource['output-filename']
        output_fn = output_dir / fn.name
        output_base_filenames.add(fn.stem)

        test_result = _validate_resource(
            bblock=bblock,
            filename=fn,
            output_filename=output_fn,
            validators=validators,
            resource_contents=extra_test_resource['contents'],
            require_fail=extra_test_resource.get('require-fail', False),
            resource_url=extra_test_resource['ref'] if isinstance(extra_test_resource['ref'], str) else None,
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
            add_snippets = {}
            for snippet_id, snippet in enumerate(snippets):
                code, lang = snippet.get('code'), snippet.get('language')
                add_snippets_formats = snippet.get('doc-uplift-formats', DEFAULT_UPLIFT_FORMATS)

                if isinstance(add_snippets_formats, str):
                    add_snippets_formats = [add_snippets_formats]
                elif not add_snippets_formats:
                    add_snippets_formats = []

                fn = bblock.files_path / (f"example_{example_id + 1}_{snippet_id + 1}"
                                          f".{FORMAT_ALIASES.get(snippet['language'], snippet['language'])}")

                output_fn = output_dir / sanitize_filename(example.get('base-output-filename', fn.name))
                i = 0
                while output_fn.stem in output_base_filenames:
                    i += 1
                    output_fn = output_fn.with_stem(f"{output_fn.stem}-{i}")

                with open(output_fn, 'w') as f:
                    f.write(code)

                snippet['path'] = output_fn

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
