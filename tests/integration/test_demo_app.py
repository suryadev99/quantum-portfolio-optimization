"""Smoke test: the Streamlit demo renders all four pages with no exception and a clean audit."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[2] / "apps" / "demo" / "app.py"
PAGES = [
    "1. Business problem",
    "2. Decomposition",
    "3. Quantum sampling",
    "4. Portfolio menu and audit",
]


@pytest.mark.slow
def test_all_pages_render_and_the_audit_is_clean():
    at = AppTest.from_file(str(APP), default_timeout=240).run()
    assert not at.exception
    for page in PAGES:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception, f"{page}: {[e.value for e in at.exception]}"
        assert not at.error, f"{page}: {[e.value for e in at.error]}"
    labels = {m.label: m.value for m in at.metric}
    assert labels["Audit violations"] == "0"


@pytest.mark.slow
def test_banner_states_no_advantage_and_not_advice():
    at = AppTest.from_file(str(APP), default_timeout=240).run()
    text = " ".join(i.value for i in at.info)
    assert "not quantum advantage" in text.lower()
    assert "not investment advice" in text.lower()
