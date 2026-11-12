import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock
import pytest

spec=importlib.util.spec_from_file_location('prepare_toto',Path(__file__).parents[2]/'.github/scripts/prepare_frozen_toto.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
REV='a'*40


def snapshot(tmp_path,sharded=False):
    source=tmp_path/'cache'/REV;source.mkdir(parents=True)
    (source/'config.json').write_text('{"patch_size":16}')
    name='model-01.safetensors' if sharded else 'model.safetensors'
    (source/name).write_bytes(b'fixture-not-real-weights')
    if sharded:(source/'model.safetensors.index.json').write_text(json.dumps({'weight_map':{'layer':name}}))
    return source


@pytest.mark.parametrize('sharded',[False,True])
def test_exact_revision_materialized_without_touching_frozen_source_or_bytes(tmp_path,sharded):
    source=snapshot(tmp_path,sharded);download=Mock(return_value=str(source));root=tmp_path/'checkout';root.mkdir()
    frozen=root/'frozen.py';frozen.write_text('unchanged forecast implementation')
    before={p.name:p.read_bytes() for p in source.iterdir()}
    receipt=m.prepare(root,m.MODEL,REV,download)
    assert (root/m.MODEL).resolve()==source
    assert frozen.read_text()=='unchanged forecast implementation'
    assert {p.name:p.read_bytes() for p in source.iterdir()}==before
    assert not receipt['forecast_source_modified'] and not receipt['weights_modified']
    assert receipt['revision']==REV and set(receipt['files_sha256'])==set(before)
    download.assert_called_once_with(repo_id=m.MODEL,revision=REV,token=False,
        allow_patterns=['config.json','*.safetensors','*.safetensors.index.json'])
    with pytest.raises(ValueError,match='OVERWRITE'):m.prepare(root,m.MODEL,REV,download)


@pytest.mark.parametrize('model,revision', [('other/model',REV),(m.MODEL,None),(m.MODEL,'main'),(m.MODEL,'../bad')])
def test_unpinned_models_refused_before_download(tmp_path,model,revision):
    with pytest.raises(ValueError,match='UNAPPROVED'):
        m.prepare(tmp_path,model,revision,lambda **kw:pytest.fail('download'))


@pytest.mark.parametrize('case', ['wrong_revision','no_config','bad_config','large_config','missing_weights','unsafe_index','empty_index','large_index','large_weights'])
def test_partial_or_untrusted_snapshot_is_not_exposed_as_model(tmp_path,case,monkeypatch):
    source=snapshot(tmp_path)
    if case=='wrong_revision':source=source.rename(source.with_name('b'*40))
    if case=='no_config':(source/'config.json').unlink()
    if case=='bad_config':(source/'config.json').write_text('{')
    if case=='large_config':(source/'config.json').write_bytes(b' '* (1024*1024+1))
    if case=='missing_weights':(source/'model.safetensors').unlink()
    if case in ['unsafe_index','empty_index','large_index']:
        (source/'model.safetensors.index.json').write_text(json.dumps({'weight_map':{'layer':'../outside.safetensors'} if case=='unsafe_index' else {}}))
        if case=='large_index':(source/'model.safetensors.index.json').write_bytes(b' '* (1024*1024+1))
    if case=='large_weights':monkeypatch.setattr(m,'MAX_WEIGHT_BYTES',1)
    with pytest.raises((ValueError,FileNotFoundError)):
        m.prepare(tmp_path/'checkout',m.MODEL,REV,lambda **kw:str(source))
    assert not (tmp_path/'checkout'/m.MODEL).exists()


def test_download_failure_is_not_replaced_with_another_revision(tmp_path):
    download=Mock(side_effect=TimeoutError('fixture'))
    with pytest.raises(TimeoutError):m.prepare(tmp_path,m.MODEL,REV,download)
    assert download.call_count==1 and not (tmp_path/m.MODEL).exists()


def test_symlink_parent_cannot_redirect_model_installation(tmp_path):
    root=tmp_path/'checkout';root.mkdir();outside=tmp_path/'outside';outside.mkdir()
    (root/'Datadog').symlink_to(outside,target_is_directory=True)
    with pytest.raises(ValueError,match='UNSAFE_MODEL_PARENT'):
        m.prepare(root,m.MODEL,REV,lambda **kw:pytest.fail('download'))
    assert not list(outside.iterdir())
