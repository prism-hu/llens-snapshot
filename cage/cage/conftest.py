"""Shared test configuration.

The server's lifespan compiles doc into build artifacts at startup (build.py). So that
tests do not overwrite the production output dir (build/doc = the location compose binds)
with test deployment values, the output dir is redirected to a session-scoped temp
directory and the deployment values are pinned to test values.
Within a session the manifest matches, so the build runs only once.
"""

from __future__ import annotations

import pytest

from cage import server

TEST_HIS_FAQ_BASE = "https://his.example/his-info"


@pytest.fixture(scope="session", autouse=True)
def _doc_build_sandbox(tmp_path_factory):
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(server, "DOC_OUT_ROOT", tmp_path_factory.mktemp("build") / "doc")
        mp.setattr(server, "HIS_FAQ_BASE", TEST_HIS_FAQ_BASE)
        yield
