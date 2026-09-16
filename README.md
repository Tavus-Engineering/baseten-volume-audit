# Baseten volume audit

Find where a very large shared volume is being used, starting with native JuiceFS statistics and using a resumable metadata scan when you need file-level detail. Designed for `/root/.cache/team_artifacts` on live Baseten training machines. H100, H200, and RTX clusters can expose **different volumes at that same path**: audit each independently and label the actual cluster/region, not just the GPU family.

Python 3.9+ on Linux, standard library only. No pip install, root-only dependencies, file-content reads, hashing of file contents, deletion, or filesystem configuration changes. The scanner lists directories and reads inode metadata. It still generates metadata traffic and directory reads may update access times according to mount policy.

## Start here: inexpensive checks, then native JuiceFS summary

```bash
git clone https://github.com/Tavus-Engineering/baseten-volume-audit.git
cd baseten-volume-audit
./scripts/preflight.sh /root/.cache/team_artifacts
./scripts/quick-summary.sh /root/.cache/team_artifacts 2 30
```

`preflight` reports mount type, capacity, inode capacity, and Baseten cache environment paths without recursively traversing the volume. The second script invokes the installed `juicefs summary` with output depth 2 and the top 30 entries. It does not install a client, change settings, or enable strict traversal. Use the JuiceFS client compatible with the mounted volume.

**Try native summary first.** Community directory statistics can avoid individual file stats, but recursive summary still visits directories and may be costly. Output depth limits the display, not necessarily traversal. Enterprise supports recursive directory statistics, which can be substantially faster. Cached/asynchronous statistics may lag writes. See [JuiceFS directory statistics](https://juicefs.com/docs/community/guide/dir-stats/), the [Community command reference](https://juicefs.com/docs/community/command_reference/), and [Enterprise capacity accounting](https://juicefs.com/docs/cloud/guide/quota/). If the CLI is missing, use the scanner below; there is no automatic full-scan fallback.

## Detailed scan on a live machine

Keep the database on **local SSD/scratch outside the target tree**, not on JuiceFS, NFS, or another shared filesystem. SQLite WAL needs local locking semantics. Verify `/tmp` has sufficient capacity; choose another local disk if needed.

```bash
mkdir -p /tmp/volume-audits
./scripts/scan.sh /root/.cache/team_artifacts /tmp/volume-audits/h200.db \
  --cluster h200-us-east --workers 2 --rate 1000
```

The wrapper lowers CPU/I/O scheduling priority and defaults to 1,000 directory entries/second across its workers. Override `--rate` to tune. `ionice` does not control remote metadata-server load. Start by scanning a representative subtree while watching training throughput and metadata latency, then increase to 4/8 workers or a higher rate if the workload tolerates it. More workers are not always faster. The Python command itself has no rate ceiling unless `--rate` is supplied.

There is no credible universal runtime estimate from 500 TB alone: inode count and metadata latency dominate. At 1,000 entries/second, one billion entries takes at least 11.6 days, before other overhead. Native directory summaries may save a full traversal.

Progress goes to stderr every 30 seconds; `--progress 10` changes that. Counters include committed directories only. One enormous flat directory uses one worker and its file counters become visible when it completes.

To stop, press Ctrl-C or send SIGTERM. Resume with the same command plus `--resume`:

```bash
./scripts/scan.sh /root/.cache/team_artifacts /tmp/volume-audits/h200.db \
  --cluster h200-us-east --workers 2 --rate 1000 --resume
```

Worker count and rate may change on resume. Root identity, cluster label, shard settings, exclusions, `--top`, and accounting options must match. Completed directories are reused; unfinished directories restart from their beginning, including after SIGKILL. A blocked filesystem syscall can delay shutdown. A completed scan is not refreshed by `--resume`; use a new database for a fresh inventory. Errored directories are recorded as finished-with-errors and require a fresh scan after fixing access.

## Reports and drilldown

```bash
# Live report: no additional volume traversal.
python3 volume_audit.py report --db /tmp/volume-audits/h200.db > /tmp/h200-progress.json

# After stopping or finishing: build recursive totals from the local DB.
python3 volume_audit.py rollup --db /tmp/volume-audits/h200.db
python3 volume_audit.py report --db /tmp/volume-audits/h200.db --limit 100 > /tmp/h200.json
python3 scripts/render-report.py /tmp/h200.json --output /tmp/h200.html

# Compare children of a specific directory. Sort by bytes or file count.
python3 volume_audit.py report --db /tmp/volume-audits/h200.db \
  --path datasets --sort files --limit 50
```

Open the HTML locally for a summary, largest directories/files, file categories, and modification-age distribution. All units in JSON are bytes; HTML uses binary units. HTML is self-contained with no external services. Filenames are escaped, including control characters. Reports can contain private paths: keep generated databases/JSON/HTML out of this public repository.

The scan retains the largest 100 regular files by logical size (`--top` changes this) and every directory's direct totals. Categories use a fixed extension mapping; unknown extensions are `other`. Age buckets are mutually exclusive: `<7d`, `7–30d` (label `<30d`), `30–90d`, `90–180d`, `180–365d`, `>=365d`, and future timestamps. Age is relative to scan start. **Old modification time is not evidence a file is unused or safe to delete.**

## Optional: multiple machines on the same volume

Use `--shards N --shard-index I`. Every machine must see the **same cluster volume and root**, use the same commit/options and shared `--scan-id`, and run exactly one distinct index from `0` to `N-1`. This repository provides workers, not SSH orchestration or provisioning.

Example, two machines on one H200 cluster:

```bash
# Machine A
./scripts/scan.sh /root/.cache/team_artifacts /tmp/h200-0.db \
  --cluster h200-us-east --scan-id audit-2026-09-16 \
  --shards 2 --shard-index 0 --workers 2 --rate 500

# Machine B: same command, different DB/index
./scripts/scan.sh /root/.cache/team_artifacts /tmp/h200-1.db \
  --cluster h200-us-east --scan-id audit-2026-09-16 \
  --shards 2 --shard-index 1 --workers 2 --rate 500
```

Top-level entry names are assigned by a stable SHA-256 hash. Each subtree (and root-level file) belongs to one shard; all shards enumerate the root. There is no shared SQLite database. Sharding can be uneven if one subtree dominates, and cannot speed up one huge flat directory: target separate subtrees manually in that case. Renames during scanning can lead to omissions or duplicate observations, as with any live walk. Run shards close together; there is no snapshot coordination.

`--rate` is per process: two processes at 500 each allow roughly 1,000 entries/second in total. More machines can saturate the same metadata service sooner. Do not combine H100, H200, and RTX inventories just because their paths match.

On each machine export a report with `--limit` at least the desired merged limit (and no greater than scan `--top`):

```bash
python3 volume_audit.py report --db /tmp/h200-0.db --limit 100 > /tmp/h200-0.json
# On Machine B: equivalent command for h200-1.db / h200-1.json.
# Copy JSON files to one machine, then:
python3 scripts/merge-reports.py /tmp/h200-0.json /tmp/h200-1.json --limit 100 > /tmp/h200-all.json
python3 scripts/render-report.py /tmp/h200-all.json --output /tmp/h200-all.html
```

Merge rejects missing/duplicate indices, incomplete/error reports, incompatible options, or differing cluster/run labels. Those labels are user assertions: matching labels do not prove the mounts expose the same volume. Merged reports include totals, categories/age, and largest files; directory drilldown remains in individual databases. Age cutoffs use each shard's start time. Local device numbers are deliberately not compared across machines.

## Accounting and scope

- **Logical bytes:** regular-file `st_size`. Sparse files may have much smaller allocated size.
- **Allocated bytes:** non-directory `st_blocks × 512`. Directory blocks are excluded by default because JuiceFS Enterprise can report recursive sizes. For a traditional POSIX filesystem only, opt into `--include-directory-blocks` (single-machine scans only).
- These are namespace/path totals, **not unique backend object usage or reclaimable bytes**. Hard links are counted per path and counted separately in `hardlink_paths`. Reflinks, compression, object-store garbage, trash, snapshots, retained versions, deleted-open files, and reserved space can make `df` or billed usage differ.
- Symlinks are counted but not followed; special files are counted without being opened. Different `st_dev` entries are excluded. Same-device bind mounts are not detected: explicitly exclude these to avoid duplicate traversal/cycles. Directory ancestors can change during a live scan; this is not a sandbox for adversarially changing namespaces.
- `--exclude datasets/scratch --exclude cache/tmp` omits exact root-relative subtrees. No glob expansion. Exclusions apply before stat. Excluded entries are counted, but their unknown descendant sizes are not estimated. `complete: true` means no observed errors **within configured scope**, not proof of an atomic snapshot or absence of excluded data.
- Permission, disappearing-file, and directory-read errors remain visible. Reports show partial totals and a bounded error sample (up to 100 per directory / 10,000 globally). All error counts are retained.
- Exit codes: `0` success, `2` traversal finished with errors, `130` interrupted, `1` invalid configuration or operational failure. A scan returning `2` still has a usable partial database; run rollup/report separately.

## Scaling and recovery design

Streaming `os.scandir`, bounded batches of 1,000 discovered directories, a fixed-size largest-file heap per worker, fixed category/age counters, and an SQLite directory queue keep Python memory independent of file count. SQLite caches are capped per connection; temporary SQL work spills to disk. Disk space scales with directory count/path lengths, not a row for every regular file. Allow space for indexes, WAL, and a second per-directory rollup table. Millions of directories can still require substantial local disk.

Children are persisted in batches but become eligible only when their parent commits its final metrics. Parent completion, global aggregates, and largest-file updates share a transaction. Recovery removes staged children of unfinished parents before replaying those parents. Scan/rollup use an exclusive process lock; reports use consistent read snapshots. SQLite writes are serialized; very small directories can become local-database bound. One busy directory is never loaded into a list.

Rollup works from deepest level to root using the local database and can be rerun. Nested directory totals overlap; do not sum the “largest directories” list. No scanner is a point-in-time inventory of a changing volume. For exact reconciliation use a quiescent volume or filesystem snapshot.

## Development

```bash
python3 -m unittest discover -s tests -v
```

CI covers Python 3.9, 3.12, and 3.14 on Linux. Tests cover sparse files, hard links, symlink cycles, special files, unusual filenames, errors, exclusions, process locking, interruption, crash recovery with staged children, sharding, and merge validation. Linux exercises non-UTF-8 filenames; macOS uses Unicode because APFS rejects arbitrary byte names. This repository has not been benchmarked against a 500 TB production volume. Tune using a representative subtree before a full run.
