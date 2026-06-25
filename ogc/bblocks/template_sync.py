from __future__ import annotations

import logging
import os
from pathlib import Path

from ogc.bblocks.permissions import ask_yes_no

logger = logging.getLogger(__name__)

_TEMPLATE_DIR_ENV = 'BBP_TEMPLATE_DIR'
_TRACKED_FILES = ('build.sh', 'view.sh')
_MAX_COMMITS_SCANNED = 20


def _is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o111)


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | 0o755)


def check_template_files(git_repo_path: Path) -> None:
    """Offer to update scaffolding files (build.sh, view.sh, ...) that are
    outdated copies of their bblock-template counterparts.

    A file is only treated as a candidate for updating if it has never been
    modified since it was added to the repo's git history - if it has, we
    assume it was intentionally customized and leave it alone.
    """
    template_dir = os.environ.get(_TEMPLATE_DIR_ENV)
    if not template_dir:
        return
    template_dir = Path(template_dir)
    if not template_dir.is_dir():
        return

    try:
        import git
        repo = git.Repo(git_repo_path)
    except Exception as e:
        logger.debug("Could not open git repo at %s to check template files: %s", git_repo_path, e)
        return

    for filename in _TRACKED_FILES:
        target = git_repo_path / filename
        template = template_dir / filename
        if not target.is_file() or not template.is_file():
            continue

        commits = list(repo.iter_commits(paths=filename, max_count=_MAX_COMMITS_SCANNED))
        if not commits:
            continue

        # Commits that only touch the file's mode (e.g. `chmod a+x build.sh`)
        # leave its blob hash unchanged, so they don't count as customization
        content_hashes = set()
        for commit in commits:
            try:
                content_hashes.add(commit.tree[filename].hexsha)
            except KeyError:
                pass
        if len(content_hashes) != 1:
            # Content actually changed across commits (or couldn't be read),
            # so we can't be sure it's still the pristine template version
            logger.debug(
                "Skipping template check for %s: its content changed across %d commit(s) "
                "(expected its content to be unchanged since it was added)", filename, len(commits),
            )
            continue

        if target.read_bytes() != template.read_bytes():
            print()
            print("╔══ Outdated template file detected")
            print(f"║ {filename} differs from the latest version in bblocks-template,")
            print(f"║ and does not appear to have been customized.")
            print("║")
            if ask_yes_no(
                f"Update {filename} to the latest bblocks-template version?",
                no_input_message=(
                    f"No interactive input available to ask about updating {filename} "
                    f"(stdin is closed) - leaving it as-is. It may be outdated; compare "
                    f"against https://github.com/opengeospatial/bblocks-template/blob/master/{filename}"
                ),
            ):
                target.write_bytes(template.read_bytes())
                _make_executable(target)
                print(f"  Updated {filename}.")
                # The executable bit was implicitly accepted along with the content update
                continue

        if not _is_executable(target):
            print()
            print("╔══ Template file is not executable")
            print(f"║ {filename} is missing the executable bit.")
            print("║")
            if ask_yes_no(
                f"Make {filename} executable?",
                no_input_message=(
                    f"No interactive input available to ask about making {filename} executable "
                    f"(stdin is closed) - leaving it as-is."
                ),
            ):
                _make_executable(target)
                print(f"  Made {filename} executable.")
