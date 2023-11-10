<%!
from html import escape as e
from datetime import datetime, timezone
from urllib.parse import urlparse, urljoin
from os.path import basename
from pathlib import Path
from ogc.na.util import is_url
import re
import os.path

uid = 0
last_uid = None
def get_uid():
    global uid, last_uid
    uid += 1
    last_uid = f"uid-{globals()['uid']}"
    return last_uid
get_filename = lambda s: basename(urlparse(s).path)
%>
<!doctype html>
<html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Building Blocks validation report</title>
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.1/font/bootstrap-icons.css">
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/css/bootstrap.min.css" rel="stylesheet" integrity="sha384-T3c6CoIi6uLrA9TneNEoa7RxnatzjcDSCmG1MXxSR1GAsXEV/Dwwykc2MPK8M2HN" crossorigin="anonymous">
        <style>
            .entry-message {
                white-space: pre-wrap;
                font-size: 80%;
                line-height: 1.1;
            }
            *[data-bs-toggle] .caret {
                display: inline-block;
                transform: rotate(90deg);
                transition: transform 0.25s;
            }
            *[data-bs-toggle].collapsed .caret {
                transform: rotate(0deg);
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h1 class="title">Building blocks validation report</h1>
            <small class="datetime">Generated at ${e(datetime.now(timezone.utc).astimezone().isoformat())}</small>
            % if counts['total'] > 0:
                <p class="summary fw-semibold ${'text-success' if counts['passed'] == counts['total'] else 'text-danger'}">
                    Number of passing building blocks: ${counts['passed']} / ${counts['total']} (${f"{counts['passed'] * 100 / counts['total']:.2f}".rstrip('0').rstrip('.')}%)
                </p>
            % endif
            <div class="text-end small" id="expand-collapse">
                <a href="#" class="expand-all">Expand all</a>
                <a href="#" class="collapse-all">Collapse all</a>
            </div>
            % if reports:
                <div class="accordion mt-2" id="bblock-reports">
                    % for i, report in enumerate(reports):
                        <div class="accordion-item bblock-report" data-bblock-id="${e(report['bblockId'])}" id="bblock-${i}">
                            <h2 class="accordion-header bblock-title">
                                <button class="accordion-button ${'collapsed' if report['result'] else ''}" type="button" data-bs-toggle="collapse" data-bs-target="#bblock-collapse-${i}">
                                    <div class="flex-fill">
                                        ${e(report['bblockName'])}
                                        <small class="ms-2 bblock-id">${e(report['bblockId'])}</small>
                                    </div>

                                    <span class="badge text-bg-${'success' if report['result'] else 'danger'} me-2">
                                        % if report['result']:
                                            Passed
                                        % else:
                                            Tests Failed
                                        % endif
                                        % if report['counts']['total'] > 0:
                                            (${f"{report['counts']['passed'] * 100 / report['counts']['total']:.2f}".rstrip('0').rstrip('.')}% passed)
                                        % else:
                                            (100%)
                                        % endif
                                    </span>
                                </button>
                            </h2>
                            <div class="accordion-collapse collapse ${'show' if not report['result'] else ''}" id="bblock-collapse-${i}">
                                <div class="accordion-body">
                                    % if report['counts']['total'] > 0:
                                        <p class="summary fw-semibold ${'text-success' if report['counts']['passed'] == report['counts']['total'] else 'text-danger'}">
                                            Test passed: ${report['counts']['passed']} / ${report['counts']['total']}
                                        </p>
                                    % endif
                                    % if report.get('globalErrors'):
                                        <div class="card mb-2 global-validation-item validation-item">
                                            <div class="card-body">
                                                <div class="card-title">
                                                    Building block global validation errors
                                                    <div class="float-end">
                                                        <span class="badge text-bg-danger me-2">Failed</span>
                                                    </div>
                                                </div>
                                                <div class="card-text text-danger">
                                                    % for subsection_title, section in report['globalErrors'].items():
                                                        <div class="font-monospace entry-message section-${e(subsection_title.lower())}">${e(section['message'])}</div>
                                                    % endfor
                                                </div>
                                            </div>
                                        </div>
                                    % endif
                                    % for item in report['items']:
                                        <div class="card mb-2 validation-item ${'require-fail' if item['source']['requireFail'] else ''}">
                                            <div class="card-body">
                                                <div class="card-title mb-0">
                                                    <button type="button" class="btn btn-sm btn-primary collapsed"
                                                            data-bs-toggle="collapse" data-bs-target="#${get_uid()}"
                                                            aria-expanded="false" aria-controls="${last_uid}"
                                                            style="--bs-btn-padding-y: .25rem; --bs-btn-padding-x: .5rem; --bs-btn-font-size: .75rem;">
                                                        <i class="bi bi-caret-right-fill caret"></i>
                                                        Details
                                                    </button>
                                                    <a href="${e(item['source']['filename'])}" target="_blank">${e(re.sub(r'.*/', '', item['source']['filename']))}</a>
                                                    <span class="badge bg-secondary ${e(item['source']['type'].lower())}">${e(item['source']['type'].replace('_', ' ').capitalize())}</span>
                                                    % if item['source']['requireFail']:
                                                        <span class="badge text-bg-info">Requires fail</span>
                                                    % endif
                                                    <div class="float-end">
                                                        % if item['result']:
                                                            <span class="badge text-bg-success me-2">Passed</span>
                                                        % else:
                                                            <span class="badge text-bg-danger me-2">Failed</span>
                                                        % endif
                                                    </div>
                                                </div>
                                                <div class="card-text mt-2 validation-text collapse" id="${last_uid}">
                                                    <div class="validation-text-inner">
                                                        % if report.get('globalErrors'):
                                                            <div class="font-monospace entry-message text-danger">Note: Test failed because there are global validation errors.</div>
                                                        % endif
                                                        % for section in item['sections']:
                                                            % if section.get('entries'):
                                                                    <div class="font-monospace subsection-title mt-2">${e(section['title'])}</div>
                                                                % for entry in section.get('entries'):
                                                                    <div class="font-monospace entry-message section-${e(section['name'].lower())} ${'text-danger' if entry['isError'] else ''}">${e(entry['message'])}</div>
                                                                % endfor
                                                            % endif
                                                        % endfor
                                                    </div>
                                                </div>
                                            </div>
                                        </div>
                                    % endfor
                                    % if not report.get('items') and not report.get('globalErrors'):
                                        <div class="alert alert-info mb-0">No tests were found for this building block.</div>
                                    % endif
                                </div>
                            </div>
                        </div>
                    % endfor
                </div>
            % else:
                <div class="alert alert-primary">No building blocks tests were found.</div>
            % endif
        </div>
        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.2/dist/js/bootstrap.bundle.min.js" integrity="sha384-C6RzsynM9kWDrMNeT87bh95OGNyZPhcTNXj1NW7RuBCsyN/o0jlpcV8Qyq46cDfL" crossorigin="anonymous"></script>
        <script type="text/javascript">
            window.addEventListener('load', () => {
                const accordionEntries = [...document.querySelectorAll('#bblock-reports .accordion-collapse')];
                document.querySelector('#expand-collapse').addEventListener('click', ev => {
                    ev.preventDefault();
                    if (ev.target.matches('.expand-all')) {
                        accordionEntries.forEach(e => e.classList.add('show'));
                    } else if (ev.target.matches('.collapse-all')) {
                        accordionEntries.forEach(e => e.classList.remove('show'));
                    }
                });
            });
        </script>
    </body>
</html>