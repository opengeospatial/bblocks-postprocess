from __future__ import annotations

import logging
import subprocess
import sys
from contextlib import contextmanager
from contextvars import ContextVar

_indent: ContextVar[int] = ContextVar('indent', default=0)

_LEVEL_COLORS = {
    logging.WARNING: '\033[33m',   # yellow
    logging.ERROR:   '\033[31m',   # red
    logging.CRITICAL:'\033[31m',   # red
}
_RESET = '\033[0m'


@contextmanager
def log_indent():
    token = _indent.set(_indent.get() + 1)
    try:
        yield
    finally:
        _indent.reset(token)


def _append_exc_info(formatter: logging.Formatter, record: logging.LogRecord, msg: str) -> str:
    if record.exc_info:
        if not record.exc_text:
            record.exc_text = formatter.formatException(record.exc_info)
    if record.exc_text:
        msg = msg + '\n' + record.exc_text
    if record.stack_info:
        msg = msg + '\n' + formatter.formatStack(record.stack_info)
    return msg


class BBlocksFormatter(logging.Formatter):
    def __init__(self, time_fmt: str, colorize: bool = False):
        super().__init__()
        self._time_fmt = time_fmt
        self._colorize = colorize

    def format(self, record):
        indent = '  ' * _indent.get()
        msg = record.getMessage()
        if record.levelno != logging.INFO:
            level_str = f'[{record.levelname}]'
            if self._colorize and record.levelno in _LEVEL_COLORS:
                level_str = f'{_LEVEL_COLORS[record.levelno]}{level_str}{_RESET}'
            msg = f'{level_str} {msg}'
        timestamp = self.formatTime(record, self._time_fmt)
        prefix = f'[{timestamp}] {indent}'
        padding = ' ' * len(prefix)
        lines = msg.splitlines()
        msg = prefix + (('\n' + padding).join(lines) if lines else '')
        return _append_exc_info(self, record, msg)


def run_logged(args: list, label: str, log_level: int = logging.INFO, **kwargs) -> None:
    """Run a subprocess and log each output line through the logging framework."""
    _logger = logging.getLogger('ogc.bblocks')
    with subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, **kwargs) as proc:
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _logger.log(log_level, "[%s] %s", label, line)
    if proc.returncode != 0:
        _logger.error("[%s] Process exited with code %d", label, proc.returncode)
        raise subprocess.CalledProcessError(proc.returncode, args)


def setup_logging(level: str = 'INFO', log_file: str | None = None):
    log_level = getattr(logging, level.upper(), logging.INFO)
    logger = logging.getLogger('ogc.bblocks')
    logger.setLevel(log_level)
    logger.propagate = False

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(BBlocksFormatter('%H:%M:%S', colorize=sys.stderr.isatty()))
    logger.addHandler(stderr_handler)

    if log_file:
        file_handler = logging.FileHandler(log_file, mode='w', encoding='utf-8')
        file_handler.setFormatter(BBlocksFormatter('%y-%m-%d %H:%M:%S'))
        logger.addHandler(file_handler)
