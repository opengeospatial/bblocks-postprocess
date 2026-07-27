#!/usr/bin/env python3
"""Build a manifest of every historical git-blob hash for the bblocks-template
scaffolding files we track (see ogc/bblocks/tracked_template_files.txt).

This lets bblocks-postprocess recognize an unmodified - if outdated - copy of
one of these files by content alone, without depending on the consumer repo's
own (possibly squashed or rewritten) git history.
"""
import json
import subprocess
import sys
from pathlib import Path


def _historical_hashes(template_dir: str, filename: str) -> list[str]:
    revs = subprocess.run(
        ['git', 'log', '--format=%H', '--', filename],
        cwd=template_dir, capture_output=True, text=True, check=True,
    ).stdout.split()
    hashes = set()
    for rev in revs:
        result = subprocess.run(
            ['git', 'rev-parse', f'{rev}:{filename}'],
            cwd=template_dir, capture_output=True, text=True,
        )
        if result.returncode == 0:
            hashes.add(result.stdout.strip())
    return sorted(hashes)


def main(template_dir: str, output_file: str, tracked_files_file: str, lineage_files_file: str | None = None) -> None:
    tracked_files = Path(tracked_files_file).read_text().split()
    manifest: dict[str, list[str] | dict[str, list[str]]] = {}
    for filename in tracked_files:
        manifest[filename] = _historical_hashes(template_dir, filename)

    if lineage_files_file:
        lineage_files = json.loads(Path(lineage_files_file).read_text())
        for target_filename, sources in lineage_files.items():
            manifest[target_filename] = {
                lineage: _historical_hashes(template_dir, source)
                for lineage, source in sources.items()
            }

    Path(output_file).write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] if len(sys.argv) > 4 else None)