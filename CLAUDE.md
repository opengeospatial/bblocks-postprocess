# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Is

A GitHub Action and standalone Python tool that postprocesses OGC Building Blocks — reusable data models combining JSON Schema, JSON-LD, SHACL, test cases, examples, and profile declarations. It generates documentation, validates outputs, performs semantic uplifting to RDF, and optionally deploys results.

## Running the Postprocessor

```bash
# Directly via Python module
python -m ogc.bblocks.bootstrap [options]

# Key options:
#   --register-file PATH    Path to register.json output
#   --items-dir DIR         Directory to scan for building blocks
#   --base-url URL          Base URL for generated output
#   --clean                 Delete old build directories first
#   --steps STEPS           Comma-separated list: annotate,jsonld,tests,transforms,doc,register
#   --filter FILTER         Process only matching building block or file
#   --fail-on-errors        Exit non-zero if validation errors found

# Via Docker
docker build -t bblocks-postprocess .
docker run -v /path/to/repo:/workspace bblocks-postprocess [options]
```

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
bootstrap.py           Loads plugins from transform-plugins.yml, then delegates
  → entrypoint.py      Parses CLI args, loads bblocks-config.yaml, calls postprocess()
    → postprocess.py   Core orchestration: discover → annotate → validate → generate docs → register
```

### Core Components

- **`models.py`** — `BuildingBlock` (single block), `BuildingBlockRegister` (collection), `ImportedBuildingBlocks` (external registers). Building blocks lazy-load their properties; remote resources are cached under `annotated_path/_cache/`.

- **`schema.py` + `extension.py`** — JSON Schema annotation (via ogc-na-tools) and reference resolution. `extension.py` merges extension points from child building blocks into parent schemas.

- **`validate.py`** — Test validation and HTML/JSON/text report generation. Validators (JSON Schema, RDF/SHACL, semantic uplift) live in `validation/`.

- **`transform.py` + `transformers/`** — Applies pluggable transformers to examples. Built-in transformers: RDF (SHACL-AF, SPARQL), jq, XSLT, JSON-LD Frame, semantic uplift. External transformers load via `transform-plugins.yml`.

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

### Plugin System (WIP on `transform-plugins` branch)

`transform-plugins.yml` allows loading external transformer modules:
```yaml
plugins:
  - modules: [my.custom.Transformer]
    install:
      pip: my-custom-package
```

`bootstrap.py` loads these before delegating to `entrypoint.py`.

## Key Configuration Files

| File | Purpose |
|------|---------|
| `bblocks-config.yaml` | Per-repo config: identifier prefix, imports, SPARQL endpoints |
| `bblock.json` | Per-block metadata: identifier, name, schema path, examples, SHACL, extension points |
| `examples.yaml` | Example snippets with test cases |
| `transforms.yaml` | Transform definitions (type, inputs, outputs, code) |
| `transform-plugins.yml` | External transformer plugin loading |

## Dependencies

- **Python**: ogc-na-tools (semantic annotation + RDF), pyshacl, rdflib (custom fork `avillar/rdflib@6.x`), jsonschema, mako, requests
- **Node.js**: `jsonld` package (for JSON-LD processing)
- Install: `pip install -r requirements.txt && npm install`

## CI/CD

- `build-docker.yml` — builds and pushes Docker image to `ghcr.io/opengeospatial/bblocks-postprocess` on push to master
- `test-postprocess.yml` — regression tests against live bblocks repos (triggered after Docker build)
- `validate-and-process.yml` — reusable workflow called by downstream repos to postprocess, commit, and deploy their building blocks