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

## Unit Tests

Unit tests live in `tests/` (pytest), mirroring the `ogc/bblocks/` layout. They cover
individual modules' logic in isolation and are independent of the live-register
regression tests in `test-postprocess.yml`/`test.yml` (which exercise the packaged
action end-to-end against real bblocks repos).

```bash
pip install -r requirements.txt -r requirements-dev.txt
python -m pytest
```

CI runs these via `.github/workflows/pytest.yml` on push/PR. See
`docs/pytest-testing-plan.md` for the prioritized checklist of what still needs
coverage.

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

- **`generate_docs.py`** — Mako-based documentation generation from templates in `templates/*/`. `templates/json-full/index.json` renders each block's full per-block JSON dump; it must stay a superset of that block's `register.json` entry (all fields `register.json` has for that block, plus `json-full`'s own extra detail like inlined `annotatedSchema`/example contents) — when a field is added to what gets published into `register.json`'s `bblocks` array (in `postprocess.py`), update `json-full`'s template to publish the same (already-resolved) value too, rather than letting it independently reimplement or drift from that value.

- **`oas30.py`** — Converts JSON Schema to OpenAPI 3.0.

- **`http_interceptor.py`** — URL mapping for local testing.

- **`template_sync.py`** — keeps a consumer repo's scaffolding files (listed in `ogc/bblocks/tracked_template_files.txt`: `build.sh`, `build-devel.sh`, `view.sh`, `create-clean-pr.sh`, `.github/workflows/pr-check.yml`) in sync with their canonical versions in **bblocks-template**. Runs from `entrypoint.py` after repo autodetection, gated by `--update-template-files` (default `true`) and always skipped when `--skip-permissions` is set — so it only ever runs on local/interactive invocations, never in CI. The Dockerfile bakes a shallow clone of bblocks-template into the image at `BBP_TEMPLATE_DIR` (`/opt/bblocks-template`) and generates `.known-template-hashes.json` (via `scripts/generate_template_hash_manifest.py`), recording every git-blob hash each tracked file has ever had across bblocks-template's history, before discarding the `.git` dir. `check_template_files()` then, per tracked file: creates it (making parent dirs as needed) if missing on disk *unless* the consumer repo's own git history shows the path was previously committed and deliberately removed (best-effort — degrades to "create" on a shallow CI-style clone); updates it in place only if its current content hash matches some known past template version (i.e. it's still an unmodified, if outdated, stock copy) — a file that's been customized is never touched. The executable bit is only set/preserved for tracked files that are executable in bblocks-template itself (the shell scripts, not the workflow YAML). Separately, `ensure_build_script_interactive()` patches `build.sh` to add `-it` to its `docker run` for the postprocess image if missing, since without a tty the interactive permission prompts (`permissions.py`) can never be answered.

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

When changing `bblocks-config.yaml` (new keys, examples, documentation comments), also update the equivalent section in the **bblocks-template** repository's `bblocks-config.yaml`, since downstream repos scaffold from it.

More generally, changes here often need companion changes in sibling repos — check each one, not just bblocks-template:
- **bblocks-template** — scaffold `bblocks-config.yaml` (and other scaffolded files) used by new registers; keep in sync with any config format changes.
- **bblocks-viewer** — consumes `register.json` and any other postprocessor output; update it for changes to output structure/fields, new config it needs to read (e.g. new `register.json` keys), or new plugin/extension mechanisms it needs to support.
- **bblocks-docs** — documents authoring/config/CLI behavior for register maintainers; update it for any user-facing behavior change here (new CLI flags, new `bblocks-config.yaml` keys, new bblock.json fields, changed defaults, etc.), not just config-file changes.
- **ogc-llm-skills** — LLM-facing skills for OGC Building Blocks; update the relevant one(s) for user-facing changes here:
  - `bblocks/consuming` (skill name `bblocks-consuming`) — how agents consume a published register (register.json fields, schemas, JSON-LD, SHACL, examples, transforms). Update for changes visible from the consumer's side (e.g. new/changed `register.json` fields, new canonical values agents should dispatch on).
  - `bblocks/authoring` (skill name `bblocks-authoring`) — how agents author bblocks (bblock.json, schema.yaml, examples.yaml, transforms.yaml, etc.). Update for changes to authoring-time behavior (new/changed config keys, accepted field values/formats, validation rules, CLI flags).

This cross-repo propagation is for changes that have actually shipped (merged to `master`, released) — not for work still sitting on `develop` or a feature branch. Don't touch sibling repos for user-facing surface that isn't live yet; revisit the propagation once the change lands on `master`.

## Dependencies

- **Python**: ogc-na-tools (semantic annotation + RDF), pyshacl, rdflib (custom fork `avillar/rdflib@6.x`), jsonschema, mako, requests
- **Node.js**: `jsonld` package (for JSON-LD processing)
- Install: `pip install -r requirements.txt && npm install`

## CI/CD

- `build-docker.yml` — builds and pushes Docker image to `ghcr.io/opengeospatial/bblocks-postprocess`, triggered on push to `develop` or on `v1.*.*` tags (tag pushes also float the `master` tag)
- `test-postprocess.yml` — regression tests against live bblocks repos (triggered after Docker build)
- `validate-and-process.yml` — reusable workflow called by downstream repos to postprocess, commit, and deploy their building blocks
- `pr-check.yml` — reusable workflow called by downstream repos (via a `pull_request`-triggered caller scaffolded from bblocks-template, tracked in `ogc/bblocks/tracked_template_files.txt`) to validate a PR before merge: runs postprocess with `fail_on_error: 'true'` so broken bblock dependencies etc. are caught early, but deliberately never commits, deploys, or pushes to a triplestore (`enable_sparql: 'false'`) — read-only by design, unlike `validate-and-process.yml`, which it does not reuse. Since a downstream repo's `items_dir`/etc. overrides live only in that repo's own caller workflow (there's no cross-workflow-file inheritance in GitHub Actions), a mismatched override would make the check silently validate nothing — guarded against with an explicit "does `items_dir` contain any `bblock.json`" sanity check that fails loudly instead.
- `test.yml` — exercises this action's own composite/Docker actions (`full/action.yml`, `postprocess/action.yml`)
- `upload-to-triplestore.yml` — pushes semantic uplift output to a SPARQL triplestore

### Releasing

`full@v1` / `postprocess@v1` (as used by `validate-and-process.yml`, `pr-check.yml`, and downstream repos) resolve against a `v1` git tag, which `build-docker.yml` only force-moves — atomically alongside the `:latest`/`:master`/`:v1` Docker image tags — when a `v1.*.*` tag is pushed. So changes to `full/action.yml`, `postprocess/action.yml`, or the Docker image (`entrypoint.py` etc.) sit inert for existing `@v1`-pinned consumers until a new release tag ships; a push to `master`/`develop` alone doesn't reach them. (`validate-and-process.yml` itself is the exception — downstream `process-bblocks.yml` callers pin it via `@master`, so changes there go live immediately on merge.)

Cut a release with `scripts/tag-release.sh [major|minor|patch] [--push]` (defaults to `patch`), which tags the next `v1.<minor>.<patch>` off the highest existing tag and optionally pushes it.

**`image_tag` input (testing pre-release Docker images via CI):** `postprocess/action.yml`, `full/action.yml`, and `validate-and-process.yml` all expose an `image_tag` input (default `''`) that selects which `ghcr.io/opengeospatial/bblocks-postprocess` tag to run — e.g. `develop`, built on every push to that branch by `build-docker.yml`. `full/action.yml` and `validate-and-process.yml` just thread the input straight through unchanged; the actual default resolution happens once, in `postprocess/action.yml`, which — when the input is left empty — resolves it dynamically from `github.action_ref` (the ref *that action itself* was invoked at): `develop` if it's `develop`, else `latest`. This means a caller only needs to point its `uses:`/`@ref` at `develop` to get the develop image too, with no separate `image_tag: develop` to remember, and no per-branch literal to flip at merge time for this piece. This threading is safe to merge anywhere. But **on `develop` only**, `full/action.yml`'s and `validate-and-process.yml`'s nested `uses:` refs are hardcoded to `@develop` instead of `@v1` (marked with `DEVELOP-ONLY` comments), because nested `uses:` refs can't be parameterized by an input — pinning just the outer call to `@develop` wouldn't be enough on its own, and this part *does* still need a manual flip. This is a deliberate, permanent divergence from what those lines read on `master`: a straight `develop`→`master` merge will carry `@develop` into `master` verbatim unless someone flips the two pins back to `@v1` by hand at merge time — there's no way to make this automatic. (The dynamic `image_tag` resolution in `postprocess/action.yml` reads whichever ref this chain of hardcoded pins ultimately lands on, so it naturally follows those pins without needing its own flip.)

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