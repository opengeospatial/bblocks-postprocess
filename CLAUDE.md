# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A GitHub Action and standalone Python tool that postprocesses OGC Building Blocks — reusable data models combining JSON Schema, JSON-LD, SHACL, test cases, examples, and profile declarations. It generates documentation, validates outputs, performs semantic uplifting to RDF, and optionally deploys results.

## Running the Postprocessor

```bash
# Directly via Python module
python -m ogc.bblocks.entrypoint [options]

# Key options:
#   --register-file PATH     Path to register.json output
#   --items-dir DIR          Directory to scan for building blocks
#   --base-url URL           Base URL for generated output
#   --clean true|false       Delete old build directories first
#   --steps STEPS            Comma-separated list: annotate,jsonld,tests,transforms,doc,register
#   --filter FILTER          Process only matching building block or file
#   --fail-on-error true|false   Exit non-zero if validation errors found
#   --skip-permissions true|false  Skip interactive prompts for transform/validator plugins (set true in CI)

# Via Docker
docker build -t bblocks-postprocess .
docker run -v /path/to/repo:/workspace bblocks-postprocess [options]
```

All flags are string-valued (`true`/`false`, not boolean switches) — see [Adding new CLI flags](#adding-new-cli-flags) for why.

## Local Testing with URL Mappings

Create `bblocks-config-local.yml` to map remote URLs to local files:
```yaml
url-mappings:
  https://example.com/path: /local/path
```

The HTTP interceptor (`http_interceptor.py`) monkey-patches urllib/requests to redirect these.

## Architecture

### Entry & Flow

```
entrypoint.py         Parses CLI args, loads bblocks-config.yaml, calls postprocess()
  → postprocess.py    Core orchestration: discover → annotate → validate → generate docs → register
```

### Core Components

- **`models.py`** — `BuildingBlock` (single block), `BuildingBlockRegister` (collection), `ImportedBuildingBlocks` (external registers). Building blocks lazy-load their properties; remote resources are cached under `annotated_path/_cache/`.

- **`schema.py` + `extension.py`** — JSON Schema annotation (via ogc-na-tools) and reference resolution. `extension.py` merges extension points from child building blocks into parent schemas.

- **`validate.py`** — Test validation and HTML/JSON/text report generation. Validators (JSON Schema, RDF/SHACL, semantic uplift) live in `validation/`.

- **`transform.py` + `transformers/`** — Applies pluggable transformers to examples. Built-in transformers: RDF (SHACL-AF, SPARQL), jq, XSLT, JSON-LD Frame, semantic uplift. External transform/validator plugins load from the `plugins.transforms` / `plugins.validators` keys in `bblocks-config.yaml` (see below).

- **`generate_docs.py`** — Mako-based documentation generation from templates in `templates/*/`.

- **`oas30.py`** — Converts JSON Schema to OpenAPI 3.0.

- **`http_interceptor.py`** — URL mapping for local testing.

### Per-Building-Block Processing

For each `bblock.json` found:
1. Validate metadata against `schemas/bblock.schema.yaml`
2. Annotate schema with semantic annotations (ogc-na-tools)
3. Resolve all `$ref` pointers
4. Convert to OAS 3.0
5. Apply transforms to examples
6. Validate (JSON Schema, JSON-LD context, RDF, SHACL)
7. Generate docs from Mako templates
8. Write annotated schema, context, etc. to `build/`

After all blocks: generate `register.json`, perform semantic uplift to JSON-LD + Turtle, optionally push to SPARQL triplestore.

### Plugin System

External transform/validator plugins are declared under `plugins.transforms` / `plugins.validators` in `bblocks-config.yaml`:
```yaml
plugins:
  transforms:
    - modules: [my.custom.Transformer]
      pip: [my-custom-package]
```
`transform.py`/`validate.py` install each plugin's pip/npm dependencies into a per-plugin sandbox (`sandbox.py`) and register it (`transformers/plugin.py`, `validation/plugin.py`). Unless `--skip-permissions` is set, the user is prompted interactively before installing/running plugin code (`permissions.py`).

The legacy standalone `transform-plugins.yml` file is still read as a fallback for `plugins.transforms` (with a deprecation warning) if `bblocks-config.yaml` doesn't declare that key.

## Key Configuration Files

| File | Purpose |
|------|---------|
| `bblocks-config.yaml` | Per-repo config: identifier prefix, imports, SPARQL endpoints |
| `bblock.json` | Per-block metadata: identifier, name, schema path, examples, SHACL, extension points |
| `examples.yaml` | Example snippets with test cases |
| `transforms.yaml` | Transform definitions (type, inputs, outputs, code) |
| `transform-plugins.yml` | Legacy external transformer plugin loading (deprecated — use `plugins.transforms` in `bblocks-config.yaml`) |

When changing `bblocks-config.yaml` (new keys, examples, documentation comments), the equivalent section in the bblocks-template repository's `bblocks-config.yaml` should also be updated, since downstream repos scaffold from it.

## Dependencies

- **Python**: ogc-na-tools (semantic annotation + RDF), pyshacl, rdflib (custom fork `avillar/rdflib@6.x`), jsonschema, mako, requests
- **Node.js**: `jsonld` package (for JSON-LD processing)
- Install: `pip install -r requirements.txt && npm install`

## CI/CD

- `build-docker.yml` — builds and pushes Docker image to `ghcr.io/opengeospatial/bblocks-postprocess`, triggered on push to `develop` or on `v1.*.*` tags (tag pushes also float the `master` tag)
- `test-postprocess.yml` — regression tests against live bblocks repos (triggered after Docker build)
- `validate-and-process.yml` — reusable workflow called by downstream repos to postprocess, commit, and deploy their building blocks
- `test.yml` — exercises this action's own composite/Docker actions (`full/action.yml`, `postprocess/action.yml`)
- `upload-to-triplestore.yml` — pushes semantic uplift output to a SPARQL triplestore

### Adding new CLI flags

The postprocessing chain has four layers that all need to be updated:

```
validate-and-process.yml  (workflow_call input, default = CI-safe value)
  → full/action.yml        (composite action input, same default)
    → postprocess/action.yml  (Docker action input + args entry, same default)
      → entrypoint.py      (argparse argument, local-safe default)
```

- Always use a string flag that accepts an explicit value — **never `action='store_true'`**. The Docker action passes args as a list of `[--flag, value]` pairs, so store-true flags cannot receive a value from the action layer.
- Local default (in `entrypoint.py`) should reflect what's safe/appropriate for interactive local runs.
- CI default (in all three `action.yml` / workflow files) should reflect what's safe for unattended CI runs.
- If the CI default differs from the local default, all three action/workflow files must declare the input explicitly with the CI default — otherwise the layer silently falls back to the `entrypoint.py` default.