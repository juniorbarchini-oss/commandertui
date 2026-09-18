import os

from commandertui import bookmarks


def test_gvfs_smb_share_gets_a_readable_label(tmp_path, monkeypatch):
    gvfs = tmp_path / "gvfs"
    gvfs.mkdir()
    (gvfs / "smb-share:server=192.168.1.10,share=Media").mkdir()
    monkeypatch.setattr(bookmarks, "GVFS_ROOT", str(gvfs))

    places = bookmarks.detect_network_mounts()

    assert len(places) == 1
    assert places[0].label == "Media @ 192.168.1.10"
    assert places[0].kind == "network"
    assert places[0].path == str(gvfs / "smb-share:server=192.168.1.10,share=Media")


def test_gvfs_missing_dir_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(bookmarks, "GVFS_ROOT", str(tmp_path / "no-such-gvfs"))
    assert bookmarks.detect_network_mounts() == []


def test_gvfs_entry_with_no_share_or_server_falls_back_to_raw_name(tmp_path, monkeypatch):
    gvfs = tmp_path / "gvfs"
    gvfs.mkdir()
    (gvfs / "some-scheme:whatever").mkdir()
    monkeypatch.setattr(bookmarks, "GVFS_ROOT", str(gvfs))

    places = bookmarks.detect_network_mounts()

    assert places[0].label == "some-scheme:whatever"


def test_all_places_includes_network_mounts(tmp_path, monkeypatch):
    gvfs = tmp_path / "gvfs"
    gvfs.mkdir()
    (gvfs / "smb-share:server=nas,share=Backups").mkdir()
    monkeypatch.setattr(bookmarks, "GVFS_ROOT", str(gvfs))
    monkeypatch.setattr(bookmarks, "MOUNT_ROOTS", ())
    monkeypatch.setattr(bookmarks, "load_saved", lambda: [])

    labels = {p.label for p in bookmarks.all_places()}

    assert "Backups @ nas" in labels
