<% import re %>
# ${bblock.name} (${bblock.itemClass.capitalize()})

`${bblock.identifier}` *v${bblock.version}*

${bblock.abstract}

% if bblock.maturity:
[*Maturity*](https://github.com/cportele/ogcapi-building-blocks#building-block-maturity): ${bblock.maturity.capitalize()}
% endif

% if bblock.description:
${'##'} Description

${bblock.description.replace('@@assets@@', assets_rel or '')}
% endif
% if bblock.examples:
${'##'} Examples
  % for example in bblock.examples:

${'###'} ${example.get('title', f"Example {loop.index + 1}")}
    % if example.get('content'):
${example['content'].replace('@@assets@@', assets_rel or '')}
    %endif
    % for snippet in example.get('snippets', []):
${'####'} ${snippet['language']}
```${snippet['language']}
${snippet['code']}
```

    % endfor
  % endfor
% endif
% if bblock.metadata.get('schema'):
${'##'} Schema

```yaml
${bblock.annotated_schema_contents}
```

Links to the schema:

* YAML version: [schema.yaml](${bblock.metadata['schema']['application/json']})
* JSON version: [schema.json](${bblock.metadata['schema']['application/yaml']})

% endif
% if bblock.ldContext:

${'#'} JSON-LD Context

```jsonld
${bblock.jsonld_context_contents}
```

You can find the full JSON-LD context here:
[${re.sub(r'.*/', '', bblock.ldContext)}](${bblock.ldContext})

% endif
% if bblock.sources:
${'##'} Sources

  % for source in bblock.sources:
    % if source.get('link'):
* [${source['title']}](${source['link']})
    % else:
* ${source['title']}
    % endif
  % endfor
% endif
* % if git_repo:

${'#'} For developers

The source code for this Building Block can be found in the following repository:

* URL: [${git_repo}](${git_repo})
* Path: `${git_path}`

% endif