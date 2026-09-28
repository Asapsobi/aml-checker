from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run every test with an empty amlcheck home and working directory and no API keys,
    so a developer's real ~/.amlcheck or .env can never leak into a result."""
    home = tmp_path / "home"
    monkeypatch.setenv("AMLCHECK_HOME", str(home))
    monkeypatch.delenv("AMLCHECK_CONFIG", raising=False)
    for key in ("EAGLE_VIRTUAL_API_KEY", "TRONGRID_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.chdir(tmp_path)
    return home
