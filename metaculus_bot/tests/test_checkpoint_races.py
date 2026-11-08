import hashlib
import io
import subprocess
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from metaculus_bot import checkpoint as c


def deleted(status=404):
    return subprocess.CalledProcessError(1, ['gh', 'api'], stderr=f'gh: missing (HTTP {status})'.encode())


def test_deleted_artifact_rediscovery_accepts_only_same_or_newer_state(monkeypatch, tmp_path):
    discover = Mock(side_effect=[{'id':10}, {'id':11}])
    download = Mock(side_effect=[deleted(), None])
    monkeypatch.setattr(c, 'restore_artifact', download)
    monkeypatch.setattr(c.time, 'sleep', Mock())
    c.restore_latest('owner/repo', discover, tmp_path/'state')
    assert [call.args[1]['id'] for call in download.call_args_list] == [10, 11]


@pytest.mark.parametrize('next_artifact,error', [(None,'DISAPPEARED'), ({'id':9},'ROLLBACK')])
def test_known_checkpoint_cannot_silently_reset_or_roll_back(monkeypatch, tmp_path, next_artifact, error):
    path = tmp_path/'state'; path.write_bytes(b'preserved-state')
    monkeypatch.setattr(c, 'restore_artifact', Mock(side_effect=deleted(410)))
    monkeypatch.setattr(c.time, 'sleep', Mock())
    with pytest.raises(RuntimeError, match=error):
        c.restore_latest('owner/repo', Mock(side_effect=[{'id':10},next_artifact]), path)
    assert path.read_bytes() == b'preserved-state'


@pytest.mark.parametrize('status', [401,403,429,500,503])
def test_non_deletion_failures_do_not_fall_back(monkeypatch, tmp_path, status):
    discover = Mock(return_value={'id':10})
    monkeypatch.setattr(c, 'restore_artifact', Mock(side_effect=deleted(status)))
    with pytest.raises(subprocess.CalledProcessError):
        c.restore_latest('owner/repo', discover, tmp_path/'state')
    assert discover.call_count == 1


def test_deleted_race_is_bounded(monkeypatch, tmp_path):
    discover = Mock(return_value={'id':10})
    monkeypatch.setattr(c, 'restore_artifact', Mock(side_effect=deleted()))
    monkeypatch.setattr(c.time, 'sleep', Mock())
    with pytest.raises(RuntimeError, match='DOWNLOAD_RACE'):
        c.restore_latest('owner/repo', discover, tmp_path/'state')
    assert discover.call_count == 3


def archive(contents=b'encrypted-sensitive-state'):
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as z: z.writestr('metaculus-checkpoint.enc', contents)
    return data.getvalue()


def test_digest_and_private_atomic_restore(monkeypatch, tmp_path):
    data=archive()
    monkeypatch.setattr(c.subprocess,'run',Mock(return_value=SimpleNamespace(stdout=data)))
    path=tmp_path/'state'; path.write_bytes(b'old')
    c.restore_artifact('owner/repo',{'id':1,'digest':'sha256:'+hashlib.sha256(data).hexdigest()},path)
    assert path.read_bytes()==b'encrypted-sensitive-state'
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.glob('.checkpoint-*'))==[]


def test_bad_digest_preserves_prior_state(monkeypatch,tmp_path):
    monkeypatch.setattr(c.subprocess,'run',Mock(return_value=SimpleNamespace(stdout=archive())))
    path=tmp_path/'state';path.write_bytes(b'old')
    with pytest.raises(RuntimeError,match='DIGEST_MISMATCH'):
        c.restore_artifact('owner/repo',{'id':1,'digest':'sha256:'+'0'*64},path)
    assert path.read_bytes()==b'old'


def test_failed_replace_preserves_prior_state_and_removes_temporary(monkeypatch,tmp_path):
    monkeypatch.setattr(c.subprocess,'run',Mock(return_value=SimpleNamespace(stdout=archive())))
    monkeypatch.setattr(c.os,'replace',Mock(side_effect=OSError('disk-full')))
    path=tmp_path/'state';path.write_bytes(b'old')
    with pytest.raises(OSError): c.restore_artifact('owner/repo',{'id':1},path)
    assert path.read_bytes()==b'old'
    assert list(tmp_path.glob('.checkpoint-*'))==[]
