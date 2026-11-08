import gzip
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys
import zipfile

import pytest

SCRIPT=Path(__file__).parents[2]/'.github/scripts'
spec=importlib.util.spec_from_file_location('recover_sports_shard',SCRIPT/'recover_sports_shard.py')
shard=importlib.util.module_from_spec(spec);spec.loader.exec_module(shard)
sys.modules['recover_sports_shard']=shard
spec=importlib.util.spec_from_file_location('catalog_recovery',SCRIPT/'recover_sports_catalog.py')
c=importlib.util.module_from_spec(spec);spec.loader.exec_module(c)
CAT={'schema':shard.SCHEMA,'first':'2026-09-20','last':'2026-10-19','runtime_fingerprint':'a'*64,
     'done':True,'contracts':{'example':{'provider':'kalshi'}}}
CAT['catalog_hash']=shard.digest(CAT['contracts'])
BLOB=gzip.compress(json.dumps(CAT).encode())
CAM={**{k:CAT[k] for k in ('first','last','runtime_fingerprint')},'root_run_id':100,
     'catalog_recovery':{'tag':'reviewed-backup','filename':'catalog.json.gz',
                         'sha256':hashlib.sha256(BLOB).hexdigest(),'catalog_hash':CAT['catalog_hash']}}
RUN={'id':101,'repository':{'full_name':c.REPOSITORY},'head_branch':'main','path':c.WORKFLOW,'event':'workflow_dispatch'}


def execute_factory(artifacts=None,blob=BLOB,run=RUN):
    calls=[]
    def execute(args):
        calls.append(args)
        if args[1:3]==['release','download']:
            (Path(args[-1])/'catalog.json.gz').write_bytes(blob);return b''
        if args[-1].endswith('/zip'):
            stream=io.BytesIO()
            with zipfile.ZipFile(stream,'w') as z:z.writestr('catalog.json.gz',blob)
            return stream.getvalue()
        if '/artifacts?' in args[-1]:return json.dumps({'artifacts':artifacts or []}).encode()
        return json.dumps(run).encode()
    return execute,calls


def test_missing_artifact_restores_exact_approved_frozen_catalog(tmp_path):
    execute,calls=execute_factory()
    receipt=c.restore(101,tmp_path,CAM,execute)
    assert receipt['kind']=='reviewed_release_backup'
    assert (tmp_path/'catalog.json.gz').read_bytes()==BLOB
    assert json.loads((tmp_path/'catalog-recovery.json').read_text())==receipt
    assert len(calls)==3
    with pytest.raises(ValueError,match='OVERWRITE'):c.restore(101,tmp_path,CAM,execute)


def test_present_artifact_does_not_use_release(tmp_path):
    execute,calls=execute_factory([{'id':1,'name':'sports-month-catalog','expired':False}])
    receipt=c.restore(101,tmp_path,CAM,execute)
    assert receipt['kind']=='actions_artifact'
    assert not any('release' in call for call in calls)


def test_invalid_present_artifact_cannot_hide_behind_valid_backup(tmp_path):
    execute,calls=execute_factory([{'id':1,'name':'sports-month-catalog','expired':False,'digest':'sha256:forged'}])
    with pytest.raises(ValueError,match='ARCHIVE_INVALID'):c.restore(101,tmp_path,CAM,execute)
    assert not (tmp_path/'catalog.json.gz').exists()
    assert not any('release' in call for call in calls)


@pytest.mark.parametrize('change', ['bytes','semantic_hash','date','runtime','unfrozen'])
def test_invalid_backup_refused_without_writing_destination(tmp_path,change):
    altered=json.loads(json.dumps(CAT));campaign=json.loads(json.dumps(CAM))
    if change=='bytes':blob=b'corrupt'
    else:
        if change=='semantic_hash':altered['contracts']['example']['provider']='changed'
        if change=='date':altered['first']='2025-01-01'
        if change=='runtime':altered['runtime_fingerprint']='b'*64
        if change=='unfrozen':altered['done']=False
        blob=gzip.compress(json.dumps(altered).encode());campaign['catalog_recovery']['sha256']=hashlib.sha256(blob).hexdigest()
    execute,_=execute_factory(blob=blob)
    with pytest.raises(ValueError):c.restore(101,tmp_path,campaign,execute)
    assert not (tmp_path/'catalog.json.gz').exists()


@pytest.mark.parametrize('key,value', [('head_branch','attacker'),('id',102),('event','pull_request'),('path','other.yml')])
def test_untrusted_run_not_recovered(tmp_path,key,value):
    execute,_=execute_factory(run={**RUN,key:value})
    with pytest.raises(ValueError,match='UNTRUSTED'):c.restore(101,tmp_path,CAM,execute)


def test_download_failure_does_not_initialize_new_state(tmp_path):
    def fail(args): raise OSError('network unavailable')
    with pytest.raises(OSError):c.restore(101,tmp_path,CAM,fail)
    assert not list(tmp_path.iterdir())


def test_invalid_recovery_order_is_rejected_before_network(tmp_path):
    for value in [99,True,'101']:
        with pytest.raises(ValueError,match='UNTRUSTED'):
            c.restore(value,tmp_path,CAM,lambda args:pytest.fail('unexpected network'))


def test_incomplete_or_duplicate_listing_is_not_missing_catalog(tmp_path):
    artifact={'id':1,'name':'sports-month-catalog','expired':False}
    for listing,code in [({'total_count':101,'artifacts':[]},'INCOMPLETE'),
                         ({'artifacts':[artifact,artifact]},'DUPLICATE')]:
        def execute(args):return json.dumps(listing if '/artifacts?' in args[-1] else RUN).encode()
        with pytest.raises(ValueError,match=code):c.restore(101,tmp_path,CAM,execute)
        assert not (tmp_path/'catalog.json.gz').exists()


def test_oversized_artifact_rejected_before_download(tmp_path):
    execute,_=execute_factory([{'id':1,'name':'sports-month-catalog','expired':False,'size_in_bytes':c.MAX_PACKED+1}])
    with pytest.raises(ValueError,match='TOO_LARGE'):c.restore(101,tmp_path,CAM,execute)


def test_backup_must_match_independently_pinned_semantic_hash(tmp_path):
    execute,_=execute_factory()
    campaign={**CAM,'catalog_recovery':{**CAM['catalog_recovery'],'catalog_hash':'0'*64}}
    with pytest.raises(ValueError,match='CONTENT_CHANGED'):c.restore(101,tmp_path,campaign,execute)


def test_wrong_policy_in_existing_artifact_cannot_be_applied(tmp_path):
    altered={**CAT,'runtime_fingerprint':'b'*64}
    execute,_=execute_factory([{'id':1,'name':'sports-month-catalog','expired':False}],gzip.compress(json.dumps(altered).encode()))
    with pytest.raises(ValueError,match='POLICY_OR_DATES'):c.restore(101,tmp_path,CAM,execute)


def test_duplicate_archive_members_are_not_extracted(tmp_path):
    execute,_=execute_factory([{'id':1,'name':'sports-month-catalog','expired':False}])
    def bad_archive(args):
        if args[-1].endswith('/zip'):
            b=io.BytesIO()
            with zipfile.ZipFile(b,'w') as z:z.writestr('../catalog.json.gz',BLOB)
            return b.getvalue()
        return execute(args)
    with pytest.raises(ValueError,match='MEMBERS'):c.restore(101,tmp_path,CAM,bad_archive)
    assert not (tmp_path.parent/'catalog.json.gz').exists()


def test_cli_rejects_unapproved_campaign_before_any_download(monkeypatch,tmp_path):
    import runpy
    campaign=tmp_path/'campaign.json';campaign.write_text(json.dumps({**CAM,'code_sha':'a'*40}))
    monkeypatch.setattr(sys,'argv',['recover_sports_catalog','--run','101','--directory',str(tmp_path/'out'),
                                  '--campaign',str(campaign),'--first','2026-09-20','--last','2026-10-19','--code-sha','b'*40])
    with pytest.raises(SystemExit,match='APPROVED_CAMPAIGN'):
        runpy.run_path(str(SCRIPT/'recover_sports_catalog.py'),run_name='__main__')
    assert not (tmp_path/'out').exists()
