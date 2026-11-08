"""Recover a frozen catalog, without changing or initializing any replay ledger.

The reviewed release is a second copy of public market metadata. Its file and
semantic hashes are pinned in the campaign; it is never a substitute for shards.
"""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import zipfile

from recover_sports_shard import command, read_checkpoint, validate_catalog, REPOSITORY, WORKFLOW, MAX_PACKED


def restore(run_id, directory, campaign, execute=command):
    if type(run_id) is not int or run_id < campaign['root_run_id']:
        raise ValueError('UNTRUSTED_CATALOG_RUN')
    run = json.loads(execute(['gh', 'api', f'repos/{REPOSITORY}/actions/runs/{run_id}']))
    if (run.get('id') != run_id or run.get('repository', {}).get('full_name') != REPOSITORY
            or run.get('head_branch') != 'main' or run.get('path') != WORKFLOW
            or run.get('event') != 'workflow_dispatch'):
        raise ValueError('UNTRUSTED_CATALOG_RUN')
    listing = json.loads(execute(['gh', 'api', f'repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100']))
    artifacts = listing['artifacts']
    if listing.get('total_count', len(artifacts)) > len(artifacts):
        raise ValueError('CATALOG_DISCOVERY_INCOMPLETE')
    candidates = [a for a in artifacts if a['name'] == 'sports-month-catalog' and not a['expired']]
    if len(candidates) > 1:
        raise ValueError('DUPLICATE_CATALOG_ARTIFACT')
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory/'catalog.json.gz'
    if destination.exists():
        raise ValueError('REFUSING_CATALOG_OVERWRITE')
    with tempfile.TemporaryDirectory(prefix='catalog-recovery-', dir=directory) as temporary:
        root = Path(temporary)
        source = root/'catalog.json.gz'
        if candidates:
            artifact = candidates[0]
            if artifact.get('size_in_bytes', 0) > MAX_PACKED:
                raise ValueError('CATALOG_ARCHIVE_TOO_LARGE')
            raw = execute(['gh', 'api', f"repos/{REPOSITORY}/actions/artifacts/{int(artifact['id'])}/zip"])
            if len(raw) > MAX_PACKED or (artifact.get('digest') and artifact['digest'] != 'sha256:'+hashlib.sha256(raw).hexdigest()):
                raise ValueError('CATALOG_ARCHIVE_INVALID')
            with zipfile.ZipFile(io.BytesIO(raw)) as zipped:
                if zipped.namelist().count('catalog.json.gz') != 1 or zipped.getinfo('catalog.json.gz').file_size > MAX_PACKED:
                    raise ValueError('INVALID_CATALOG_MEMBERS')
                source.write_bytes(zipped.read('catalog.json.gz'))
            origin = {'kind': 'actions_artifact', 'run_id': run_id, 'artifact_id': artifact['id']}
        else:
            recovery = campaign['catalog_recovery']
            execute(['gh', 'release', 'download', recovery['tag'], '--repo', REPOSITORY,
                     '--pattern', recovery['filename'], '--dir', str(root)])
            source = root/recovery['filename']
            if source.stat().st_size > MAX_PACKED or hashlib.sha256(source.read_bytes()).hexdigest() != recovery['sha256']:
                raise ValueError('CATALOG_RECOVERY_DIGEST_MISMATCH')
            frozen = read_checkpoint(source)
            validate_catalog(frozen, campaign)
            if frozen['catalog_hash'] != recovery['catalog_hash']:
                raise ValueError('CATALOG_RECOVERY_CONTENT_CHANGED')
            origin = {'kind': 'reviewed_release_backup', 'tag': recovery['tag'], 'missing_from_run': run_id}
        catalog = read_checkpoint(source)
        if (catalog.get('schema') != 'quantura-sports-month-v1'
                or any(catalog.get(k) != campaign[k] for k in ('first', 'last', 'runtime_fingerprint'))):
            raise ValueError('CATALOG_POLICY_OR_DATES_CHANGED')
        if catalog.get('done'):
            validate_catalog(catalog, campaign)
        receipt = {**origin, 'schema': 'quantura-sports-catalog-recovery-v1',
                   'sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
                   'catalog_hash': catalog.get('catalog_hash'), 'contracts': len(catalog['contracts'])}
        os.replace(source, destination)
        (directory/'catalog-recovery.json').write_text(json.dumps(receipt, indent=2)+'\n')
        print(json.dumps(receipt))
        return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', required=True, type=int)
    parser.add_argument('--directory', required=True, type=Path)
    parser.add_argument('--campaign', required=True, type=Path)
    parser.add_argument('--first', required=True)
    parser.add_argument('--last', required=True)
    parser.add_argument('--code-sha', required=True)
    args = parser.parse_args()
    campaign = json.loads(args.campaign.read_text())
    if (args.first, args.last, args.code_sha) != (campaign['first'], campaign['last'], campaign['code_sha']):
        raise SystemExit('CATALOG_RECOVERY_REQUIRES_APPROVED_CAMPAIGN')
    restore(args.run, args.directory, campaign)
