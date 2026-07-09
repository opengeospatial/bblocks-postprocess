from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ogc.bblocks.permissions import ask_yes_no

logger = logging.getLogger(__name__)

_TEMPLATE_DIR_ENV = 'BBP_TEMPLATE_DIR'
_TRACKED_FILES = ('build.sh', 'view.sh')
_MAX_COMMITS_SCANNED = 20

_POSTPROCESS_IMAGE_MARKER = 'bblocks-postprocess'
_DOCKER_RUN_RE = re.compile(r'docker\s+run\b')


def _split_logical_lines(text: str) -> list[tuple[str, int, int]]:
    """Group physical lines into shell logical lines (joining `\\`-continued ones).

    Returns a list of (joined_text, first_line_index, last_line_index).
    """
    lines = text.splitlines(keepends=True)
    logical = []
    i = 0
    while i < len(lines):
        start = i
        buf = lines[i]
        while buf.rstrip('\n').rstrip().endswith('\\') and i + 1 < len(lines):
            i += 1
            buf += lines[i]
        logical.append((buf, start, i))
        i += 1
    return logical


def _has_interactive_flags(command: str) -> bool:
    tokens = command.replace('\\\n', ' ').split()
    if any(t in ('-it', '-ti') for t in tokens):
        return True
    has_i = any(t in ('-i', '--interactive') for t in tokens)
    has_t = any(t in ('-t', '--tty') for t in tokens)
    return has_i and has_t


def ensure_build_script_interactive(git_repo_path: Path) -> bool:
    """Make sure build.sh's `docker run` for the postprocess image passes `-it`.

    Without `-it`, stdin/a tty aren't available inside the container, so none
    of the interactive permission prompts (see permissions.py) can ever be
    answered - they silently deny by default. If the flag is missing, add it
    and report that the caller should stop and ask the user to re-run.

    Returns True if build.sh was modified (the caller should stop the run).
    """
    build_script = git_repo_path / 'build.sh'
    if not build_script.is_file():
        return False

    text = build_script.read_text()
    for logical_command, start, end in _split_logical_lines(text):
        if not _DOCKER_RUN_RE.search(logical_command) or _POSTPROCESS_IMAGE_MARKER not in logical_command:
            continue
        if _has_interactive_flags(logical_command):
            return False

        lines = text.splitlines(keepends=True)
        match = _DOCKER_RUN_RE.search(lines[start])
        if not match:
            # `docker run` and the image are on different physical lines; bail
            # out rather than guessing where to insert the flag.
            logger.warning(
                "build.sh invokes %s without -it, but the fix couldn't be applied automatically "
                "(docker run and the image name are on different lines). Please add -it to the "
                "docker run command by hand.", _POSTPROCESS_IMAGE_MARKER,
            )
            return False

        insertion_point = match.end()
        lines[start] = lines[start][:insertion_point] + ' -it' + lines[start][insertion_point:]
        build_script.write_text(''.join(lines))
        print()
        print("╔══ build.sh updated")
        print("║ build.sh was missing -it on the docker run command, so interactive")
        print("║ permission prompts could never be answered. Added it.")
        print("║ Please re-run build.sh.")
        return True

    return False


def _is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o111)


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | 0o755)


def check_template_files(git_repo_path: Path, mode: str = 'ask') -> None:
    """Update scaffolding files (build.sh, view.sh, ...) that are outdated
    copies of their bblocks-template counterparts.

    A file is only treated as a candidate for updating if it has never been
    modified since it was added to the repo's git history - if it has, we
    assume it was intentionally customized and leave it alone.

    mode:
        'ask'    - prompt before updating (default); if stdin isn't
                   interactive, warn and leave the file as-is
        'always' - update without prompting
        'never'  - skip the check entirely
    """
    if mode == 'never':
        return

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
            if mode == 'always':
                target.write_bytes(template.read_bytes())
                _make_executable(target)
                logger.info("Updated %s to the latest bblocks-template version.", filename)
                continue

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
            if mode == 'always':
                _make_executable(target)
                logger.info("Made %s executable.", filename)
                continue

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
