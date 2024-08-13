from __future__ import annotations

import dataclasses
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from ogc.bblocks.models import BuildingBlock, BuildingBlockRegister


class Validator(ABC):

    def __init__(self, bblock: BuildingBlock, register: BuildingBlockRegister):
        self.bblock = bblock
        self.register = register

    @abstractmethod
    def validate(self, filename: Path, output_filename: Path,
                 report: ValidationReportItem,
                 contents: str | None = None,
                 schema_ref: str | None = None,
                 base_uri: str | None = None,
                 resource_url: str | None = None,
                 require_fail: bool | None = None,
                 prefixes: dict[str, str] | None = None,
                 **kwargs) -> bool | None:
        raise NotImplementedError


class ValidationItemSourceType(Enum):
    TEST_RESOURCE = 'Test resource'
    EXAMPLE = 'Example'


class ValidationReportSection(Enum):
    GENERAL = 'General'
    FILES = 'Files'
    JSON_SCHEMA = 'JSON Schema'
    JSON_LD = 'JSON-LD'
    TURTLE = 'Turtle'
    SHACL = 'SHACL'
    SEMANTIC_UPLIFT = 'Semantic Uplift'
    UNKNOWN = 'Unknown errors'


@dataclasses.dataclass
class ValidationItemSource:
    type: ValidationItemSourceType
    filename: Path | None = None
    example_index: int | None = None
    snippet_index: int | None = None
    language: str | None = None
    require_fail: bool = False
    source_url: str | None = None


@dataclasses.dataclass
class ValidationReportEntry:
    section: ValidationReportSection
    message: str
    is_error: bool = False
    payload: dict | None = None
    is_global: bool = False


class ValidationReportItem:

    def __init__(self, source: ValidationItemSource):
        self._has_errors = False
        self.source = source
        self._sections: dict[ValidationReportSection, list[ValidationReportEntry]] = {s: []
                                                                                      for s in ValidationReportSection}
        self._uplifted_files: dict[str, tuple[Path, str]] = {}
        self._has_general_errors = False
        self._used_files: list[tuple[Path | str, bool]] = []

    def add_entry(self, entry: ValidationReportEntry):
        self._sections.setdefault(entry.section, []).append(entry)
        if entry.is_error and (not entry.payload or entry.payload.get('op') != 'require-fail'):
            self._has_errors = True
            if entry.is_global:
                self._has_general_errors = True

    def add_uplifted_file(self, file_format: str, path: Path, contents: str):
        self._uplifted_files[file_format] = (path, contents)

    def write_text(self, bblock: BuildingBlock, report_fn: Path):
        with open(report_fn, 'w') as f:
            f.write(f"Validation report for {bblock.identifier} - {bblock.name}\n")
            f.write(f"Generated {datetime.now(timezone.utc).astimezone().isoformat()}\n")
            for section in ValidationReportSection:
                entries = self._sections.get(section)
                if not entries:
                    continue
                f.write(f"=== {section.value} ===\n")
                for entry in entries:
                    if entry.is_error:
                        f.write("\n** Validation error **\n")
                    f.write(f"{entry.message}\n")
                f.write(f"=== End {section.value} ===\n\n")

    @property
    def failed(self) -> bool:
        return self._has_general_errors or self.source.require_fail != self._has_errors

    @property
    def general_errors(self) -> bool:
        return self._has_general_errors

    @property
    def sections(self) -> dict[ValidationReportSection, list[ValidationReportEntry]]:
        return self._sections

    @property
    def uplifted_files(self) -> dict[str, tuple[Path, str]]:
        return self._uplifted_files
