import subprocess

from commandertui import bookmarks, netlocations


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_build_uri_and_label():
    assert netlocations.build_uri("smb", " 10.0.0.5 ") == "smb://10.0.0.5/"
    assert netlocations.build_uri("smb", "nas", "Media") == "smb://nas/Media/"
    assert netlocations.label_for("smb://nas/Media/") == "Media @ nas (smb)"
    assert netlocations.label_for("sftp://nas/") == "nas (sftp)"
    assert netlocations.is_uri("ftp://x/") and not netlocations.is_uri("/home/x")


def test_list_shares_parses_output(monkeypatch):
    monkeypatch.setattr(netlocations, "_gio", lambda args, interactive=False: _cp(stdout="video-in\nmnt\n"))
    assert netlocations.list_shares("nas") == (["video-in", "mnt"], "")


def test_list_shares_failure_returns_error(monkeypatch):
    monkeypatch.setattr(netlocations, "_gio", lambda args, interactive=False: _cp(2, stderr="denied"))
    assert netlocations.list_shares("nas") == (None, "denied")


def test_mount_already_mounted_counts_as_ok(monkeypatch):
    monkeypatch.setattr(
        netlocations, "_gio", lambda args, interactive=False: _cp(2, stderr="Location is already mounted")
    )
    assert netlocations.mount("smb://nas/") == (True, "")


def test_local_path_reads_gio_info(monkeypatch):
    out = "display name: x\nlocal path: /run/user/1000/gvfs/smb-share:server=nas,share=m\n"
    monkeypatch.setattr(netlocations, "_gio", lambda args, interactive=False: _cp(stdout=out))
    assert netlocations.local_path("smb://nas/m/") == "/run/user/1000/gvfs/smb-share:server=nas,share=m"


def test_saved_locations_roundtrip_and_show_in_places(tmp_path, monkeypatch):
    monkeypatch.setattr(netlocations, "NETWORK_FILE", str(tmp_path / "network.json"))
    monkeypatch.setattr(netlocations, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(bookmarks, "GVFS_ROOT", str(tmp_path / "none"))
    netlocations.save_location("smb://nas/Media/")
    netlocations.save_location("smb://nas/Media/")  # no duplicates
    assert [loc.uri for loc in netlocations.load_locations()] == ["smb://nas/Media/"]
    places = [p for p in bookmarks.all_places() if p.kind == "net-saved"]
    assert places[0].path == "smb://nas/Media/"
    netlocations.remove_location("smb://nas/Media/")
    assert netlocations.load_locations() == []
