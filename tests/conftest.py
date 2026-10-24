"""Shared test setup: settings with Claude switched off, an in-memory database, a frozen
simulated clock, the demo dataset, and helpers to build sensor features."""
from datetime import datetime, timezone

import pytest

from backend.clock import Clock
from backend.db import Database
from backend.households import load_files
from backend.schemas import SensorFeatures
from backend.settings import load_settings

T0 = datetime(2026, 10, 24, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def settings():
    s = load_settings(env={})                      # ignore the developer's .env and real keys
    s.hazard = dict(s.hazard, use_claude=False)
    s.anthropic_api_key = None
    s.apify_token = None
    return s


@pytest.fixture
def db():
    return Database(":memory:")


@pytest.fixture
def clock():
    c = Clock(simulated=True)
    c.freeze(T0)
    return c


@pytest.fixture
def ds(settings):
    return load_files(settings.data_paths, settings.site["center"])


def features(node, wet=0.3, tilt=0.0, rate=0.0, quality="ok", age=1.0, now=T0, simulated=False):
    """A SensorFeatures record with just the fields the hazard engine reads."""
    return SensorFeatures(node, "liugui-demo", now, now, now, 30, (f"{node}:1",), 1, 30, "test", age,
                          300 + 400 * wet if wet is not None else None, wet, 0.0, tilt, tilt, rate,
                          quality, () if quality == "ok" else ("test_reason",), simulated)


def calm_features(now=T0):
    return {n: features(n, now=now) for n in ("N01", "N02", "N03")}
