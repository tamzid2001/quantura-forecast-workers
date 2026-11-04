import copy
import gzip
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('recovery', Path(__file__).parents[2]/'.github/scripts/recover_sports_shard.py')
recovery = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recovery)
CAMPAIGN = {'first': '2026-09-20', 'last': '2026-10-19', 'runtime_fingerprint': 'b'*64, 'root_run_id': 100}
KEY = 'kalshi:example'
NUMBER = int(recovery.hashlib.sha256(KEY.encode()).hexdigest(), 16) % 4
CATALOG = {**CAMPAIGN, 'schema': recovery.SCHEMA, 'done': True, 'contracts': {KEY: {'provider': 'kalshi'}}}
CATALOG['catalog_hash'] = recovery.digest(CATALOG['contracts'])
SHARD = {'schema': recovery.SCHEMA, 'shard': NUMBER, 'catalog_hash': CATALOG['catalog_hash'],
         'items': {KEY: {'status': 'complete'}}}


@pytest.mark.parametrize('value', ['[]', '{"4":101}', '{"0":true}', '{"0":"101"}', '{"0":-1}'])
def test_invalid_source_map(value):
    with pytest.raises(ValueError):
        recovery.sources(value, 102)


def test_override_only_one_shard_keeps_other_latest_sources():
    assert recovery.sources('{"3":101}', 102) == {'0':102, '1':102, '2':102, '3':101}


@pytest.mark.parametrize('field,value', [('first','2026-09-19'), ('runtime_fingerprint','c'*64),
                                         ('done',False), ('catalog_hash','forged')])
def test_mismatched_catalog_refused(field, value):
    with pytest.raises(ValueError, match='CATALOG_MISMATCH'):
        recovery.validate_catalog({**CATALOG, field:value}, CAMPAIGN)


def test_catalog_hash_recomputed_over_actual_contents():
    changed = copy.deepcopy(CATALOG)
    changed['contracts'][KEY]['provider'] = 'changed'
    with pytest.raises(ValueError, match='CATALOG_MISMATCH'):
        recovery.validate_catalog(changed, CAMPAIGN)


def test_wrong_shard_or_outside_catalog_item_rejected():
    for shard in [{**SHARD, 'shard': (NUMBER+1)%4}, {**SHARD, 'items': {'unknown': {'status':'complete'}}}]:
        with pytest.raises(ValueError, match='RECOVERY_'):
            recovery.validate_shard(shard, NUMBER, CATALOG)


def test_bounded_decompression(tmp_path, monkeypatch):
    path = tmp_path/'checkpoint.gz'
    path.write_bytes(gzip.compress(b' ' * 1024))
    monkeypatch.setattr(recovery, 'MAX_JSON', 100)
    with pytest.raises(ValueError, match='JSON_TOO_LARGE'):
        recovery.read_checkpoint(path)


def test_restore_uses_verified_run_metadata_and_publishes_receipt(tmp_path):
    calls = []
    def execute(args):
        calls.append(args)
        if args[1] == 'api':
            return json.dumps({'id':int(args[-1].split('/')[-1]), 'status':'completed', 'head_branch':'main',
                               'repository':{'full_name':recovery.REPOSITORY}, 'path':recovery.WORKFLOW,
                               'event':'workflow_dispatch'}).encode()
        directory = Path(args[-1]); directory.mkdir()
        name = args[args.index('--name')+1]
        file, value = ('catalog.json.gz', CATALOG) if name == 'sports-month-catalog' else (f'shard-{NUMBER}.json.gz', SHARD)
        (directory/file).write_bytes(gzip.compress(json.dumps(value).encode()))
        return b''
    receipt = recovery.restore(NUMBER, 101, 102, tmp_path/'out', CAMPAIGN, execute)
    assert receipt['checkpoint_run_id'] == 101 and receipt['items'] == 1
    assert receipt['catalog_run_id'] == 102
    assert len(calls) == 5
    assert recovery.read_checkpoint(tmp_path/'out'/f'shard-{NUMBER}.json.gz') == SHARD
    assert (tmp_path/'out'/f'recovery-{NUMBER}.json').is_file()
    with pytest.raises(ValueError, match='OVERWRITE'):
        recovery.restore(NUMBER, 101, 102, tmp_path/'out', CAMPAIGN, execute)


def test_future_source_and_wrong_repository_cannot_be_recovered(tmp_path):
    with pytest.raises(ValueError, match='INVALID_RECOVERY_ORDER'):
        recovery.restore(NUMBER, 103, 102, tmp_path, CAMPAIGN)
    with pytest.raises(ValueError, match='UNTRUSTED_RECOVERY_RUN'):
        recovery.validate_run({'repository': {'full_name':'attacker/repo'}}, CAMPAIGN)
