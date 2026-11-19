"""Fixture validator plugin used by tests/test_validate.py (no dependencies)."""


class FixtureValidator:
    mime_types = ['text/plain']
    file_extensions = ['.txt']

    def validate(self, meta):
        return [
            {'message': 'legacy ok', 'is_error': False},
            {'message': 'legacy bad', 'is_error': True},
            {'message': 'just a warning', 'severity': 'warning',
             'payload': {'subsection': 'Checks', 'op': 'require-fail', 'line': 3}},
            {'message': 'info', 'severity': 'INFO'},
        ]
