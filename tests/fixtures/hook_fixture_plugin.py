"""Fixture build-plugin class for tests/hooks/test_plugin_harness.py.

Exercises the real _plugin_harness.py subprocess wire protocol end to end:
before_run (no-op), after_register (mutates and returns a new register),
before_bblock (raises for one specific bblock id, to exercise the harness's
error-reporting path, and prints to stdout otherwise, to exercise its log
capture).
"""


class FixtureBuildPlugin:

    def before_run(self, register, context):
        return None

    def after_register(self, register, context):
        result = dict(register)
        result['seenShapes'] = sorted(register.get('shapes', []))
        return result

    def before_bblock(self, stage, bblock, register, context):
        if bblock.get('identifier') == 'boom':
            raise ValueError(f"deliberate failure for {bblock['identifier']}")
        print(f"before_bblock {stage} {bblock.get('identifier')}")
        return None
