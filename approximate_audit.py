"""Stratified, bounded-memory sampling of directory entries; no file contents read."""
import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import signal
import stat
import threading
import time

from volume_audit import Limiter, Interrupted

class BudgetExceeded(Exception):
    pass


METRICS = ('files', 'logical', 'allocated', 'directories')


def expanded(values, population):
    """Total and design-based variance for a uniform sample without replacement.

    Each value is (estimated total, variance). The second term carries uncertainty
    from nested samples. A one-item sample of a larger population has unknown variance.
    """
    count = len(values)
    if not population:
        return 0., 0.
    if not count:
        return 0., None
    mean = sum(v[0] for v in values) / count
    total = population * mean
    if any(v[1] is None for v in values) or (count == 1 and population > count):
        return total, None
    between = sum((v[0] - mean) ** 2 for v in values) / (count - 1) if count > 1 else 0.
    variance = population * (population - count) * between / count
    variance += population / count * sum(v[1] for v in values)
    return total, variance


class Sampler:
    def __init__(self, args):
        self.args = args
        self.root = os.path.realpath(args.root)
        self.device = os.stat(self.root).st_dev
        self.stop = threading.Event()
        self.limiter = Limiter(args.rate)
        self.entries = {}
        self.started = time.time()
        self.counters = dict(directories_opened=0, entries_listed=0, file_stats=0)
        self.error_samples = []
        self.excludes = [os.path.normpath(p) for p in args.exclude]
        self.finished = False
        self.root_result = None
        self.budget = None
        self.frames = {}
        self.last_publish = time.monotonic()
        self.budget_exhausted = []

    def tick(self):
        if self.limiter.wait(self.stop):
            raise Interrupted()
        if time.monotonic() - self.last_publish >= min(10, self.args.progress):
            for path, (result, retain) in self.frames.items():
                if retain: self.record(path, result, True)
            self.write()
            self.last_publish = time.monotonic()
        if self.budget:
            starts, deadline = self.budget
            for counter, limit in [('directories_opened', self.args.subtree_directories),
                                   ('entries_listed', self.args.subtree_entries),
                                   ('file_stats', self.args.subtree_stats)]:
                if self.counters[counter] - starts[counter] >= limit:
                    raise BudgetExceeded(counter)
            if time.monotonic() >= deadline:
                raise BudgetExceeded('time')

    def error(self, path, exc):
        if len(self.error_samples) < 100:
            self.error_samples.append(dict(path=os.path.relpath(path, self.root), error=str(exc)))

    def walk(self, path, depth=0, retain=True, directory_budget=None):
        if depth == 1:
            self.budget = (dict(self.counters), time.monotonic() + self.args.subtree_seconds)
            directory_budget = self.args.subtree_directories
        try:
            return self._walk(path, depth, retain, directory_budget)
        except BudgetExceeded as exc:
            if depth != 1: raise
            result = self.frames[path][0]
            result['partial'] = True
            self.budget_exhausted.append(os.path.relpath(path, self.root))
            # Only completed work is retained; do not extrapolate time-truncated samples.
            if retain: self.record(path, result, True)
            return result
        finally:
            if depth == 1:
                self.budget = None
                self.frames = {p:v for p,v in self.frames.items() if p == self.root}

    def _walk(self, path, depth, retain, directory_budget):
        result = dict(values={m: (0., 0.) for m in METRICS}, errors=0, estimated=False,
                      direct_files=0, direct_directories=0, sampled_files=0, sampled_directories=0, partial=False)
        self.frames[path] = (result, retain)
        self.tick()
        result['values']['directories'] = (1., 0.)
        rng = random.Random(hashlib.sha256(os.fsencode(path) + str(self.args.seed).encode()).digest())
        reservoirs = [[], []]  # files, directories
        populations = [0, 0]
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                if os.fstat(fd).st_dev != self.device:
                    result['values']['directories'] = (0., 0.)
                    return result
                self.counters['directories_opened'] += 1
                with os.scandir(fd) as iterator:
                    for entry in iterator:
                        self.tick()
                        self.counters['entries_listed'] += 1
                        child = os.path.join(path, entry.name)
                        relative = os.path.relpath(child, self.root) if self.excludes else ''
                        if any(relative == e or relative.startswith(e + os.sep) for e in self.excludes):
                            continue
                        try:
                            if entry.is_dir(follow_symlinks=False): kind = 1
                            elif entry.is_file(follow_symlinks=False): kind = 0
                            else: continue  # Never follow links; special files excluded.
                        except OSError as exc:
                            result['errors'] += 1; self.error(child, exc); continue
                        populations[kind] += 1
                        reservoir = reservoirs[kind]
                        # Census the root's directories to preserve top-level comparisons.
                        if (depth == 0 and kind == 1) or len(reservoir) < self.args.sample_size:
                            reservoir.append(child)
                        else:
                            index = rng.randrange(populations[kind])
                            if index < self.args.sample_size: reservoir[index] = child
            finally:
                os.close(fd)
        except OSError as exc:
            result['errors'] += 1; self.error(path, exc)
        nf, nd = populations
        size = self.args.sample_size
        if depth == 0:
            kf, kd = min(nf, size), nd
        elif nf + nd <= size:
            kf, kd = nf, nd
        elif nf and nd:
            kf = min(nf, max(1, min(size - 1, round(size * nf / (nf + nd)))))
            kd = min(nd, size - kf)
            kf = min(nf, size - kd)
        else:
            kf, kd = min(nf, size), min(nd, size)
        if directory_budget is not None and nd:
            # Divide the budget before sampling, rather than multiplying it at each level.
            kd = min(kd, max(1, (directory_budget - 1) // 8)) if directory_budget > 1 else 0
            if kd == 0: result['partial'] = True
        if self.budget:
            remaining_stats = self.args.subtree_stats - (self.counters['file_stats'] - self.budget[0]['file_stats'])
            kf = min(kf, remaining_stats)
            if nf and not kf: result['partial'] = True
        selected = [rng.sample(reservoirs[0], kf), rng.sample(reservoirs[1], kd)]
        result.update(direct_files=nf, direct_directories=nd, sampled_files=kf, sampled_directories=kd,
                      estimated=kf < nf or kd < nd)
        file_values = {'logical': [], 'allocated': []}
        for child in selected[0]:
            self.tick()
            try:
                self.counters['file_stats'] += 1
                st = os.stat(child, follow_symlinks=False)
                if not stat.S_ISREG(st.st_mode) or st.st_dev != self.device:
                    raise OSError('Entry changed type or filesystem during sampling')
                file_values['logical'].append((st.st_size, 0.))
                file_values['allocated'].append((st.st_blocks * 512, 0.))
            except OSError as exc:
                result['errors'] += 1; self.error(child, exc)
                for metric in file_values: file_values[metric].append((0., None))
        result['values']['files'] = (float(nf), 0.)
        for metric, values in file_values.items(): result['values'][metric] = expanded(values, nf)
        base_values = dict(result['values'])
        # Reserve parent slots before descendants so the capped tree stays connected.
        if retain:
            self.record(path, result, True)
        # Accumulate only sampled child totals: bounded by sample size below root.
        children = {m: [] for m in METRICS}
        for index, child in enumerate(selected[1]):
            child_budget = None if directory_budget is None else (directory_budget - 1) // kd + int(index < (directory_budget - 1) % kd)
            sub = self.walk(child, depth + 1, retain and kd == nd and depth < 2, child_budget)
            result['partial'] |= sub['partial']
            result['errors'] += sub['errors']
            result['estimated'] |= sub['estimated']
            for metric in METRICS:
                children[metric].append(sub['values'][metric])
                # Missing selected siblings stay unknown, rather than inflating an early subset.
                values = children[metric] + [(0., None)] * (kd - len(children[metric])) if depth else children[metric]
                value, variance = expanded(values, nd if depth else len(children[metric]))
                direct, direct_variance = base_values[metric]
                result['values'][metric] = (direct + value, None if variance is None or direct_variance is None else variance + direct_variance)
            if retain: self.record(path, result, True)
            if depth == 0:
                self.root_result = result
                self.record(path, result, True)
                self.write()
                print(json.dumps({'completed_top_level': os.path.basename(child), **self.counters}), flush=True)
        if retain: self.record(path, result, depth == 0 and not self.finished)
        self.frames.pop(path, None)
        return result

    def record(self, path, result, partial=False):
        relative = os.path.relpath(path, self.root)
        partial = partial or result.get('partial', False)
        entry = dict(path=relative, parent=None if relative == '.' else os.path.dirname(relative) or '.',
                     errors=result['errors'], complete=not partial and not result['estimated'] and not result['errors'],
                     estimated=result['estimated'], coverage_complete=not partial,
                     **{k: result[k] for k in ('direct_files','direct_directories','sampled_files','sampled_directories')})
        for metric, (value, variance) in result['values'].items():
            entry[metric] = round(value)
            entry[metric + '_margin95'] = None if variance is None or result['errors'] or partial else round(1.96 * math.sqrt(max(0., variance)))
        if relative != '.' and entry['parent'] not in self.entries: return
        if len(self.entries) < 12000 or relative in self.entries: self.entries[relative] = entry

    def write(self, status=None):
        now = datetime.datetime.now(datetime.timezone.utc).isoformat()
        data = dict(cluster=self.args.cluster, root=self.root, filesystem=self.args.filesystem,
                    method='approximate', complete=False, coverage_complete=self.finished and self.entries.get('.', {}).get('coverage_complete', False),
                    status=status or ('Sampling pass finished — partial estimates' if self.finished and not self.entries.get('.', {}).get('coverage_complete', False) else 'Approximate scan complete' if self.finished else 'Sampling — partial estimates'),
                    started_at=datetime.datetime.fromtimestamp(self.started, datetime.timezone.utc).isoformat(),
                    updated_at=now, entries=sorted(self.entries.values(), key=lambda e: (e['path'] != '.', e['path'])),
                    sample_size=self.args.sample_size, seed=self.args.seed, counters=self.counters,
                    error_samples=self.error_samples, budget_exhausted=self.budget_exhausted, budget_limits=dict(directories=self.args.subtree_directories, file_stats=self.args.subtree_stats, entries=self.args.subtree_entries, seconds=self.args.subtree_seconds), truncated=True, max_depth=2,
                    uncertainty_note='Approximate 95% sampling margins, not guarantees. Rare large files may be missed; zero sample variation does not prove identical contents. File counts below unsampled folders are estimates. Symlinks and special files excluded.')
        output = Path(self.args.output)
        with open(str(output) + '.tmp', 'w') as f: json.dump(data, f)
        os.replace(str(output) + '.tmp', output)


def run(args):
    if args.sample_size < 2: raise ValueError('--sample-size must be at least 2')
    if args.resume or args.shards != 1 or args.shard_index != 0 or args.include_directory_blocks:
        raise ValueError('Approximate mode does not support resume, sharding, or directory-block accounting')
    if args.db or not args.output: raise ValueError('Use --output, without --db, for an approximate scan; exact databases remain separate')
    root = os.path.realpath(args.root)
    output = os.path.realpath(args.output)
    if os.path.commonpath([root, output]) == root: raise ValueError('Output must be outside the scanned tree')
    if os.path.exists(output): raise ValueError('Output already exists; choose a new approximate scan output')
    for exclude in args.exclude:
        if os.path.isabs(exclude) or os.path.normpath(exclude).startswith('..'):
            raise ValueError('Exclusions must be root-relative paths within the scan')
    sampler = Sampler(args)
    previous = {s: signal.signal(s, lambda *_: sampler.stop.set()) for s in (signal.SIGINT, signal.SIGTERM)}
    try:
        result = sampler.walk(sampler.root)
        sampler.finished = True
        sampler.record(sampler.root, result)
        sampler.write('Approximate scan finished with errors' if result['errors'] else None)
        return 2 if result['errors'] else 0
    except Interrupted:
        sampler.write('Sampling paused — partial estimates')
        return 130
    finally:
        for sig, handler in previous.items(): signal.signal(sig, handler)
