#!/usr/bin/env python3
"""Expire regular cache files after 30 days without reads or changes."""
import argparse
import datetime
import json
import os
import stat
import time

TTL_SECONDS = 30 * 24 * 60 * 60
INTERNAL_NAMES = {'.trash', '.jfs', '.accesslog', '.stats', '.config', '.control'}


def sweep(root, *, tracked_since, apply=False, readers_quiesced=False, now=None):
    """Use only after strict access-time tracking is enabled on every client.

    POSIX has no atomic unlink-if-last-read-unchanged operation. Apply therefore
    requires a maintenance window with all readers and writers paused.
    """
    now = time.time() if now is None else now
    if not 0 < tracked_since <= now:
        raise ValueError('tracking start must be a past Unix timestamp')
    if apply and not readers_quiesced:
        raise ValueError('apply requires all cache readers and writers to be paused')
    root = os.path.abspath(root)
    root_stat = os.stat(root, follow_symlinks=False)
    if not stat.S_ISDIR(root_stat.st_mode) or root == '/':
        raise ValueError('root must be a real cache directory, not / or a symlink')
    result = dict(scanned=0, eligible=0, deleted=0, bytes_deleted=0,
                  changed=0, errors=0, warming_up=now - tracked_since < TTL_SECONDS)
    if result['warming_up']:
        return result
    cutoff = now - TTL_SECONDS

    def failed(_error):
        result['errors'] += 1

    # Descriptor-relative operations prevent a renamed ancestor from redirecting
    # an unlink through a replacement symlink outside the cache.
    for directory, dirs, files, fd in os.fwalk(root, follow_symlinks=False, onerror=failed):
        dirs[:] = [name for name in dirs if name not in INTERNAL_NAMES and not name.startswith('.jfs.')]
        for name in dirs[:]:
            try:
                entry = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if entry.st_dev != root_stat.st_dev or not stat.S_ISDIR(entry.st_mode):
                    dirs.remove(name)
            except OSError:
                dirs.remove(name)
                result['errors'] += 1
        for name in files:
            if name in INTERNAL_NAMES or name.startswith('.jfs.'):
                continue
            try:
                before = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if not stat.S_ISREG(before.st_mode) or before.st_dev != root_stat.st_dev:
                    continue
                result['scanned'] += 1
                # A recent write/metadata change protects never-read/new files too.
                if max(before.st_atime, before.st_mtime, before.st_ctime) >= cutoff:
                    continue
                result['eligible'] += 1
                if not apply:
                    continue
                after = os.stat(name, dir_fd=fd, follow_symlinks=False)
                if (before.st_dev, before.st_ino, before.st_size,
                    before.st_atime_ns, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_dev, after.st_ino, after.st_size,
                    after.st_atime_ns, after.st_mtime_ns, after.st_ctime_ns):
                    result['changed'] += 1
                    continue
                os.unlink(name, dir_fd=fd)
                result['deleted'] += 1
                result['bytes_deleted'] += before.st_size
            except OSError:
                result['errors'] += 1
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root')
    parser.add_argument('--strict-read-tracking-since', required=True,
                        help='UTC ISO timestamp when strict atime was verified on ALL clients')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--readers-quiesced', action='store_true',
                        help='confirm all clients have paused cache reads and writes')
    args = parser.parse_args()
    try:
        start = datetime.datetime.fromisoformat(args.strict_read_tracking_since.replace('Z', '+00:00'))
        if start.tzinfo is None:
            raise ValueError('tracking start requires a timezone')
        result = sweep(args.root, tracked_since=start.timestamp(), apply=args.apply,
                       readers_quiesced=args.readers_quiesced)
    except (ValueError, OSError):
        parser.exit(2, 'Invalid root, tracking timestamp, or maintenance-window confirmation.\n')
    # Aggregate operational counts only: no filenames, contents, mount credentials,
    # or raw exceptions enter stdout/stderr.
    print(json.dumps(result, sort_keys=True))
    return 1 if result['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
