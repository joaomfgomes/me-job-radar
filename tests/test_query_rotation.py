"""Offline tests for secondary-search rotation (query expansion, step 3).

Core searches must run every cycle; secondary searches round-robin in
small batches so LinkedIn traffic doesn't scale 1:1 with search count.
"""

from jobradar.config import Settings
from jobradar.models import JobPosting, SearchConfig
from jobradar.pipeline import Pipeline
from jobradar.store import Store
from tests.conftest import FakeNotifier


class FakeSource:
    """Records which search names were actually queried, per call."""

    def __init__(self):
        self.calls: list[str] = []

    def fetch(self, search: SearchConfig) -> list[JobPosting]:
        self.calls.append(search.name)
        return []


def make_settings(secondary_searches=None, secondary_batch_size=2) -> Settings:
    return Settings(
        searches=[SearchConfig(name="Core A", keywords="AI Engineer", location="Remote")],
        telegram_bot_token="x",
        telegram_chat_id="y",
        secondary_searches=secondary_searches or [],
        secondary_batch_size=secondary_batch_size,
    )


def secondary_pool(n: int) -> list[SearchConfig]:
    return [SearchConfig(name=f"Secondary {i}", keywords="LLM Engineer", location="Remote") for i in range(n)]


def test_no_secondary_searches_runs_core_only(tmp_path):
    store = Store(tmp_path / "test.db")
    settings = make_settings(secondary_searches=[])
    source = FakeSource()
    pipeline = Pipeline(settings, store, {"linkedin": source}, FakeNotifier())

    pipeline.run_cycle()

    assert source.calls == ["Core A"]
    store.close()


def test_secondary_batch_rotates_through_pool_with_wraparound(tmp_path):
    store = Store(tmp_path / "test.db")
    settings = make_settings(secondary_searches=secondary_pool(5), secondary_batch_size=2)
    source = FakeSource()
    pipeline = Pipeline(settings, store, {"linkedin": source}, FakeNotifier())

    for _ in range(4):
        pipeline.run_cycle()

    # Core runs every cycle; secondary advances 2 at a time through a pool of 5,
    # wrapping back to the start once exhausted.
    secondary_calls = [c for c in source.calls if c != "Core A"]
    assert secondary_calls == [
        "Secondary 0", "Secondary 1",
        "Secondary 2", "Secondary 3",
        "Secondary 4", "Secondary 0",  # wraps
        "Secondary 1", "Secondary 2",
    ]
    assert source.calls.count("Core A") == 4  # core ran every single cycle


def test_batch_size_larger_than_pool_queries_whole_pool_once(tmp_path):
    store = Store(tmp_path / "test.db")
    settings = make_settings(secondary_searches=secondary_pool(3), secondary_batch_size=10)
    source = FakeSource()
    pipeline = Pipeline(settings, store, {"linkedin": source}, FakeNotifier())

    pipeline.run_cycle()

    secondary_calls = [c for c in source.calls if c != "Core A"]
    # Not 10 calls, not duplicated -- capped at the pool size.
    assert secondary_calls == ["Secondary 0", "Secondary 1", "Secondary 2"]


def test_rotation_state_persists_across_cycles_not_reset_each_time(tmp_path):
    store = Store(tmp_path / "test.db")
    settings = make_settings(secondary_searches=secondary_pool(4), secondary_batch_size=1)
    source = FakeSource()
    pipeline = Pipeline(settings, store, {"linkedin": source}, FakeNotifier())

    for _ in range(4):
        pipeline.run_cycle()

    secondary_calls = [c for c in source.calls if c != "Core A"]
    assert secondary_calls == ["Secondary 0", "Secondary 1", "Secondary 2", "Secondary 3"]
