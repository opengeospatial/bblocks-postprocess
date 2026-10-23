"""Tests for ogc.bblocks.template_sync's build.sh `-it` check (ensure_build_script_interactive):
a literal -it, -i/-t pairs, and a conditionally-set tty variable are all accepted, and a
docker run with none of them gets -it added.
"""
from ogc.bblocks.template_sync import _has_interactive_flags, ensure_build_script_interactive

_IMAGE = 'ghcr.io/opengeospatial/bblocks-postprocess:${BBP_IMAGE_TAG:-latest}'

_CONDITIONAL_TTY = f"""#!/bin/bash
TTY=$([ -t 0 ] && [ -t 1 ] && echo -it)
docker run ${{TTY}} --pull=always --rm --workdir /workspace -v "$(pwd):/workspace" \\
  {_IMAGE} \\
  --clean true --base-url http://localhost:9090/register/ "$@"
"""


def test_literal_it_is_interactive():
    assert _has_interactive_flags(f'docker run -it --rm {_IMAGE}')


def test_separate_i_and_t_are_interactive():
    assert _has_interactive_flags(f'docker run -i --tty --rm {_IMAGE}')


def test_missing_flags_are_not_interactive():
    assert not _has_interactive_flags(f'docker run --rm {_IMAGE}')


def test_variable_set_to_it_is_interactive():
    for ref in ('${TTY}', '$TTY', '"${TTY}"'):
        assert _has_interactive_flags(f'docker run {ref} --rm {_IMAGE}', _CONDITIONAL_TTY), ref


def test_variable_not_set_to_it_is_not_interactive():
    script = 'OPTS="--rm"\ndocker run ${OPTS} ' + _IMAGE + '\n'
    assert not _has_interactive_flags('docker run ${OPTS} ' + _IMAGE, script)


def test_conditional_tty_build_script_is_left_alone(tmp_path):
    build_script = tmp_path / 'build.sh'
    build_script.write_text(_CONDITIONAL_TTY)
    assert ensure_build_script_interactive(tmp_path) is False
    assert build_script.read_text() == _CONDITIONAL_TTY


def test_build_script_without_it_gets_it_added(tmp_path):
    build_script = tmp_path / 'build.sh'
    build_script.write_text(f'#!/bin/bash\ndocker run --rm {_IMAGE} "$@"\n')
    assert ensure_build_script_interactive(tmp_path) is True
    assert build_script.read_text() == f'#!/bin/bash\ndocker run -it --rm {_IMAGE} "$@"\n'
