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


def test_unmounted_mount_point_is_hidden(tmp_path, monkeypatch):
    (tmp_path / "i7server").mkdir()
    monkeypatch.setattr(bookmarks, "MOUNT_ROOTS", (str(tmp_path),))
    monkeypatch.setattr(bookmarks.os.path, "ismount", lambda p: False)
    assert bookmarks.detect_mounted() == []


def test_home_fuse_mounts_detected_from_proc_mounts(tmp_path, monkeypatch):
    home = "/home/tester"
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "365: /home/tester/365 fuse.rclone rw 0 0\n"
        "gdrive: /home/tester/Google\\040Drive fuse.rclone rw 0 0\n"
        "gvfsd-fuse /run/user/1000/gvfs fuse.gvfsd-fuse rw 0 0\n"
        "/dev/sda1 /home/tester/data ext4 rw 0 0\n"
    )
    monkeypatch.setattr(bookmarks, "PROC_MOUNTS", str(mounts))
    monkeypatch.setattr(bookmarks.os.path, "expanduser", lambda p: home)

    places = bookmarks.detect_home_mounts()

    assert [(p.label, p.path, p.kind) for p in places] == [
        ("365", "/home/tester/365", "rclone"),
        ("Google Drive", "/home/tester/Google Drive", "rclone"),
    ]
