import json

import pytest

from commandertui import updater


def test_version_ordering_is_numeric_not_textual():
    assert updater.is_newer("1.10.0", "1.9.0")
    assert updater.is_newer("v1.2.0", "1.1.9")
    assert not updater.is_newer("1.1.0", "1.1.0")
    assert not updater.is_newer("1.0.9", "1.1.0")
    assert updater.is_newer("1.2.0", "1.2.0rc1")  # a pre-release is older than the final
    assert not updater.is_newer("1.2.0rc1", "1.2.0")


def test_check_asks_pypi_on_every_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "fetch_latest", lambda: calls.append(1) or "2.0.0")
    monkeypatch.setattr(updater, "detect_install", lambda: "pipx")

    info = updater.check(current="1.1.0", now=1000.0)
    assert info == updater.UpdateInfo("1.1.0", "2.0.0", "pipx")
    assert updater.check(current="1.1.0", now=1000.0 + 60) is not None
    assert calls == [1, 1]  # no daily cache: each launch asks again


def test_check_is_silent_when_offline(monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest", lambda: None)
    assert updater.check(current="1.1.0") is None
    assert updater._load_cache() == {}  # nothing cached, so the next launch retries


def test_up_to_date_returns_nothing(monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest", lambda: "1.1.0")
    assert updater.check(current="1.1.0") is None


def test_skipped_version_stays_quiet_but_a_newer_one_does_not(monkeypatch):
    monkeypatch.setattr(updater, "detect_install", lambda: "pip")
    monkeypatch.setattr(updater, "fetch_latest", lambda: "2.0.0")
    updater.check(current="1.1.0")
    updater.skip_version("2.0.0")
    assert updater.check(current="1.1.0") is None
    monkeypatch.setattr(updater, "fetch_latest", lambda: "2.1.0")
    assert updater.check(current="1.1.0").latest == "2.1.0"


def test_fetch_latest_swallows_network_errors(monkeypatch):
    def boom(*a, **k):
        raise OSError("no route")

    monkeypatch.setattr(updater.urllib.request, "urlopen", boom)
    assert updater.fetch_latest() is None


def test_fake_latest_env_shows_an_update_without_a_release(monkeypatch):
    monkeypatch.setenv(updater.ENV_FAKE_LATEST, "9.9.9")
    monkeypatch.setattr(updater, "fetch_latest", lambda: pytest.fail("must not hit the network"))
    monkeypatch.setattr(updater, "detect_install", lambda: "pipx")
    assert updater.check(current="1.1.0").latest == "9.9.9"


def test_update_commands_per_install_kind():
    assert updater.update_command("pipx", "2.0.0") == [
        "pipx",
        "upgrade",
        "--pip-args=--no-cache-dir",
        "commandertui",
    ]
    pip = updater.update_command("pip", "2.0.0")
    assert pip[1:] == ["-m", "pip", "install", "--upgrade", "--no-cache-dir", "commandertui"]
    assert updater.update_command("source", "2.0.0") is None
    arch = updater.update_command("pacman", "2.0.0")
    assert arch[:2] == ["bash", "-c"]
    script = arch[2]
    assert "v2.0.0/packaging/arch/PKGBUILD" in script and "pkgver=2.0.0" in script
    assert "updpkgsums" in script and "makepkg -si --nocheck" in script  # tests ran in the release workflow
    assert "sudo" not in script  # the password is only ever asked by makepkg, in the terminal


def test_detect_install_source_checkout():
    # This repo is a git checkout when the tests run from it.
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(updater.__file__)))
    expected = "source" if os.path.isdir(os.path.join(root, ".git")) else None
    if expected:
        assert updater.detect_install() == "source"


def test_disable_switches(monkeypatch):
    monkeypatch.delenv(updater.ENV_DISABLE, raising=False)
    assert not updater.updates_disabled()
    assert updater.updates_disabled(cli_flag=True)
    monkeypatch.setenv(updater.ENV_DISABLE, "1")
    assert updater.updates_disabled()


def test_cache_write_failure_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(updater, "CONFIG_DIR", str(tmp_path / "nope" / "\0bad"))
    updater._save_cache({"a": 1})  # must not raise


def test_cache_file_is_plain_json(monkeypatch):
    monkeypatch.setattr(updater, "fetch_latest", lambda: "3.0.0")
    monkeypatch.setattr(updater, "detect_install", lambda: "pip")
    updater.check(current="1.1.0", now=5.0)
    with open(updater.CACHE_FILE) as f:
        assert json.load(f) == {"checked": 5.0, "latest": "3.0.0"}


def test_check_status_tells_ok_new_skipped_and_offline(monkeypatch):
    monkeypatch.setattr(updater, "detect_install", lambda: "pip")
    monkeypatch.setattr(updater, "fetch_latest", lambda: "1.1.0")
    assert updater.check_status(current="1.1.0") == (None, "ok")
    monkeypatch.setattr(updater, "fetch_latest", lambda: "2.0.0")
    info, status = updater.check_status(current="1.1.0")
    assert status == "new" and info.latest == "2.0.0"
    updater.skip_version("2.0.0")
    assert updater.check_status(current="1.1.0") == (None, "skipped")
    monkeypatch.setattr(updater, "fetch_latest", lambda: None)
    assert updater.check_status(current="1.1.0") == (None, "offline")


def _info(latest="2.0.0", install="pipx"):
    return updater.UpdateInfo(current="1.0.0", latest=latest, install=install)


def test_run_update_requires_the_new_version_to_be_installed(monkeypatch):
    import subprocess

    monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
    monkeypatch.setattr(updater, "installed_version", lambda: "2.0.0")
    assert updater.run_update(_info()) is True
    # pipx said "already up to date" (exit 0) but the old version is still there
    monkeypatch.setattr(updater, "installed_version", lambda: "1.0.0")
    assert updater.run_update(_info()) is False
    # version unknown: trust the exit code
    monkeypatch.setattr(updater, "installed_version", lambda: None)
    assert updater.run_update(_info()) is True


def test_run_update_fails_on_nonzero_exit_without_checking_version(monkeypatch):
    import subprocess

    monkeypatch.setattr(updater.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    monkeypatch.setattr(updater, "installed_version", lambda: "2.0.0")
    assert updater.run_update(_info()) is False
    assert updater.run_update(_info(install="source")) is False


def test_update_notice_explains_the_wait_and_the_sudo_prompt():
    arch = updater.update_notice(_info(install="pacman"))
    assert "1.0.0" in arch and "2.0.0" in arch  # from -> to
    assert (
        "few seconds" in arch and "sudo password" in arch and "once" not in arch
    )  # makepkg -s may ask earlier too
    assert "sudo" not in updater.update_notice(_info(install="pipx"))


def test_run_update_prints_the_notice_before_running_the_command(monkeypatch, capsys):
    import subprocess

    order = []
    monkeypatch.setattr(
        updater.subprocess,
        "run",
        lambda cmd, **kw: order.append("run") or subprocess.CompletedProcess(cmd, 0),
    )
    monkeypatch.setattr(updater, "installed_version", lambda: "2.0.0")
    assert updater.run_update(_info(install="pacman")) is True
    assert "Updating CommanderTUI" in capsys.readouterr().out
    assert order == ["run"]


async def test_update_dialog_buttons_fit_a_small_terminal(tmp_path):
    from commandertui.app import CommanderApp
    from commandertui.modals import UpdateScreen
    from textual.widgets import Button

    for install in ("pacman", "pipx", "pip", "source"):
        app = CommanderApp(str(tmp_path), str(tmp_path), check_updates=False)
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(UpdateScreen(_info(install=install)))
            await pilot.pause(0.2)
            screen = app.screen
            assert isinstance(screen, UpdateScreen)
            box = screen.query_one("#update-box").region
            for button in screen.query(Button):
                region = button.region
                assert region.width > 0 and region.right <= box.right, (install, button.id, region)
                assert region.right <= 80 and region.bottom <= 24, (install, button.id, region)
