# Bestest ADB Sync

[![tests](https://github.com/BuildWithDuke/bestest-adb-sync/actions/workflows/tests.yml/badge.svg)](https://github.com/BuildWithDuke/bestest-adb-sync/actions/workflows/tests.yml)

An [rsync](https://wiki.archlinux.org/title/rsync)-like program to sync files between a computer and an Android device

> **This is a modified fork.**
> It is based on [jb2170/better-adb-sync](https://github.com/jb2170/better-adb-sync)
> (Apache License 2.0), which has been unmaintained since September 2023. The
> files in `src/BestestADBSync/` have been changed from the upstream versions;
> see [What this fork changes](#what-this-fork-changes) below.
> Upstream in turn is a rewrite of Google's [adb-sync](https://github.com/google/adb-sync).

## What this fork changes

**Sync compares file size, not just mtime.** Upstream decides a file is already
synced using `source_mtime > destination_mtime` alone, having parsed the size
from `ls` and then discarded it. This silently corrupts archives: an interrupted
transfer leaves a truncated file whose mtime is the moment it was written, which
is *newer* than the source, so it is never re-fetched and stays truncated
forever. Fixes upstream issues
[#23](https://github.com/jb2170/better-adb-sync/issues/23) and
[#32](https://github.com/jb2170/better-adb-sync/issues/32).

**File metadata is read with one device-side `find`** rather than a recursive
per-directory `ls -la` walk:

- `-printf %T@` yields a real epoch timestamp, so mtimes no longer depend on
  parsing the device's *rendering* of a date in the host's timezone, no longer
  truncate to the minute ([#48](https://github.com/jb2170/better-adb-sync/issues/48)),
  and no longer break on files older than six months
  ([#54](https://github.com/jb2170/better-adb-sync/issues/54))
- NUL-separated records keep filenames containing newlines intact
  ([#56](https://github.com/jb2170/better-adb-sync/issues/56))
- one round trip replaces one per directory

Devices whose `find` lacks `-printf` fall back to the previous `ls` parsing. A
per-filesystem `mtime_precision` feeds a comparison tolerance, so that path's
minute resolution does not make every file look perpetually stale.

Also fixed:

- device output is decoded with `surrogateescape`, so filenames that are not
  valid in the chosen encoding no longer raise `UnicodeDecodeError`
  ([#42](https://github.com/jb2170/better-adb-sync/issues/42),
  [#44](https://github.com/jb2170/better-adb-sync/issues/44),
  [#51](https://github.com/jb2170/better-adb-sync/issues/51))
- a failed constructor no longer masks its own error with `AttributeError`
  during teardown ([#43](https://github.com/jb2170/better-adb-sync/issues/43))
- one unparseable `ls` entry is skipped with a warning instead of aborting the
  entire sync ([#53](https://github.com/jb2170/better-adb-sync/issues/53))
- seconds are preserved when restoring mtimes on the device

There is a test suite; none of it requires a connected device:

```
$ pip install -e '.[dev]'
$ pytest
```

It runs in CI against Python 3.8 through 3.13 on Linux, and 3.12 on macOS.

## Installation

From source:

```
$ git clone https://github.com/BuildWithDuke/bestest-adb-sync
$ cd bestest-adb-sync
$ pip install .
```

Note that `pip install BetterADBSync` installs **upstream** from
[PyPI](https://pypi.org/project/BetterADBSync/), not this fork.

## QRD

To push from your computer to your phone use
```
$ adbsync push LOCAL ANDROID
```

To pull from your phone to your computer use
```
$ adbsync pull ANDROID LOCAL
```

Full help is available with `$ adbsync --help`

## Intro

This is a (pretty much from scratch) rewrite of Google's [adbsync](https://github.com/google/adb-sync) repo.

The reason for the rewrite is to

1. Update the repo to Python 3 codestyle (strings are by default UTF-8, no more b"" and u"", classes don't need to inherit from object, 4 space indentation etc)
2. Add in support for `--exclude`, `--exclude-from`, `--del`, `--delete-excluded` like `rsync` has (this required a complete rewrite of the diffing algorithm)

## Additions

- `--del` will delete files and folders on the destination end that are not present on the source end. This does not include exluded files.
- `--delete-excluded` will delete excluded files and folders on the destination end.
- `--exclude` can be used many times. Each should be a `fnmatch` pattern relative to the source. These patterns will be ignored unless `--delete-excluded` is specified.
- `--exclude-from` can be used many times. Each should be a filename of a file containing `fnmatch` patterns relative to the source.

## Possible future TODOs

I am satisfied with my code so far, however a few things could be added if they are ever needed

- `--backup` and `--backup-dir-local` or `--backup-dir-android` to move outdated / to-delete files to another folder instead of deleting

---

---BEGIN ORIGINAL README.md---

adb-sync
========

adb-sync is a tool to synchronize files between a PC and an Android device
using the ADB (Android Debug Bridge).

Related Projects
================

Before getting used to this, please review this list of projects that are
somehow related to adb-sync and may fulfill your needs better:

* [rsync](http://rsync.samba.org/) is a file synchronization tool for local
  (including FUSE) file systems or SSH connections. This can be used even with
  Android devices if rooted or using an app like
  [SSHelper](https://play.google.com/store/apps/details?id=com.arachnoid.sshelper).
* [adbfs](http://collectskin.com/adbfs/) is a FUSE file system that uses adb to
  communicate to the device. Requires a rooted device, though.
* [adbfs-rootless](https://github.com/spion/adbfs-rootless) is a fork of adbfs
  that requires no root on the device. Does not play very well with rsync.
* [go-mtpfs](https://github.com/hanwen/go-mtpfs) is a FUSE file system to
  connect to Android devices via MTP. Due to MTP's restrictions, only a certain
  set of file extensions is supported. To store unsupported files, just add
  .txt! Requires no USB debugging mode.

Setup
=====

Android Side
------------

First you need to enable USB debugging mode. This allows authorized computers
(on Android before 4.4.3 all computers) to perform possibly dangerous
operations on your device. If you do not accept this risk, do not proceed and
try using [go-mtpfs](https://github.com/hanwen/go-mtpfs) instead!

On your Android device:

* Go to the Settings app.
* If there is no "Developer Options" menu:
  * Select "About".
  * Tap "Build Number" seven times.
  * Go back.
* Go to "Developer Options".
* Enable "USB Debugging".

PC Side
-------

* Install the [Android SDK](http://developer.android.com/sdk/index.html) (the
  stand-alone Android SDK "for an existing IDE" is sufficient). Alternatively,
  some Linux distributions come with a package named like "android-tools-adb"
  that contains the required tool.
* Make sure "adb" is in your PATH. If you use a package from your Linux
  distribution, this should already be the case; if you used the SDK, you
  probably will have to add an entry to PATH in your ~/.profile file, log out
  and log back in.
* `git clone https://github.com/google/adb-sync`
* `cd adb-sync`
* Copy or symlink the adb-sync script somewhere in your PATH. For example:
  `cp adb-sync /usr/local/bin/`

Usage
=====

To get a full help, type:

```
adb-sync --help
```

To synchronize your music files from ~/Music to your device, type one of:

```
adb-sync ~/Music /sdcard
adb-sync ~/Music/ /sdcard/Music
```

To synchronize your music files from ~/Music to your device, deleting files you
removed from your PC, type one of:

```
adb-sync --delete ~/Music /sdcard
adb-sync --delete ~/Music/ /sdcard/Music
```

To copy all downloads from your device to your PC, type:

```
adb-sync --reverse /sdcard/Download/ ~/Downloads
```

ADB Channel
===========

This package also contains a separate tool called adb-channel, which is a
convenience wrapper to connect a networking socket on the Android device to
file descriptors on the PC side. It can even launch and shut down the given
application automatically!

It is best used as a `ProxyCommand` for SSH (install
[SSHelper](https://play.google.com/store/apps/details?id=com.arachnoid.sshelper)
first) using a configuration like:

```
Host sshelper
Port 2222
ProxyCommand adb-channel tcp:%p com.arachnoid.sshelper/.SSHelperActivity 1
```

After adding this to `~/.ssh/config`, run `ssh-copy-id sshelper`.

Congratulations! You can now use `rsync`, `sshfs` etc. to the host name
`sshelper`.

Contributing
============

Patches to this project are very welcome.

Before sending a patch or pull request, we ask you to fill out one of the
Contributor License Agreements:

* [Google Individual Contributor License Agreement, v1.1](https://developers.google.com/open-source/cla/individual)
* [Google Software Grant and Corporate Contributor License Agreement, v1.1](https://developers.google.com/open-source/cla/corporate)

Disclaimer
==========

This is not an official Google product.


---END ORIGINAL README.md---

---
