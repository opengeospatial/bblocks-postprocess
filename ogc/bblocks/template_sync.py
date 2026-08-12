from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

_TEMPLATE_DIR_ENV = 'BBP_TEMPLATE_DIR'
_HASH_MANIFEST_FILENAME = '.known-template-hashes.json'
_TRACKED_FILES = tuple((Path(__file__).parent / 'tracked_template_files.txt').read_text().split())
_LINEAGE_FILES: dict[str, dict[str, str]] = json.loads(
    (Path(__file__).parent / 'lineage_template_files.json').read_text()
)
_OGC_ORGS = {'opengeospatial', 'ogcincubator'}

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


def ensure_build_script_interactive(repo_path: Path) -> bool:
    """Make sure build.sh's `docker run` for the postprocess image passes `-it`.

    Without `-it`, stdin/a tty aren't available inside the container, so none
    of the interactive permission prompts (see permissions.py) can ever be
    answered - they silently deny by default. If the flag is missing, add it
    and report that the caller should stop and ask the user to re-run.

    Returns True if build.sh was modified (the caller should stop the run).
    """
    build_script = repo_path / 'build.sh'
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


def _was_deliberately_removed(repo_path: Path, filename: str) -> bool:
    """Best-effort check for whether `filename` was committed and later
    removed from `repo_path`'s own git history (as opposed to never having
    existed there).

    Note this is a narrower question than the one the hash-manifest approach
    replaced consumer-repo git history for (see check_template_files):
    that heuristic judged whether *existing* content was still pristine, and
    broke because every auto-update added a new "changed" commit to the same
    history it was reading from. Here we only ever ask "does any commit touch
    this path at all", while the file is absent - a question our own writes
    can't retroactively pollute the answer to, since a create only happens
    when there's currently nothing on disk to have written over.

    Still best-effort: on a shallow clone (the default for `actions/checkout`
    in CI) history older than the fetch depth simply isn't there, so a
    genuine past deletion can look identical to "never existed". When that
    happens - or there's no git repo at all - this returns False, i.e. we
    fall back to treating the file as never having existed and create it,
    rather than risk skipping a file that should be added.
    """
    try:
        import git
        repo = git.Repo(repo_path)
        return next(repo.iter_commits(paths=filename, max_count=1), None) is not None
    except Exception as e:
        logger.debug("Could not check git history for %s: %s", filename, e)
        return False


def _is_executable(path: Path) -> bool:
    return bool(path.stat().st_mode & 0o111)


def _make_executable(path: Path) -> None:
    path.chmod(path.stat().st_mode | 0o755)


def _content_hash(data: bytes) -> str:
    """git's own blob hash, so it lines up with the manifest generated at image
    build time (scripts/generate_template_hash_manifest.py) without needing an
    actual git repository to compute it."""
    header = f"blob {len(data)}\0".encode()
    return hashlib.sha1(header + data).hexdigest()


def _load_known_hashes(template_dir: Path) -> dict[str, set[str]]:
    manifest_file = template_dir / _HASH_MANIFEST_FILENAME
    if not manifest_file.is_file():
        return {}
    try:
        raw = json.loads(manifest_file.read_text())
        return {filename: set(hashes) for filename, hashes in raw.items() if isinstance(hashes, list)}
    except Exception as e:
        logger.debug("Could not read template hash manifest at %s: %s", manifest_file, e)
        return {}


def _load_lineage_hashes(template_dir: Path) -> dict[str, dict[str, set[str]]]:
    manifest_file = template_dir / _HASH_MANIFEST_FILENAME
    if not manifest_file.is_file():
        return {}
    try:
        raw = json.loads(manifest_file.read_text())
        return {
            filename: {lineage: set(hashes) for lineage, hashes in lineages.items()}
            for filename, lineages in raw.items() if isinstance(lineages, dict)
        }
    except Exception as e:
        logger.debug("Could not read template hash manifest at %s: %s", manifest_file, e)
        return {}


def check_template_files(repo_path: Path, enabled: bool = True) -> dict[str, bool]:
    """Create or update scaffolding files (build.sh, view.sh, ...) that are
    missing or outdated copies of their bblocks-template counterparts.

    A missing file is created from the current template version, unless this
    repo's own git history shows it was committed and later deliberately
    removed (see _was_deliberately_removed - best-effort only, since a
    shallow CI clone can't always tell).

    An existing file is only treated as a candidate for updating if its
    content hash matches some version the file has genuinely had at some
    point in bblocks-template's own history (see _HASH_MANIFEST_FILENAME) -
    if it doesn't, we assume it was intentionally customized and leave it
    alone. Unlike checking the consumer repo's own git history, this doesn't
    care how the current content got there (a human commit, an earlier
    auto-update, or never having been committed at all), so it isn't confused
    by our own past updates.

    Since a match against the manifest means the file is provably an
    unmodified (if outdated) stock copy, updating it - and fixing its
    executable bit - is applied directly, without prompting: there's nothing
    to ask permission for, unlike genuinely risky operations (arbitrary
    transform/plugin code) that permissions.py gates.

    Returns a dict of filename -> whether it now matches the latest template
    version (whether it already did, or was just updated).
    """
    up_to_date: dict[str, bool] = {}

    if not enabled:
        return up_to_date

    template_dir = os.environ.get(_TEMPLATE_DIR_ENV)
    if not template_dir:
        return up_to_date
    template_dir = Path(template_dir)
    if not template_dir.is_dir():
        return up_to_date

    known_hashes = _load_known_hashes(template_dir)

    for filename in _TRACKED_FILES:
        target = repo_path / filename
        template = template_dir / filename
        if not template.is_file():
            continue

        template_bytes = template.read_bytes()
        template_hash = _content_hash(template_bytes)
        # Mirror the template's own executable bit (e.g. the .sh scripts are
        # executable, a .github/workflows/*.yml tracked file isn't) rather
        # than assuming every tracked file should be made executable.
        should_be_executable = _is_executable(template)

        if not target.is_file():
            if _was_deliberately_removed(repo_path, filename):
                logger.debug(
                    "Skipping template check for %s: it was previously committed and removed "
                    "from this repo's history, so it's assumed to be an intentional removal", filename,
                )
                up_to_date[filename] = False
                continue

            # Nothing to preserve - either the repo predates this file being
            # tracked, or it was deleted and we couldn't tell (e.g. a shallow
            # CI clone). Either way, treat it the same as an outdated stock
            # copy and (re)create it.
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(template_bytes)
            if should_be_executable:
                _make_executable(target)
            logger.info("Added %s from the latest bblocks-template version.", filename)
            up_to_date[filename] = True
            continue

        target_bytes = target.read_bytes()
        target_hash = _content_hash(target_bytes)

        if target_hash == template_hash:
            up_to_date[filename] = True
        elif target_hash not in known_hashes.get(filename, ()):
            # Content was never a genuine template version, so it's assumed
            # to be intentionally customized - leave it alone
            logger.debug(
                "Skipping template check for %s: its content doesn't match any known "
                "bblocks-template version (assumed to be customized)", filename,
            )
            up_to_date[filename] = False
        else:
            target.write_bytes(template_bytes)
            if should_be_executable:
                _make_executable(target)
            logger.info("Updated %s to the latest bblocks-template version.", filename)
            up_to_date[filename] = True

        if up_to_date[filename] and should_be_executable and not _is_executable(target):
            _make_executable(target)
            logger.info("Made %s executable.", filename)

    return up_to_date


def _lineage_for_owner(owner: str | None) -> str:
    return 'ogc' if owner in _OGC_ORGS else 'thirdparty'


def sync_lineage_files(repo_path: Path, owner: str | None, enabled: bool = True) -> dict[str, str | None]:
    """Create or update files that have more than one valid "lineage"
    depending on the kind of repo they live in (currently just SECURITY.md:
    an OGC-owned-repo version vs. a third-party one - see
    lineage_template_files.json), unlike check_template_files's tracked
    files, which have a single canonical version.

    A missing target file is created from the lineage matching `owner`
    (OGC_ORGS) - this is the only place `owner` is consulted. Once a file
    exists, which lineage it belongs to is decided purely by matching its
    content hash against each lineage's known history (see
    _load_lineage_hashes), exactly like check_template_files - so a
    deliberately cross-wired repo (e.g. an OGC-controlled repo hosted under
    a third-party org) is never "corrected" back based on owner, only ever
    left alone (if customized) or updated within whatever lineage it
    already matches.

    Returns a dict of target filename -> lineage it now matches, or None if
    it was left alone as customized.
    """
    result: dict[str, str | None] = {}

    if not enabled or not _LINEAGE_FILES:
        return result

    template_dir = os.environ.get(_TEMPLATE_DIR_ENV)
    if not template_dir:
        return result
    template_dir = Path(template_dir)
    if not template_dir.is_dir():
        return result

    lineage_hashes = _load_lineage_hashes(template_dir)

    for filename, sources in _LINEAGE_FILES.items():
        target = repo_path / filename
        known = lineage_hashes.get(filename, {})

        if not target.is_file():
            lineage = _lineage_for_owner(owner)
            source = template_dir / sources[lineage]
            if not source.is_file():
                continue
            target.write_bytes(source.read_bytes())
            logger.info("Created %s from the bblocks-template %r lineage.", filename, lineage)
            result[filename] = lineage
            continue

        target_hash = _content_hash(target.read_bytes())

        matched_lineage = next(
            (
                lineage for lineage, source in sources.items()
                if (template_dir / source).is_file()
                and (target_hash == _content_hash((template_dir / source).read_bytes())
                     or target_hash in known.get(lineage, ()))
            ),
            None,
        )

        if matched_lineage is None:
            logger.debug(
                "Skipping lineage check for %s: its content doesn't match any known "
                "bblocks-template version of any lineage (assumed to be customized)", filename,
            )
            result[filename] = None
            continue

        source = template_dir / sources[matched_lineage]
        source_bytes = source.read_bytes()
        if target_hash != _content_hash(source_bytes):
            target.write_bytes(source_bytes)
            logger.info("Updated %s to the latest bblocks-template %r version.", filename, matched_lineage)
        result[filename] = matched_lineage

    return result
