from typing import Iterable, Iterator, List, NoReturn, Optional, Tuple
import logging
import os
import re
import stat
import shlex
import datetime
import subprocess

from ..SAOLogging import logging_fatal

from .Base import FileMeta, FileSystem

class UnparseableLsLine(Exception):
    """An `ls -la` line the parser does not recognise; skippable, not fatal."""

class AndroidFileSystem(FileSystem):
    RE_TESTCONNECTION_NO_DEVICE = re.compile("^adb\\: no devices/emulators found$")
    RE_TESTCONNECTION_DAEMON_NOT_RUNNING = re.compile("^\\* daemon not running; starting now at tcp:\\d+$")
    RE_TESTCONNECTION_DAEMON_STARTED = re.compile("^\\* daemon started successfully$")

    RE_LS_TO_STAT = re.compile(
        r"""^
        (?:
        (?P<S_IFREG> -) |
        (?P<S_IFBLK> b) |
        (?P<S_IFCHR> c) |
        (?P<S_IFDIR> d) |
        (?P<S_IFLNK> l) |
        (?P<S_IFIFO> p) |
        (?P<S_IFSOCK> s))
        [-r][-w][-xsS]
        [-r][-w][-xsS]
        [-r][-w][-xtT] # Mode string
        [ ]+
        (?:
        [0-9]+ # Number of hard links
        [ ]+
        )?
        [^ ]+ # User name/ID
        [ ]+
        [^ ]+ # Group name/ID
        [ ]+
        (?(S_IFBLK) [^ ]+[ ]+[^ ]+[ ]+) # Device numbers
        (?(S_IFCHR) [^ ]+[ ]+[^ ]+[ ]+) # Device numbers
        (?(S_IFDIR) (?P<dirsize>[0-9]+ [ ]+))? # Directory size
        (?(S_IFREG) (?P<st_size> [0-9]+) [ ]+) # Size
        (?(S_IFLNK) ([0-9]+) [ ]+) # Link length
        (?P<st_mtime>
        [0-9]{4}-[0-9]{2}-[0-9]{2} # Date
        [ ]
        [0-9]{2}:[0-9]{2}) # Time
        [ ]
        # Don't capture filename for symlinks (ambiguous).
        (?(S_IFLNK) .* | (?P<filename> .*))
        $""", re.DOTALL | re.VERBOSE)

    RE_NO_SUCH_FILE = re.compile("^.*: No such file or directory$")
    RE_LS_NOT_A_DIRECTORY = re.compile("ls: .*: Not a directory$")
    RE_TOTAL = re.compile("^total \\d+$")

    RE_REALPATH_NO_SUCH_FILE = re.compile("^realpath: .*: No such file or directory$")
    RE_REALPATH_NOT_A_DIRECTORY = re.compile("^realpath: .*: Not a directory$")

    ADBSYNC_END_OF_COMMAND = "ADBSYNC END OF COMMAND"

    # One device-side `find` replaces the recursive per-directory `ls -la` walk.
    # %T@ is a real epoch timestamp, so mtimes no longer depend on parsing the
    # device's rendering of a date in the host's timezone, and survive being
    # older than six months (issues #48, #54). NUL record separation keeps
    # filenames containing newlines intact (issue #56).
    FIND_PRINTF_FORMAT = "%M|%s|%T@|%p\\0"

    RE_FIND_RECORD = re.compile(
        r"^(?P<type>[-dlbcps])"
        r"[-rwxsStT]{9}\|"      # permission bits, unused
        r"(?P<st_size>[0-9]+)\|"
        r"(?P<st_mtime>[0-9]+)(?:\.[0-9]+)?\|"
        r"(?P<filename>.*)$",
        re.DOTALL
    )

    RE_FIND_NO_SUCH_FILE = re.compile(r"^find: .*: No such file or directory$")

    FIND_TYPE_TO_S_IF = {
        "-": stat.S_IFREG,
        "d": stat.S_IFDIR,
        "l": stat.S_IFLNK,
        "b": stat.S_IFBLK,
        "c": stat.S_IFCHR,
        "p": stat.S_IFIFO,
        "s": stat.S_IFSOCK,
    }

    def __init__(self, adb_arguments: List[str], adb_encoding: str) -> None:
        super().__init__(adb_arguments)
        self.adb_encoding = adb_encoding
        # None until probed; see supports_find_printf
        self._find_printf_supported: Optional[bool] = None
        # bound before Popen so __del__ never raises AttributeError if the spawn
        # itself fails, which would otherwise mask the real error (issue #43)
        self.proc_adb_shell = None
        # No PTY is allocated because stdin is a pipe, so device output reaches
        # us verbatim: newlines inside filenames are not CRLF-translated.
        # Do not pass -T here; it changes adb's buffering and the marker echo
        # never arrives, hanging the read loop.
        self.proc_adb_shell = subprocess.Popen(
            self.adb_arguments + ["shell"],
            stdin = subprocess.PIPE,
            stdout = subprocess.PIPE,
            stderr = subprocess.STDOUT
        )

    def __del__(self):
        proc = getattr(self, "proc_adb_shell", None)
        if proc is None:
            return
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
            proc.wait(timeout = 10)
        except Exception:
            # interpreter teardown; nothing useful left to report
            pass

    def decode(self, data: bytes) -> str:
        """Decode device output, preserving undecodable bytes rather than dying.

        Filenames on Android are arbitrary byte strings and need not be valid in
        adb_encoding; surrogateescape round-trips them instead of raising
        UnicodeDecodeError (issues #42, #44, #51).
        """
        return data.decode(self.adb_encoding, errors = "surrogateescape")

    def adb_shell_raw(self, commands: List[str]) -> bytes:
        """Run a command and return its raw, undecoded output.

        Needed for NUL-separated output, which cannot be read line-wise.
        """
        self.proc_adb_shell.stdin.write(shlex.join(commands).encode(self.adb_encoding, errors = "surrogateescape"))
        self.proc_adb_shell.stdin.write(" </dev/null\n".encode(self.adb_encoding))
        self.proc_adb_shell.stdin.write(shlex.join(["echo", self.ADBSYNC_END_OF_COMMAND]).encode(self.adb_encoding))
        self.proc_adb_shell.stdin.write(" </dev/null\n".encode(self.adb_encoding))
        self.proc_adb_shell.stdin.flush()

        # The marker cannot be matched line-wise: NUL-separated output has no
        # trailing newline, so the final record and the echoed marker arrive as
        # one readline(). Accumulate instead and strip the marker off the end.
        marker = self.ADBSYNC_END_OF_COMMAND.encode(self.adb_encoding)
        buffer = bytearray()
        while adb_line := self.proc_adb_shell.stdout.readline():
            buffer += adb_line
            trimmed = buffer.rstrip(b"\r\n")
            if trimmed.endswith(marker):
                buffer = trimmed[:-len(marker)]
                break
        return bytes(buffer)

    def adb_shell(self, commands: List[str]) -> Iterator[str]:
        text = self.decode(self.adb_shell_raw(commands))
        # a command that printed nothing must yield no lines, not one blank one:
        # callers treat any unexpected line as fatal
        if text.endswith("\n"):
            text = text[:-1]
        if not text:
            return
        for line in text.split("\n"):
            yield line.rstrip("\r")

    def line_not_captured(self, line: str) -> NoReturn:
        logging.critical("ADB line not captured")
        logging_fatal(line)

    def test_connection(self):
        for line in self.adb_shell([":"]):
            print(line)

            if self.RE_TESTCONNECTION_DAEMON_NOT_RUNNING.fullmatch(line) or self.RE_TESTCONNECTION_DAEMON_STARTED.fullmatch(line):
                continue

            raise BrokenPipeError

    def ls_to_stat(self, line: str) -> Tuple[str, os.stat_result]:
        if self.RE_NO_SUCH_FILE.fullmatch(line):
            raise FileNotFoundError
        elif self.RE_LS_NOT_A_DIRECTORY.fullmatch(line):
            raise NotADirectoryError
        elif match := self.RE_LS_TO_STAT.fullmatch(line):
            match_groupdict = match.groupdict()
            st_mode = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH # 755
            if match_groupdict['S_IFREG']:
                st_mode |= stat.S_IFREG
            if match_groupdict['S_IFBLK']:
                st_mode |= stat.S_IFBLK
            if match_groupdict['S_IFCHR']:
                st_mode |= stat.S_IFCHR
            if match_groupdict['S_IFDIR']:
                st_mode |= stat.S_IFDIR
            if match_groupdict['S_IFIFO']:
                st_mode |= stat.S_IFIFO
            if match_groupdict['S_IFLNK']:
                st_mode |= stat.S_IFLNK
            if match_groupdict['S_IFSOCK']:
                st_mode |= stat.S_IFSOCK
            st_size = None if match_groupdict["st_size"] is None else int(match_groupdict["st_size"])
            st_mtime = int(datetime.datetime.strptime(match_groupdict["st_mtime"], "%Y-%m-%d %H:%M").timestamp())

            # Fill the rest with dummy values.
            st_ino = 1
            st_rdev = 0
            st_nlink = 1
            st_uid = -2  # Nobody.
            st_gid = -2  # Nobody.
            st_atime = st_ctime = st_mtime

            return match_groupdict["filename"], os.stat_result((st_mode, st_ino, st_rdev, st_nlink, st_uid, st_gid, st_size, st_atime, st_mtime, st_ctime))
        else:
            raise UnparseableLsLine(line)

    # --- find-based listing -------------------------------------------------

    def supports_find_printf(self) -> bool:
        """Whether the device's find understands -printf, probed once."""
        if self._find_printf_supported is None:
            probe = self.decode(self.adb_shell_raw(
                ["find", "/", "-maxdepth", "0", "-printf", self.FIND_PRINTF_FORMAT]
            )).rstrip("\0")
            self._find_printf_supported = self.RE_FIND_RECORD.fullmatch(probe) is not None
            if not self._find_printf_supported:
                logging.warning(
                    "Device's find does not support -printf; falling back to parsing 'ls -la'. "
                    "Timestamps will be minute-resolution."
                )
                logging.debug(f"find probe returned: {probe!r}")
        return self._find_printf_supported

    def parse_find_output(self, raw: bytes) -> List[re.Match]:
        """Split NUL-separated find records, logging any interleaved diagnostics.

        find writes errors (permission denied, missing paths) to stderr, which is
        merged into stdout by the shared adb shell. Those are newline-terminated
        while records are NUL-terminated, so they surface as unparseable leading
        text within a chunk.
        """
        records: List[re.Match] = []
        for chunk in self.decode(raw).split("\0"):
            if not chunk:
                continue
            # try the whole chunk first: a filename may legitimately contain \n
            match = self.RE_FIND_RECORD.fullmatch(chunk)
            if match is not None:
                records.append(match)
                continue
            *diagnostics, candidate = chunk.split("\n")
            match = self.RE_FIND_RECORD.fullmatch(candidate) if candidate else None
            if match is not None:
                records.append(match)
            else:
                diagnostics = chunk.split("\n")
            for line in diagnostics:
                line = line.rstrip("\r")
                if not line:
                    continue
                if self.RE_FIND_NO_SUCH_FILE.fullmatch(line):
                    # an absent path is normal: the caller turns the empty
                    # result into FileNotFoundError and handles it
                    logging.debug(line)
                else:
                    logging.warning(line)
        return records

    def relative_parts(self, root: str, path: str) -> Optional[List[str]]:
        """Path components of `path` relative to `root`, or None if not under it."""
        if path == root:
            return []
        prefix = root if root.endswith("/") else root + "/"
        if not path.startswith(prefix):
            return None
        return path[len(prefix):].split("/")

    def get_files_tree(self, tree_path: str, follow_links: bool = False):
        if not self.supports_find_printf():
            return super().get_files_tree(tree_path, follow_links = follow_links)

        command = ["find"]
        if follow_links:
            command.append("-L")
        command += [tree_path, "-printf", self.FIND_PRINTF_FORMAT]

        records = self.parse_find_output(self.adb_shell_raw(command))
        if not records:
            raise FileNotFoundError(tree_path)

        root = self.normpath(tree_path)
        root_meta = None
        root_is_dir = False
        entries: List[Tuple[List[str], bool, FileMeta]] = []

        for match in records:
            groups = match.groupdict()
            type_char = groups["type"]
            path = groups["filename"]
            parts = self.relative_parts(root, path)
            if parts is None:
                logging.warning(f"Ignoring unexpected path from find: {path}")
                continue

            if type_char == "l":
                # without -L find does not descend symlinks; with -L, anything
                # still reported as a link is one it could not resolve
                if follow_links:
                    logging.warning(f"Skipping broken symlink {path}")
                else:
                    logging.warning(f"Ignoring symlink {path}")
                if not parts:
                    return None
                continue
            if type_char not in ("-", "d"):
                logging.warning(f"Ignoring special file {path}")
                if not parts:
                    return None
                continue

            mtime = int(groups["st_mtime"])
            is_dir = type_char == "d"
            # find gives no atime; the ls-based path had none either and reused
            # mtime for it, so keep that behaviour
            meta = FileMeta(mtime, mtime, None if is_dir else int(groups["st_size"]))

            if not parts:
                root_meta, root_is_dir = meta, is_dir
            else:
                entries.append((parts, is_dir, meta))

        if root_meta is None:
            raise FileNotFoundError(tree_path)
        if not root_is_dir:
            return root_meta

        tree = {".": root_meta}
        for parts, is_dir, meta in entries:
            node = tree
            for part in parts[:-1]:
                node = node.get(part)
                if not isinstance(node, dict):
                    node = None
                    break
            if node is None:
                continue # a parent directory was skipped above
            node[parts[-1]] = {".": meta} if is_dir else meta
        return tree

    @property
    def sep(self) -> str:
        return "/"

    def unlink(self, path: str) -> None:
        for line in self.adb_shell(["rm", path]):
            self.line_not_captured(line)

    def rmdir(self, path: str) -> None:
        for line in self.adb_shell(["rm", "-r", path]):
            self.line_not_captured(line)

    def makedirs(self, path: str) -> None:
        for line in self.adb_shell(["mkdir", "-p", path]):
            self.line_not_captured(line)

    def realpath(self, path: str) -> str:
        for line in self.adb_shell(["realpath", path]):
            if self.RE_REALPATH_NO_SUCH_FILE.fullmatch(line):
                raise FileNotFoundError
            elif self.RE_REALPATH_NOT_A_DIRECTORY.fullmatch(line):
                raise NotADirectoryError
            else:
                return line
            # permission error possible?

    def find_record_to_stat(self, match: re.Match) -> os.stat_result:
        groups = match.groupdict()
        st_mode = stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH # 755
        st_mode |= self.FIND_TYPE_TO_S_IF[groups["type"]]
        st_mtime = int(groups["st_mtime"])
        return os.stat_result((
            st_mode, 1, 0, 1, -2, -2, int(groups["st_size"]), st_mtime, st_mtime, st_mtime
        ))

    def lstat(self, path: str) -> os.stat_result:
        if self.supports_find_printf():
            records = self.parse_find_output(self.adb_shell_raw(
                ["find", path, "-maxdepth", "0", "-printf", self.FIND_PRINTF_FORMAT]
            ))
            if not records:
                raise FileNotFoundError(path)
            return self.find_record_to_stat(records[0])
        for line in self.adb_shell(["ls", "-lad", path]):
            try:
                return self.ls_to_stat(line)[1]
            except UnparseableLsLine:
                self.line_not_captured(line)
        raise FileNotFoundError(path)

    def lstat_in_dir(self, path: str) -> Iterable[Tuple[str, os.stat_result]]:
        for line in self.adb_shell(["ls", "-la", path]):
            if self.RE_TOTAL.fullmatch(line):
                continue
            try:
                yield self.ls_to_stat(line)
            except UnparseableLsLine:
                # one unreadable entry must not abort the whole sync (issue #53)
                logging.warning(f"Skipping unparseable entry in {path}: {line}")

    @property
    def mtime_precision(self) -> int:
        # `find -printf %T@` and `touch -t ....ss` are second-accurate; the
        # `ls -la` fallback can only see whole minutes
        return 1 if self.supports_find_printf() else 60

    def utime(self, path: str, times: Tuple[int, int]) -> None:
        # the .%S suffix preserves seconds rather than truncating to the
        # minute, which previously made every restored mtime drift (issue #48)
        atime = datetime.datetime.fromtimestamp(times[0]).strftime("%Y%m%d%H%M.%S")
        mtime = datetime.datetime.fromtimestamp(times[1]).strftime("%Y%m%d%H%M.%S")
        for line in self.adb_shell(["touch", "-at", atime, "-mt", mtime, path]):
            self.line_not_captured(line)

    def join(self, base: str, leaf: str) -> str:
        return os.path.join(base, leaf).replace("\\", "/") # for Windows

    def split(self, path: str) -> Tuple[str, str]:
        head, tail = os.path.split(path)
        return head.replace("\\", "/"), tail # for Windows

    def normpath(self, path: str) -> str:
        return os.path.normpath(path).replace("\\", "/")

    def push_file_here(self, source: str, destination: str, show_progress: bool = False) -> None:
        if show_progress:
            kwargs_call = {}
        else:
            kwargs_call = {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL
            }
        if subprocess.call(self.adb_arguments + ["push", source, destination], **kwargs_call):
            logging_fatal("Non-zero exit code from adb push")
