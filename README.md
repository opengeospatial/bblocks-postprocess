# bblocks-postprocess

A GitHub Action and standalone Python tool that postprocesses [OGC Building Blocks](https://github.com/opengeospatial/bblocks-template) — reusable data models combining:

* JSON Schema
* JSON-LD contexts
* SHACL shapes
* test cases and examples
* validation reports
* profile declarations
* associated OpenAPI (OAS 3.0) definitions

For a given source repository it discovers building blocks, annotates and validates them, generates documentation, performs semantic uplift to RDF, and produces a `register.json` describing the whole collection.

## Using this as a GitHub Action

Two composite/Docker actions are published from this repository:

* **`opengeospatial/bblocks-postprocess/postprocess@v1`** — runs the postprocessor itself (Docker action, wraps `python -m ogc.bblocks.entrypoint`). Use this if you just want the annotate/validate/document/register steps.
* **`opengeospatial/bblocks-postprocess/full@v1`** — thin wrapper around `postprocess` with the same inputs; intended to be composed into a larger workflow.

Most downstream repos don't call either directly — they instead invoke the reusable workflow:

* **`.github/workflows/validate-and-process.yml`** — checks out the repo, restores/saves a plugin sandbox cache, runs `full`, commits the generated build output back to the repo, deploys the viewer/docs to GitHub Pages, and (optionally) pushes the semantic uplift to a SPARQL triplestore via `upload-to-triplestore.yml`.

Example usage from a building blocks repository:

```yaml
jobs:
  postprocess:
    uses: opengeospatial/bblocks-postprocess/.github/workflows/validate-and-process.yml@v1
    secrets: inherit
```

See `postprocess/action.yml` / `full/action.yml` for the full list of inputs (register file location, items directory, base URL, viewer deployment, SPARQL credentials, etc.).

## Running locally

```bash
pip install -r requirements.txt
npm install

python -m ogc.bblocks.entrypoint \
  --items-dir _sources \
  --register-file build-local/register.json \
  --generated-docs-path build-local/generateddocs \
  --annotated-path build-local/annotated \
  --base-url https://example.org/bblocks/
```

Run `python -m ogc.bblocks.entrypoint --help` for the full list of flags (`--clean`, `--filter`, `--steps`, `--fail-on-error`, `--skip-permissions`, `--log-level`, ...).

### Via Docker

```bash
docker build -t bblocks-postprocess .
docker run -v /path/to/repo:/workspace -w /workspace bblocks-postprocess [flags]
```

### Local testing with URL mappings

Create a `bblocks-config-local.yml` next to your `bblocks-config.yaml` to redirect remote URLs to local files while developing:

```yaml
url-mappings:
  https://example.com/path: /local/path
```

`http_interceptor.py` monkey-patches `urllib`/`requests` to honor these mappings.

## Repository layout

| Path | Purpose |
|------|---------|
| `ogc/bblocks/` | Postprocessor source (entrypoint, orchestration, validation, transforms, doc generation) |
| `postprocess/action.yml` | Docker-based GitHub Action running the postprocessor |
| `full/action.yml` | Composite action wrapping `postprocess` |
| `.github/workflows/validate-and-process.yml` | Reusable workflow: checkout → postprocess → commit → deploy → (optional) triplestore push |
| `.github/workflows/build-docker.yml` | Builds/pushes the `ghcr.io/opengeospatial/bblocks-postprocess` image |
| `.github/workflows/test-postprocess.yml` | Regression tests against live bblocks repos |
| `docs/` | Design notes (transformer plugins, validation plugins) |
| `scripts/` | Maintenance scripts (template hash manifest, release tagging) |

See `CLAUDE.md` for a deeper architectural walkthrough of the postprocessing pipeline.

## Dependencies

* **Python** (3.10): `ogc-na-tools` (semantic annotation + RDF), `pyshacl`, a patched `rdflib` fork (`avillar/rdflib@6.x`), `jsonschema`, `mako`, `requests`
* **Node.js**: the `jsonld` package, used for JSON-LD processing

## License

See [LICENSE](LICENSE).
