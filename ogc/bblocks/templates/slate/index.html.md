---
title: ${bblock.name} (${bblock.itemClass.capitalize()})
% if bblock.examples:
<%
  known_langs = {
    'json': 'JSON',
    'turtle': 'RDF/Turtle',
    'plaintext': 'Plain text',
    'txt': 'Plain text',
    'yaml': 'YAML',
    'java': 'Java',
    'python': 'Python',
    'javascript': 'Javascript',
  }
  langs = {snippet['language']: True for example in bblock.examples for snippet in example.get('snippets', [])}
%>
  % if len(langs) > 1:
language_tabs:
    % for lang in langs:
  - ${lang}${(': ' + known_langs[lang]) if lang in known_langs else ''}
    % endfor
  % endif
% endif

toc_footers:
  - Version ${bblock.version}
  - <a href='#'>${bblock.name}</a>
  - <a href='https://blocks.ogc.org/register.html'>Building Blocks register</a>

search: true

code_clipboard: true

meta:
  - name: ${bblock.name} (${bblock.itemClass.capitalize()})
---
<% import re %>

${'#'} ${bblock.name} `${bblock.identifier}`

${bblock.abstract}

[Maturity](https://github.com/cportele/ogcapi-building-blocks#building-block-maturity): ${bblock.maturity.capitalize()}

% if bblock.description:
${'#'} Description

${bblock.description.replace('@@assets@@', assets_rel or '')}
% endif
% if bblock.examples:
${'#'} Examples
  % for example in bblock.examples:

${'##'} ${example.get('title', f"Example {loop.index + 1}")}

    % if example.get('content'):
${example['content'].replace('@@assets@@', assets_rel or '')}
    %endif
    % for snippet in example.get('snippets', []):
```${snippet['language']}
${snippet['code']}
```

    % endfor
  % endfor
% endif
% if bblock.schema:

${'#'} JSON Schema

```yaml--schema
${bblock.annotated_schema_contents}
```

Links to the schema:

* YAML version: <a href="${bblock.metadata['schema']['application/yaml']}" target="_blank">bblock.metadata['schema']['application/yaml']</a>
* JSON version: <a href="${bblock.metadata['schema']['application/json']}" target="_blank">${bblock.metadata['schema']['application/json']</a>

% endif
% if bblock.ldContext:

${'#'} JSON-LD Context

```json--ldContext
${bblock.jsonld_context_contents}
```

You can find the full JSON-LD context here:
<a href="${bblock.ldContext}" target="_blank">${bblock.ldContext}</a>

% endif
% if bblock.sources:
${'#'} References

  % for source in bblock.sources:
    % if source.get('link'):
* [${source['title']}](${source['link']})
    % else:
* ${source['title']}
    % endif
  % endfor
% endif
