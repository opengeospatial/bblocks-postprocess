#!/usr/bin/env python3
"""Build a manifest of every historical git-blob hash for the bblocks-template
scaffolding files we track (build.sh, view.sh).

This lets bblocks-postprocess recognize an unmodified - if outdated - copy of
one of these files by content alone, without depending on the consumer repo's
own (possibly squashed or rewritten) git history.
"""
import json
import subprocess
import sys
from pathlib import Path

TRACKED_FILES = ('build.sh', 'view.sh')


def main(template_dir: str, output_file: str) -> None:
    manifest: dict[str, list[str]] = {}
    for filename in TRACKED_FILES:
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
        manifest[filename] = sorted(hashes)
    Path(output_file).write_text(json.dumps(manifest, indent=2) + '\n')


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2])