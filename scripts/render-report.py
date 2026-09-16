#!/usr/bin/env python3
"""Turn a JSON audit report into a self-contained, offline HTML report."""
import argparse
import html
import json


def size(value):
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB', 'PiB'):
        if abs(value) < 1024 or unit == 'PiB':
            return f'{value:,.2f} {unit}'
        value /= 1024


def escape(value):
    # JSON escaping also makes filenames containing newlines/control bytes legible.
    return html.escape(json.dumps(value, ensure_ascii=True)[1:-1] if isinstance(value, str) else str(value))


def table(title, rows, name):
    body = ''.join('<tr><td>' + escape(row[name]) + '</td>' + ''.join(
        '<td>' + escape(size(row.get(k, 0)) if k != 'files' else f'{row.get(k, 0):,}') + '</td>'
        for k in ('files', 'logical', 'allocated')) + '</tr>' for row in rows)
    return f'<h2>{escape(title)}</h2><table><tr><th>Name / path</th><th>Files</th><th>Logical</th><th>Allocated</th></tr>{body}</table>'


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('report')
    p.add_argument('--output', required=True)
    args = p.parse_args()
    with open(args.report) as f:
        report = json.load(f)
    totals = report['totals']
    content = '<!doctype html><html lang="en"><meta charset="utf-8"><title>Volume audit</title><style>' \
        'body{font:16px system-ui;max-width:1200px;margin:40px auto;padding:0 24px;color:#152536;background:#f7f9fb}' \
        'h1{margin-bottom:8px}table{border-collapse:collapse;width:100%;background:white}' \
        'td,th{padding:10px;border-bottom:1px solid #dde3e9;text-align:right}' \
        'td:first-child,th:first-child{text-align:left;overflow-wrap:anywhere}td:first-child{font-family:monospace}' \
        '.cards{display:flex;gap:16px;flex-wrap:wrap}.card{background:#fff;padding:20px;min-width:180px;border:1px solid #dde3e9}' \
        '.card strong{display:block;font-size:28px}p{line-height:1.5}.status{font-weight:bold}</style><h1>Volume audit</h1>'
    content += '<p>' + escape(report.get('cluster') or report.get('config', {}).get('cluster', '')) + ' · ' + escape(report['root']) + '</p>'
    content += '<p class="status">' + ('Scan complete within configured scope' if report['complete'] else 'INCOMPLETE / ERRORS — totals are partial') + '</p><div class="cards">'
    for label, value in [('Regular files', f"{totals['files']:,}"), ('Logical size', size(totals['logical'])),
                         ('Allocated blocks', size(totals['allocated'])), ('Errors', totals['errors'])]:
        content += f'<div class="card">{escape(label)}<strong>{escape(value)}</strong></div>'
    content += '</div><p>' + escape(report['accounting']) + '</p>'
    content += '<p>Hard-linked paths: ' + str(totals['hardlink_paths']) + '; excluded entries: ' + str(totals['excluded']) + '. Live scan; not a point-in-time snapshot. Age is modification time, not last use.</p>'
    for key, title in [('children', 'Immediate child directories'), ('largest_directories', 'Largest recursive directories (overlap; do not sum)'), ('largest_files', 'Largest files')]:
        if key in report:
            content += table(title, report[key], 'path')
    for kind, title in [('type', 'File categories'), ('mtime_age', 'File modification age (exclusive buckets)')]:
        content += table(title, [g for g in report['groups'] if g['kind'] == kind], 'bucket')
    if report.get('error_samples'):
        content += '<h2>Error samples</h2><pre>' + html.escape(json.dumps(report['error_samples'], indent=2)) + '</pre>'
    content += '</html>'
    with open(args.output, 'w') as f:
        f.write(content)


if __name__ == '__main__':
    main()
