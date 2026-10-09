#!/usr/bin/env python3
"""Add directory-only metadata to an existing snapshot without listing its files."""
import argparse
import ctypes
import datetime
import functools
import json
import os
import pwd
import grp
import stat
import struct
import sys
import time


def timestamp(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()


def birth_time(fd, st):
    if hasattr(st, 'st_birthtime'):
        return timestamp(st.st_birthtime)
    if sys.platform != 'linux':
        return None
    # Linux statx ABI: 256-byte structure; stx_btime starts at byte 80.
    # Query an already-open descriptor, avoiding path replacement races.
    libc = ctypes.CDLL(None, use_errno=True)
    if not hasattr(libc, 'statx'):
        return None
    buf = ctypes.create_string_buffer(256)
    if libc.statx(fd, ctypes.c_char_p(b''), 0x1000 | 0x100, 0x800, buf) != 0:
        return None
    if not struct.unpack_from('=I', buf.raw)[0] & 0x800:
        return None
    seconds, nanos = struct.unpack_from('=qI', buf.raw, 80)
    return timestamp(seconds + nanos / 1e9)


@functools.lru_cache(maxsize=1024)
def owner_name(uid):
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return None


@functools.lru_cache(maxsize=1024)
def group_name(gid):
    try:
        return grp.getgrgid(gid).gr_name
    except KeyError:
        return None


def directory_fd(root_fd, relative):
    parts = [] if relative == '.' else relative.split('/')
    if any(p in ('', '.', '..') for p in parts) or relative.startswith('/'):
        raise ValueError('Unsafe relative directory path')
    fd = os.dup(root_fd)
    try:
        for part in parts:
            child = os.open(part, getattr(os, 'O_PATH', os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def enrich(snapshot, expected_inode=None, rate=200):
    root = snapshot['root']
    if not os.path.isabs(root):
        raise ValueError('Snapshot root must be absolute')
    fd = os.open(root, getattr(os, 'O_PATH', os.O_RDONLY) | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        identity = os.fstat(fd)
        if expected_inode is not None and identity.st_ino != expected_inode:
            raise ValueError('Root inode mismatch; refusing metadata from a different volume')
        for entry in snapshot['entries']:
            started = time.monotonic()
            observed = timestamp(time.time())
            try:
                child = directory_fd(fd, entry['path'])
                try:
                    st = os.fstat(child)
                    if st.st_dev != identity.st_dev or not stat.S_ISDIR(st.st_mode):
                        raise ValueError('Not a directory on the selected filesystem')
                    entry['metadata'] = dict(uid=st.st_uid, gid=st.st_gid,
                        owner=owner_name(st.st_uid), group=group_name(st.st_gid),
                        created_at=birth_time(child, st), modified_at=timestamp(st.st_mtime),
                        accessed_at=timestamp(st.st_atime), observed_at=observed)
                finally:
                    os.close(child)
            except (OSError, ValueError) as exc:
                entry['metadata'] = dict(observed_at=observed, error=str(exc))
            time.sleep(max(0, 1 / rate - (time.monotonic() - started)))
        snapshot['metadata_updated_at'] = timestamp(time.time())
        snapshot['metadata_scope'] = 'Directory inode only; not recursive activity. OS ownership is not teammate attribution. Access times may be stale or affected by scans. Birth time is null when unsupported. Collected separately from size inventory.'
        return snapshot
    finally:
        os.close(fd)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot', required=True)
    p.add_argument('--output', required=True)
    p.add_argument('--expected-root-inode', type=int)
    p.add_argument('--rate', type=int, default=200, help='Maximum directory metadata probes per second')
    args = p.parse_args()
    if args.rate < 1:
        p.error('--rate must be positive')
    with open(args.snapshot) as f:
        data = json.load(f)
    result = enrich(data, args.expected_root_inode, args.rate)
    with open(args.output + '.tmp', 'w') as f:
        json.dump(result, f)
    os.replace(args.output + '.tmp', args.output)
    print(json.dumps({'entries': len(result['entries']), 'metadata_errors': sum('error' in e['metadata'] for e in result['entries']), 'metadata_updated_at': result['metadata_updated_at']}))


if __name__ == '__main__':
    main()
