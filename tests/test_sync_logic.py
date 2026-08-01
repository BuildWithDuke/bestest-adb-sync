"""Tests for the sync decision logic and the device output parsers.

None of these require a connected device.
"""

import os
import stat

import pytest

from BetterADBSync import FileSyncer
from BetterADBSync.FileSystems.Base import FileMeta
from BetterADBSync.FileSystems.Android import AndroidFileSystem, UnparseableLsLine


def make_afs(find_supported: bool = True) -> AndroidFileSystem:
    """An AndroidFileSystem with no adb subprocess behind it."""
    afs = AndroidFileSystem.__new__(AndroidFileSystem)
    afs.adb_arguments = ["adb"]
    afs.adb_encoding = "UTF-8"
    afs._find_printf_supported = find_supported
    afs.proc_adb_shell = None
    return afs


def record(type_char: str, size: int, mtime: str, path: str) -> bytes:
    return f"{type_char}rwxrwx---|{size}|{mtime}|{path}\0".encode()


class TestNeedsCopy:
    def test_half_pulled_file_is_recopied(self):
        """Issue #23/#32: an interrupted transfer leaves a truncated file whose
        mtime is newer than the source. mtime alone says 'done'; size says no."""
        source = FileMeta(1000, 1000, 5_000_000)
        truncated_destination = FileMeta(2000, 2000, 1_200_000)
        assert FileSyncer.needs_copy(source, truncated_destination) is True

    def test_identical_file_is_not_recopied(self):
        meta = FileMeta(1000, 1000, 5_000_000)
        assert FileSyncer.needs_copy(meta, meta) is False

    def test_newer_source_is_copied(self):
        assert FileSyncer.needs_copy(FileMeta(2000, 2000, 100), FileMeta(1000, 1000, 100)) is True

    def test_older_source_is_not_copied(self):
        assert FileSyncer.needs_copy(FileMeta(1000, 1000, 100), FileMeta(2000, 2000, 100)) is False

    def test_same_size_within_tolerance_is_not_copied(self):
        """A 59s drift must not trigger a copy when one side is minute-resolution."""
        source = FileMeta(1059, 1059, 100)
        destination = FileMeta(1000, 1000, 100)
        assert FileSyncer.needs_copy(source, destination, mtime_tolerance = 60) is False
        assert FileSyncer.needs_copy(source, destination, mtime_tolerance = 0) is True

    def test_size_difference_beats_tolerance(self):
        source = FileMeta(1000, 1000, 200)
        destination = FileMeta(1000, 1000, 100)
        assert FileSyncer.needs_copy(source, destination, mtime_tolerance = 3600) is True

    def test_legacy_two_tuples_still_compare(self):
        """Sizeless leaves fall back to mtime-only comparison."""
        assert FileSyncer.needs_copy((1000, 2000), (1000, 1000)) is True
        assert FileSyncer.needs_copy((1000, 1000), (1000, 2000)) is False


class TestDiffTrees:
    @staticmethod
    def diff(source, destination, tolerance = 0):
        return FileSyncer.diff_trees(
            source, destination, "/src", "/dst", [],
            os.path.join, os.path.join,
            folder_file_overwrite_error = False,
            mtime_tolerance = tolerance,
        )

    def test_truncated_file_lands_in_copy_tree(self):
        source = {".": FileMeta(1, 1, None), "a.jpg": FileMeta(1000, 1000, 5_000_000)}
        destination = {".": FileMeta(1, 1, None), "a.jpg": FileMeta(2000, 2000, 1_200_000)}
        _, copy, _, _, _ = self.diff(source, destination)
        assert FileSyncer.prune_tree(copy) == {"a.jpg": FileMeta(1000, 1000, 5_000_000)}

    def test_complete_file_is_left_alone(self):
        meta = FileMeta(1000, 1000, 5_000_000)
        source = {".": FileMeta(1, 1, None), "a.jpg": meta}
        destination = {".": FileMeta(1, 1, None), "a.jpg": meta}
        _, copy, _, _, _ = self.diff(source, destination)
        assert FileSyncer.prune_tree(copy) is None


class TestParseFindOutput:
    def test_parses_plain_records(self):
        afs = make_afs()
        raw = record("d", 4096, "1779351448.109322183", "/sdcard/DCIM") + \
              record("-", 2048576, "1785606518.957574372", "/sdcard/DCIM/a.jpg")
        records = afs.parse_find_output(raw)
        assert [m.group("filename") for m in records] == ["/sdcard/DCIM", "/sdcard/DCIM/a.jpg"]
        assert records[1].group("st_size") == "2048576"
        assert records[1].group("st_mtime") == "1785606518"

    def test_filename_containing_newline_survives(self):
        """Issue #56: NUL separation keeps a newline inside a filename intact."""
        afs = make_afs()
        records = afs.parse_find_output(record("-", 10, "1000", "/sdcard/we\nird.jpg"))
        assert len(records) == 1
        assert records[0].group("filename") == "/sdcard/we\nird.jpg"

    def test_interleaved_stderr_is_logged_not_fatal(self, caplog):
        """Issue #53: an unreadable entry must not abort the listing."""
        afs = make_afs()
        raw = record("d", 4096, "1000", "/sdcard") + \
              b"find: /sdcard/secret: Permission denied\n" + \
              record("-", 10, "2000", "/sdcard/a.jpg")
        records = afs.parse_find_output(raw)
        assert [m.group("filename") for m in records] == ["/sdcard", "/sdcard/a.jpg"]
        assert "Permission denied" in caplog.text

    def test_fractional_mtime_is_truncated_to_seconds(self):
        afs = make_afs()
        records = afs.parse_find_output(record("-", 1, "1785606518.957574372", "/x"))
        assert records[0].group("st_mtime") == "1785606518"


class TestGetFilesTree:
    def test_builds_nested_tree(self, monkeypatch):
        afs = make_afs()
        raw = record("d", 4096, "100", "/sdcard/DCIM") + \
              record("d", 4096, "200", "/sdcard/DCIM/Camera") + \
              record("-", 500, "300", "/sdcard/DCIM/Camera/a.jpg") + \
              record("-", 600, "400", "/sdcard/DCIM/b.jpg")
        monkeypatch.setattr(afs, "adb_shell_raw", lambda commands: raw)
        assert afs.get_files_tree("/sdcard/DCIM") == {
            ".": FileMeta(100, 100, None),
            "Camera": {".": FileMeta(200, 200, None), "a.jpg": FileMeta(300, 300, 500)},
            "b.jpg": FileMeta(400, 400, 600),
        }

    def test_single_file_root_returns_leaf(self, monkeypatch):
        afs = make_afs()
        monkeypatch.setattr(afs, "adb_shell_raw", lambda commands: record("-", 42, "900", "/sdcard/a.jpg"))
        assert afs.get_files_tree("/sdcard/a.jpg") == FileMeta(900, 900, 42)

    def test_missing_path_raises_filenotfound(self, monkeypatch):
        afs = make_afs()
        monkeypatch.setattr(afs, "adb_shell_raw",
            lambda commands: b"find: /nope: No such file or directory\n")
        with pytest.raises(FileNotFoundError):
            afs.get_files_tree("/nope")

    def test_missing_path_is_not_warned_about(self, monkeypatch, caplog):
        """A destination that does not exist yet is normal, not a warning."""
        import logging as _logging
        afs = make_afs()
        monkeypatch.setattr(afs, "adb_shell_raw",
            lambda commands: b"find: '/nope': No such file or directory\n")
        with caplog.at_level(_logging.WARNING):
            afs.parse_find_output(afs.adb_shell_raw([]))
        assert caplog.text == ""

    def test_symlink_is_skipped_without_copy_links(self, monkeypatch):
        afs = make_afs()
        raw = record("d", 4096, "100", "/sdcard/DCIM") + \
              record("l", 21, "200", "/sdcard/DCIM/link") + \
              record("-", 500, "300", "/sdcard/DCIM/a.jpg")
        monkeypatch.setattr(afs, "adb_shell_raw", lambda commands: raw)
        tree = afs.get_files_tree("/sdcard/DCIM")
        assert "link" not in tree
        assert "a.jpg" in tree

    def test_children_of_skipped_directory_are_dropped(self, monkeypatch):
        afs = make_afs()
        raw = record("d", 4096, "100", "/sdcard/DCIM") + \
              record("l", 21, "200", "/sdcard/DCIM/link") + \
              record("-", 500, "300", "/sdcard/DCIM/link/orphan.jpg")
        monkeypatch.setattr(afs, "adb_shell_raw", lambda commands: raw)
        tree = afs.get_files_tree("/sdcard/DCIM")
        assert tree == {".": FileMeta(100, 100, None)}


class TestRelativeParts:
    @pytest.mark.parametrize("root, path, expected", [
        ("/sdcard/DCIM", "/sdcard/DCIM", []),
        ("/sdcard/DCIM", "/sdcard/DCIM/a.jpg", ["a.jpg"]),
        ("/sdcard/DCIM", "/sdcard/DCIM/Camera/a.jpg", ["Camera", "a.jpg"]),
        ("/", "/sdcard", ["sdcard"]),
        ("/sdcard/DCIM", "/sdcard/DCIMOTHER/a.jpg", None),
        ("/sdcard/DCIM", "/elsewhere", None),
    ])
    def test_relative_parts(self, root, path, expected):
        assert make_afs().relative_parts(root, path) == expected


class TestLsFallback:
    def test_unparseable_line_raises_skippable_error(self):
        afs = make_afs(find_supported = False)
        with pytest.raises(UnparseableLsLine):
            afs.ls_to_stat("this is not an ls line")

    def test_lstat_in_dir_skips_bad_lines(self, monkeypatch, caplog):
        afs = make_afs(find_supported = False)
        lines = [
            "total 8",
            "-rw-rw---- 1 root sdcard_rw 500 2024-01-15 10:30 a.jpg",
            "?????????? ? ?    ?         ?   ?                ohno",
            "-rw-rw---- 1 root sdcard_rw 600 2024-01-15 10:31 b.jpg",
        ]
        monkeypatch.setattr(afs, "adb_shell", lambda commands: iter(lines))
        results = list(afs.lstat_in_dir("/sdcard"))
        assert [name for name, _ in results] == ["a.jpg", "b.jpg"]
        assert "Skipping unparseable entry" in caplog.text

    def test_ls_parses_size_and_type(self):
        afs = make_afs(find_supported = False)
        name, st = afs.ls_to_stat("-rw-rw---- 1 root sdcard_rw 500 2024-01-15 10:30 a.jpg")
        assert name == "a.jpg"
        assert st.st_size == 500
        assert stat.S_ISREG(st.st_mode)

    def test_fallback_reports_minute_precision(self):
        assert make_afs(find_supported = False).mtime_precision == 60
        assert make_afs(find_supported = True).mtime_precision == 1


class TestAdbShellLineSplitting:
    def test_empty_output_yields_no_lines(self, monkeypatch):
        """A silent command must yield nothing; callers treat any line as fatal."""
        afs = make_afs()
        monkeypatch.setattr(afs, "adb_shell_raw", lambda commands: b"")
        assert list(afs.adb_shell(["rm", "/sdcard/x"])) == []

    def test_trailing_newline_does_not_produce_blank_line(self, monkeypatch):
        afs = make_afs()
        monkeypatch.setattr(afs, "adb_shell_raw", lambda commands: b"one\ntwo\n")
        assert list(afs.adb_shell(["ls"])) == ["one", "two"]

    def test_undecodable_bytes_do_not_raise(self, monkeypatch):
        """Issues #42/#44/#51: filenames need not be valid UTF-8."""
        afs = make_afs()
        monkeypatch.setattr(afs, "adb_shell_raw", lambda commands: b"caf\xa0.jpg\n")
        assert list(afs.adb_shell(["ls"])) == ["caf\udca0.jpg"]


class FakeStdout:
    def __init__(self, data: bytes):
        self._lines = data.splitlines(keepends = True)
        self._index = 0

    def readline(self) -> bytes:
        if self._index >= len(self._lines):
            return b""  # a real pipe would block here forever
        line = self._lines[self._index]
        self._index += 1
        return line


class TestAdbShellRaw:
    @staticmethod
    def make(data: bytes) -> AndroidFileSystem:
        afs = make_afs()
        afs.proc_adb_shell = type("P", (), {})()
        afs.proc_adb_shell.stdout = FakeStdout(data)
        afs.proc_adb_shell.stdin = type("W", (), {
            "write": lambda self, b: None, "flush": lambda self: None
        })()
        return afs

    MARK = AndroidFileSystem.ADBSYNC_END_OF_COMMAND.encode()

    def test_nul_output_without_trailing_newline(self):
        """The final NUL record and the echoed marker arrive as one readline();
        matching the marker line-wise hangs the read loop forever."""
        afs = self.make(b"drwx------|4096|100|/sdcard\0" + self.MARK + b"\n")
        assert afs.adb_shell_raw(["find"]) == b"drwx------|4096|100|/sdcard\0"

    def test_newline_terminated_output(self):
        afs = self.make(b"one\ntwo\n" + self.MARK + b"\n")
        assert afs.adb_shell_raw(["ls"]) == b"one\ntwo\n"

    def test_empty_output(self):
        afs = self.make(self.MARK + b"\n")
        assert afs.adb_shell_raw([":"]) == b""

    def test_multiple_nul_records(self):
        raw = b"d---------|1|1|/a\0----------|2|2|/a/b\0"
        afs = self.make(raw + self.MARK + b"\n")
        assert afs.adb_shell_raw(["find"]) == raw


class TestTeardown:
    def test_del_without_subprocess_does_not_raise(self):
        """Issue #43: a failed constructor must not mask its error in __del__."""
        afs = AndroidFileSystem.__new__(AndroidFileSystem)
        afs.__del__()  # no proc_adb_shell attribute at all
