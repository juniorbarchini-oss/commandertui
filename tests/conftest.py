import pytest


@pytest.fixture(autouse=True)
def no_real_update_check(monkeypatch, tmp_path, isolated_user_home):
    """Tests must never reach PyPI or write the user's real update cache."""
    monkeypatch.setenv("COMMANDERTUI_NO_UPDATE_CHECK", "1")
    monkeypatch.delenv("COMMANDERTUI_FAKE_LATEST", raising=False)
    from commandertui import updater

    monkeypatch.setattr(updater, "CACHE_FILE", str(tmp_path / "update.json"))


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch, tmp_path, isolated_user_home):
    """Tests must never read or write the user's real settings file."""
    from commandertui import settings

    monkeypatch.setattr(settings, "SETTINGS_FILE", str(tmp_path / "settings.json"))


@pytest.fixture(autouse=True)
def isolated_user_home(monkeypatch, tmp_path_factory):
    """Keep all test HOME/XDG writes and cached configuration paths in tmp."""
    home = tmp_path_factory.mktemp("isolated-home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))

    # These modules cache paths at import time, before fixtures can set HOME.
    from commandertui import bookmarks, netlocations, settings, updater

    config = home / ".config" / "commandertui"
    for module in (bookmarks, netlocations, settings, updater):
        monkeypatch.setattr(module, "CONFIG_DIR", str(config))
    monkeypatch.setattr(bookmarks, "BOOKMARKS_FILE", str(config / "bookmarks.json"))
    monkeypatch.setattr(netlocations, "NETWORK_FILE", str(config / "network.json"))
