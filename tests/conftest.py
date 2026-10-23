import pytest


@pytest.fixture(autouse=True)
def no_real_update_check(monkeypatch, tmp_path):
    """Tests must never reach PyPI or write the user's real update cache."""
    monkeypatch.setenv("COMMANDERTUI_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("COMMANDERTUI_FAKE_LATEST", raising=False)
    from commandertui import updater

    monkeypatch.setattr(updater, "CACHE_FILE", str(tmp_path / "update.json"))
