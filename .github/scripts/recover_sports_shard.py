"""Restore explicitly selected, same-campaign shard receipts; never reinitialize.

This controller is checked out at the workflow revision separately from the
frozen forecasting code. It does not change that code or select trading rules.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile

REPOSITORY = 'tamzid2001/quantura-forecast-workers'
WORKFLOW = '.github/workflows/sports-month-backtest.yml'
SCHEMA = 'quantura-sports-month-v1'
MAX_PACKED = 64*1024*1024
MAX_JSON = 512*1024*1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def read_checkpoint(path):
    if path.stat().st_size > MAX_PACKED:
        raise ValueError('CHECKPOINT_ARCHIVE_TOO_LARGE')
    with gzip.open(path, 'rb') as stream:
        data = stream.read(MAX_JSON+1)
    if len(data) > MAX_JSON:
        raise ValueError('CHECKPOINT_JSON_TOO_LARGE')
    return json.loads(data)


def validate_catalog(catalog, campaign):
    if (catalog.get('schema') != SCHEMA or catalog.get('done') is not True
            or any(catalog.get(k) != campaign[k] for k in ('first', 'last', 'runtime_fingerprint'))
            or catalog.get('catalog_hash') != digest(catalog.get('contracts'))):
        raise ValueError('RECOVERY_CATALOG_MISMATCH')


def validate_shard(shard, number, catalog):
    items = shard.get('items')
    if (shard.get('schema') != SCHEMA or type(shard.get('shard')) is not int
            or shard['shard'] != number or shard.get('catalog_hash') != catalog['catalog_hash']
            or not isinstance(items, dict)):
        raise ValueError('RECOVERY_SHARD_MISMATCH')
    for key, item in items.items():
        if (key not in catalog['contracts'] or int(hashlib.sha256(key.encode()).hexdigest(), 16) % 4 != number
                or not isinstance(item, dict) or item.get('status') not in {'complete', 'excluded', 'forecasted', 'retry'}):
            raise ValueError('RECOVERY_ITEM_MISMATCH')


def validate_run(run, campaign):
    if (run.get('repository', {}).get('full_name') != REPOSITORY or run.get('head_branch') != 'main'
            or run.get('path') != WORKFLOW or run.get('event') != 'workflow_dispatch'
            or run.get('status') != 'completed' or int(run['id']) < campaign['root_run_id']):
        raise ValueError('UNTRUSTED_RECOVERY_RUN')


def sources(value, default):
    data = json.loads(value)
    if not isinstance(data, dict) or set(data)-{'0', '1', '2', '3'}:
        raise ValueError('INVALID_RECOVERY_SOURCES')
    if type(default) is not int or default <= 0 or any(type(v) is not int or v <= 0 for v in data.values()):
        raise ValueError('INVALID_RECOVERY_RUN_ID')
    return {str(i): data.get(str(i), default) for i in range(4)}


def command(arguments):
    return subprocess.run(arguments, check=True, capture_output=True, timeout=180).stdout


def restore(number, chosen, base, directory, campaign, execute=command):
    if number not in range(4) or chosen > base:
        raise ValueError('INVALID_RECOVERY_ORDER')
    with tempfile.TemporaryDirectory(prefix='sports-recovery-') as temporary:
        root = Path(temporary)
        catalogs = []
        for role, run_id in [('base', base), ('source', chosen)]:
            run = json.loads(execute(['gh', 'api', f'repos/{REPOSITORY}/actions/runs/{run_id}']))
            validate_run(run, campaign)
            target = root/role
            execute(['gh', 'run', 'download', str(run_id), '--repo', REPOSITORY,
                     '--name', 'sports-month-catalog', '--dir', str(target)])
            catalog = read_checkpoint(target/'catalog.json.gz')
            validate_catalog(catalog, campaign)
            catalogs.append(catalog)
        if catalogs[0]['catalog_hash'] != catalogs[1]['catalog_hash']:
            raise ValueError('FROZEN_CATALOG_CHANGED')
        target = root/'shard'
        execute(['gh', 'run', 'download', str(chosen), '--repo', REPOSITORY,
                 '--name', f'sports-month-shard-{number}', '--dir', str(target)])
        filename = f'shard-{number}.json.gz'
        checkpoint = read_checkpoint(target/filename)
        validate_shard(checkpoint, number, catalogs[0])
        directory.mkdir(parents=True, exist_ok=True)
        if (directory/filename).exists():
            raise ValueError('REFUSING_CHECKPOINT_OVERWRITE')
        os.replace(target/filename, directory/filename)
        receipt = {'schema': 'quantura-sports-shard-recovery-v1', 'shard': number,
                   'catalog_run_id': base, 'checkpoint_run_id': chosen,
                   'catalog_hash': catalogs[0]['catalog_hash'], 'items': len(checkpoint['items']),
                   'checkpoint_sha256': hashlib.sha256((directory/filename).read_bytes()).hexdigest()}
        (directory/f'recovery-{number}.json').write_text(json.dumps(receipt, indent=2)+'\n')
        print(json.dumps(receipt))
        return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', required=True)
    parser.add_argument('--resume-run', required=True, type=int)
    parser.add_argument('--shard', required=True, type=int)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--campaign', type=Path, default=Path('market_research/sports_month_campaign.json'))
    args = parser.parse_args()
    runs = sources(args.sources, args.resume_run)
    restore(args.shard, runs[str(args.shard)], args.resume_run, args.directory,
            json.loads(args.campaign.read_text()))
