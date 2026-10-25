import os
import subprocess

from commandertui import bookmarks, netlocations, trash


def _cp(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_build_uri_and_label():
    assert netlocations.build_uri("smb", " 10.0.0.5 ") == "smb://10.0.0.5/"
    assert netlocations.build_uri("smb", "nas", "Media") == "smb://nas/Media/"
    assert netlocations.label_for("smb://nas/Media/") == "Media @ nas (smb)"
    assert netlocations.label_for("sftp://nas/") == "nas (sftp)"
    assert netlocations.is_uri("ftp://x/") and not netlocations.is_uri("/home/x")


def test_list_shares_parses_output(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "linux")
    monkeypatch.setattr(netlocations, "_gio", lambda args, interactive=False: _cp(stdout="video-in\nmnt\n"))
    assert netlocations.list_shares("nas") == (["video-in", "mnt"], "")


def test_list_shares_failure_returns_error(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "linux")
    monkeypatch.setattr(netlocations, "_gio", lambda args, interactive=False: _cp(2, stderr="denied"))
    assert netlocations.list_shares("nas") == (None, "denied")


def test_mount_already_mounted_counts_as_ok(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "linux")
    monkeypatch.setattr(
        netlocations, "_gio", lambda args, interactive=False: _cp(2, stderr="Location is already mounted")
    )
    assert netlocations.mount("smb://nas/") == (True, "")


def test_local_path_reads_gio_info(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "linux")
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


def test_darwin_list_shares_parses_smbutil_output(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "darwin")
    smbutil_out = (
        "\nShare                                           Type    Comments\n"
        "-------------------------------\n"
        "Media                                           Disk    Movies and Music\n"
        "Backup Space                                    Disk    Time Machine\n"
        "IPC$                                            Pipe    IPC Service\n"
        "admin$                                          Disk    Hidden Admin\n"
        "\n4 shares listed\n"
    )
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return _cp(0, stdout=smbutil_out)

    monkeypatch.setattr(netlocations.subprocess, "run", fake_run)
    assert netlocations.list_shares("192.168.1.10", user="john doe") == (["Media", "Backup Space"], "")
    assert calls == [["smbutil", "view", "-N", "//john%20doe@192.168.1.10"]]


def test_darwin_list_shares_failure_returns_error(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "darwin")
    monkeypatch.setattr(
        netlocations.subprocess,
        "run",
        lambda cmd, **kw: _cp(1, stderr="smbutil: server rejected the connection: Authentication error"),
    )
    shares, err = netlocations.list_shares("nas", user="hbarchini")
    assert shares is None and "Authentication error" in err


def test_darwin_mount_and_local_path_use_osascript_with_user_and_spaces(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "darwin")
    monkeypatch.setattr(netlocations, "DARWIN_MOUNT_WAIT", 1.0)
    monkeypatch.setattr(netlocations, "DARWIN_POLL_INTERVAL", 0.01)

    called = []
    checks = {"count": 0}

    def fake_run(cmd, **kw):
        if cmd[0] == "/sbin/mount":
            checks["count"] += 1
            if checks["count"] >= 2:
                out = "//john%20doe@nas/Backup%20Space on /Volumes/Backup Space (smbfs, nodev, nosuid)\n"
                return _cp(0, stdout=out)
            return _cp(0, stdout="")
        called.append(cmd)
        return _cp(0)

    monkeypatch.setattr(netlocations.subprocess, "run", fake_run)

    uri = netlocations.build_uri("smb", "nas", "Backup Space", user="john doe")
    assert uri == "smb://john%20doe@nas/Backup%20Space/"
    assert netlocations.label_for(uri) == "Backup Space @ nas (smb)"
    assert netlocations.mount(uri) == (True, "")
    assert called == [["osascript", "-e", 'mount volume "smb://john%20doe@nas/Backup%20Space"']]
    assert netlocations.local_path(uri) == "/Volumes/Backup Space"


def test_darwin_homonymous_shares_match_host_and_unmount_right_volume(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "darwin")
    mount_table = (
        "//otheruser@server-a/HD1 on /Volumes/HD1 (smbfs, nodev, nosuid, mounted by hbarchini)\n"
        "//hbarchini@SERVER-B/HD1 on /Volumes/HD1-1 (smbfs, nodev, nosuid, mounted by hbarchini)\n"
    )
    du_calls = []

    def fake_run(cmd, **kw):
        if cmd[0] == "/sbin/mount":
            return _cp(0, stdout=mount_table)
        if cmd[0] == "diskutil":
            du_calls.append(cmd)
            return _cp(0)
        raise AssertionError(f"Unexpected command: {cmd}")

    monkeypatch.setattr(netlocations.subprocess, "run", fake_run)

    uri_a = netlocations.build_uri("smb", "server-a", "HD1", user="hbarchini")
    uri_b = netlocations.build_uri("smb", "server-b", "HD1", user="hbarchini")
    uri_c = netlocations.build_uri("smb", "server-c", "HD1", user="hbarchini")

    assert netlocations.local_path(uri_a) == "/Volumes/HD1"
    assert netlocations.local_path(uri_b) == "/Volumes/HD1-1"
    assert netlocations.local_path(uri_c) is None  # not mounted even though /Volumes/HD1 exists

    assert netlocations.unmount(uri_b) == (True, "")
    assert du_calls == [["diskutil", "unmount", "/Volumes/HD1-1"]]
    assert netlocations.unmount(uri_c) == (False, "Not a mounted network location")


def test_darwin_saved_location_persists_user_and_metadata(tmp_path, monkeypatch):
    import json

    monkeypatch.setattr(netlocations, "NETWORK_FILE", str(tmp_path / "network.json"))
    monkeypatch.setattr(netlocations, "CONFIG_DIR", str(tmp_path))
    uri = netlocations.build_uri("smb", "10.0.0.5", "My Share", user="hbarchini")
    netlocations.save_location(uri)
    raw = json.loads((tmp_path / "network.json").read_text())
    assert raw[0]["user"] == "hbarchini"
    assert raw[0]["protocol"] == "smb"
    assert raw[0]["host"] == "10.0.0.5"
    assert raw[0]["share"] == "My Share"
    loaded = netlocations.load_locations()
    assert len(loaded) == 1 and loaded[0].user == "hbarchini" and loaded[0].uri == uri


def test_darwin_rejects_sftp_and_ftp(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "darwin")
    ok, err = netlocations.mount("sftp://nas/")
    assert not ok and "not supported on macOS" in err
    assert netlocations.local_path("ftp://nas/") is None


def test_unmount_linux_and_darwin(monkeypatch):
    # Linux: gio mount -u <uri>
    monkeypatch.setattr(netlocations.sys, "platform", "linux")
    gio_calls = []
    monkeypatch.setattr(
        netlocations,
        "_gio",
        lambda args, interactive=False: (gio_calls.append(args), _cp(0))[1],
    )
    assert netlocations.unmount("smb://nas/Media/") == (True, "")
    assert gio_calls == [["mount", "-u", "smb://nas/Media/"]]

    # Linux busy error
    monkeypatch.setattr(
        netlocations,
        "_gio",
        lambda args, interactive=False: _cp(2, stderr="Target is busy"),
    )
    assert netlocations.unmount("smb://nas/Media/") == (False, "Target is busy")

    # macOS: diskutil unmount <local_path>
    monkeypatch.setattr(netlocations.sys, "platform", "darwin")
    du_calls = []

    def fake_darwin_run(cmd, **kw):
        if cmd[0] == "/sbin/mount":
            return _cp(0, stdout="//user@nas/Media on /Volumes/Media (smbfs, nodev)\n")
        du_calls.append(cmd)
        return _cp(0)

    monkeypatch.setattr(netlocations.subprocess, "run", fake_darwin_run)
    assert netlocations.unmount("smb://user@nas/Media/") == (True, "")
    assert du_calls == [["diskutil", "unmount", "/Volumes/Media"]]
    assert netlocations.unmount("/Volumes/Media") == (True, "")
    assert du_calls[-1] == ["diskutil", "unmount", "/Volumes/Media"]
    assert netlocations.is_smb_mount_point("/Volumes/Media") is True
    assert netlocations.is_smb_mount_point("/Volumes/USBDISK") is False

    # macOS busy error
    def fake_darwin_busy(cmd, **kw):
        if cmd[0] == "/sbin/mount":
            return _cp(0, stdout="//user@nas/Media on /Volumes/Media (smbfs, nodev)\n")
        return _cp(1, stderr="Volume Media on disk4 failed to unmount: dissented by PID 99")

    monkeypatch.setattr(netlocations.subprocess, "run", fake_darwin_busy)
    ok, err = netlocations.unmount("smb://user@nas/Media/")
    assert not ok and "failed to unmount" in err
    ok, err = netlocations.unmount("/Volumes/Media")
    assert not ok and "failed to unmount" in err
    ok, err = netlocations.unmount("/Volumes/USBDISK")
    assert not ok and "Not a mounted network location" in err


def test_trash_fallback_uses_macos_trash_on_darwin(tmp_path, monkeypatch):
    import sys

    f = tmp_path / "doc.txt"
    f.write_text("trash me")
    fake_home = tmp_path / "home"
    fake_home.mkdir()

    monkeypatch.setattr(trash.sys, "platform", "darwin")
    monkeypatch.setitem(sys.modules, "send2trash", None)  # force layer 1 failure
    monkeypatch.setattr(trash.subprocess, "run", lambda *a, **k: _cp(1))  # force layer 2 failure
    monkeypatch.setattr(
        os.path,
        "expanduser",
        lambda p: str(fake_home / p[2:]) if p.startswith("~/") else p,
    )

    assert trash.send_to_trash(str(f)) is True
    assert not f.exists()
    assert (fake_home / ".Trash" / "doc.txt").read_text() == "trash me"


def test_backend_missing_checks_gvfs_mount_files(monkeypatch, tmp_path):
    monkeypatch.setattr(netlocations.sys, "platform", "linux")
    monkeypatch.setattr(netlocations, "gio_available", lambda: True)
    mounts = tmp_path / "gvfs" / "mounts"
    mounts.mkdir(parents=True)
    monkeypatch.setattr(netlocations, "_gvfs_mount_dirs", lambda: [str(mounts)])
    monkeypatch.setattr(netlocations, "_fuse_present", lambda: True)
    assert netlocations.backend_missing("smb") is True
    (mounts / "smb.mount").write_text("")
    assert netlocations.backend_missing("smb") is False
    assert netlocations.backend_missing("sftp") is True
    monkeypatch.setattr(
        netlocations, "_fuse_present", lambda: False
    )  # backend ok but no folder to open the share in
    assert netlocations.backend_missing("smb") is True


def test_backend_missing_is_false_on_macos_or_without_gio(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "darwin")
    assert netlocations.backend_missing("smb") is False
    monkeypatch.setattr(netlocations.sys, "platform", "linux")
    monkeypatch.setattr(netlocations, "gio_available", lambda: False)
    assert netlocations.backend_missing("smb") is False


def test_backend_install_command_per_package_manager(monkeypatch):
    monkeypatch.setattr(
        netlocations.shutil, "which", lambda name: "/usr/bin/" + name if name == "apt-get" else None
    )
    assert netlocations.backend_install_command() == [
        "sudo",
        "apt-get",
        "install",
        "gvfs-backends",
        "gvfs-fuse",
    ]
    monkeypatch.setattr(
        netlocations.shutil, "which", lambda name: "/usr/bin/pacman" if name == "pacman" else None
    )
    assert netlocations.backend_install_command() == ["sudo", "pacman", "-S", "gvfs", "gvfs-smb"]
    monkeypatch.setattr(netlocations.shutil, "which", lambda name: None)
    assert netlocations.backend_install_command() is None
    assert netlocations.install_backend() is False


def test_install_backend_reports_exit_code(monkeypatch):
    monkeypatch.setattr(netlocations, "backend_install_command", lambda: ["sudo", "true"])
    monkeypatch.setattr(netlocations.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0))
    assert netlocations.install_backend() is True
    monkeypatch.setattr(netlocations.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1))
    assert netlocations.install_backend() is False


def test_anonymous_location_is_saved_and_mounted_without_prompting(monkeypatch, tmp_path):
    monkeypatch.setattr(netlocations.sys, "platform", "linux")
    monkeypatch.setattr(netlocations, "NETWORK_FILE", str(tmp_path / "network.json"))
    netlocations.save_location("smb://nas/Public/", anonymous=True)
    loc = netlocations.load_locations()[0]
    assert loc.anonymous is True and loc.user is None
    assert netlocations.is_anonymous("smb://nas/Public/") and not netlocations.is_anonymous("smb://other/")
    calls = []

    def fake_gio(args, interactive=False):
        calls.append((args, interactive))
        return _cp()

    monkeypatch.setattr(netlocations, "_gio", fake_gio)
    assert netlocations.mount("smb://nas/Public/", interactive=True) == (True, "")  # saved as anonymous
    assert calls == [(["mount", "--anonymous", "smb://nas/Public/"], False)]
    calls.clear()
    netlocations.mount("smb://nas/Public/", anonymous=False)  # explicit user login wins over the saved flag
    assert calls == [(["mount", "smb://nas/Public/"], False)]


def test_anonymous_list_shares_mounts_the_server_first(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "linux")
    calls = []

    def fake_gio(args, interactive=False):
        calls.append(args)
        return _cp(stdout="Public\nHD1\n") if args[0] == "list" else _cp()

    monkeypatch.setattr(netlocations, "_gio", fake_gio)
    assert netlocations.list_shares("nas", anonymous=True) == (["Public", "HD1"], "")
    assert calls == [["mount", "--anonymous", "smb://nas/"], ["list", "smb://nas/"]]
    monkeypatch.setattr(
        netlocations, "_gio", lambda args, interactive=False: _cp(returncode=1, stderr="denied")
    )
    assert netlocations.list_shares("nas", anonymous=True) == (None, "denied")


def test_reset_login_unmounts_the_share_and_the_server(monkeypatch):
    monkeypatch.setattr(netlocations.sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(
        netlocations, "_gio", lambda args, interactive=False: calls.append(args) or _cp(returncode=1)
    )
    netlocations.reset_login("smb://nas/Public/")  # "not mounted" errors are fine
    assert calls == [["mount", "-u", "smb://nas/Public/"], ["mount", "-u", "smb://nas/"]]
    calls.clear()
    netlocations.reset_login("smb://nas/")
    assert calls == [["mount", "-u", "smb://nas/"]]
