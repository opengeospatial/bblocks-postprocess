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


class ConfigurableBuildPlugin:
    """Takes its config as a positional constructor argument and echoes it back,
    plus the context it saw, from after_register."""

    def __init__(self, config):
        self.config = config

    def after_register(self, register, context):
        return {'config': self.config, 'context': context}


class BuggyConfigurableBuildPlugin:
    """Accepts config but raises a TypeError from inside the constructor body."""

    def __init__(self, config):
        raise TypeError("deliberate TypeError from constructor body")


class EchoContextBuildPlugin:
    """No-arg constructor; echoes the context it saw."""

    def after_register(self, register, context):
        return {'context': context}
