#!/usr/bin/env python3
"""Merge complete, compatible shard JSON reports without touching the volume."""
import argparse
import collections
import json
import sys


def merge(reports, limit):
    if not reports:
        raise ValueError('No reports')
    first = reports[0]['config']
    keys = ('version', 'root', 'cluster', 'scan_id', 'shards', 'top', 'exclude', 'include_directory_blocks')
    if not first['cluster'] or not first['scan_id']:
        raise ValueError('Reports must have --cluster and --scan-id')
    if limit > first['top']:
        raise ValueError('Requested limit exceeds the scan --top')
    seen = set()
    totals = collections.Counter()
    groups = collections.defaultdict(collections.Counter)
    largest = []
    for report in reports:
        conf = report['config']
        if any(conf[k] != first[k] for k in keys):
            raise ValueError('Incompatible cluster, scan ID, root, or scan options')
        index = conf['shard_index']
        if index in seen:
            raise ValueError('Duplicate shard index')
        if not report['complete']:
            raise ValueError('A shard is unfinished or has errors; inspect its report')
        if len(report['largest_files']) < min(limit, report['totals']['files']):
            raise ValueError('Export each shard with report --limit at least the merge limit')
        seen.add(index)
        totals.update(report['totals'])
        largest.extend(report['largest_files'])
        for g in report['groups']:
            groups[g['kind'], g['bucket']].update({k: g[k] for k in ('files', 'logical', 'allocated')})
    if seen != set(range(first['shards'])):
        raise ValueError('Missing shard indices')
    return dict(cluster=first['cluster'], scan_id=first['scan_id'], root=first['root'],
                shards=first['shards'], complete=True, totals=dict(totals),
                accounting=reports[0]['accounting'],
                groups=[dict(kind=k, bucket=b, **v) for (k, b), v in sorted(groups.items())],
                largest_files=sorted(largest, key=lambda f: (f['logical'], f['path']), reverse=True)[:limit],
                note='Live scans are not a snapshot. Directory drilldown remains in each shard database; age buckets use each shard start time.')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('reports', nargs='+')
    p.add_argument('--limit', type=int, default=30)
    args = p.parse_args()
    try:
        if args.limit < 1:
            raise ValueError('limit must be positive')
        reports = []
        for path in args.reports:
            with open(path) as f:
                reports.append(json.load(f))
        print(json.dumps(merge(reports, args.limit), indent=2))
    except (OSError, ValueError, KeyError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
