import pytest

from ogc.bblocks.validation import graph_cache


@pytest.fixture(autouse=True)
def clear_graph_cache():
    graph_cache.clear()
    yield
    graph_cache.clear()
