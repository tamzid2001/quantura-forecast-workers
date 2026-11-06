import base64
from datetime import datetime,timezone,timedelta
import json
import subprocess
import io
import sys
import zipfile
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from metaculus_bot import automation as a


def response(code=0,status=None,body=None):
    return SimpleNamespace(returncode=code,stdout=json.dumps(body or {}).encode(),
                           stderr=(f'private-token (HTTP {status})'.encode() if status else b''))


def test_transient_reads_are_bounded_and_successful_response_is_returned(monkeypatch):
    call=Mock(side_effect=[response(1,503),response(1,429),response(body={'sha':'valid'})])
    monkeypatch.setattr(a.subprocess,'run',call);monkeypatch.setattr(a.time,'sleep',lambda _:None)
    assert a.github('safe/read')['sha']=='valid' and call.call_count==3


@pytest.mark.parametrize('status',[401,403,404,409,422,500])
def test_write_failures_are_sanitized_and_never_blindly_retried(status,monkeypatch):
    call=Mock(return_value=response(1,status))
    monkeypatch.setattr(a.subprocess,'run',call)
    with pytest.raises(a.GitHubAPIError) as caught:a.github('safe/dispatch',{'ref':'main'},'POST')
    assert caught.value.status==status and call.call_count==1
    assert 'private-token' not in str(caught.value)


def test_transport_timeout_is_not_retried_for_dispatch(monkeypatch):
    call=Mock(side_effect=subprocess.TimeoutExpired('gh',30))
    monkeypatch.setattr(a.subprocess,'run',call)
    with pytest.raises(a.GitHubAPIError,match='UNAVAILABLE'):a.github('safe/dispatch',{'ref':'main'},'POST')
    assert call.call_count==1


def environment(monkeypatch):
    for key,value in {'GITHUB_REPOSITORY':'owner/repo','METACULUS_BOT_ID':'1','GITHUB_RUN_ID':'42'}.items():monkeypatch.setenv(key,value)


def test_heartbeat_sha_conflict_refetches_before_retry(monkeypatch,tmp_path):
    environment(monkeypatch)
    call=Mock(side_effect=[{'sha':'old'},a.GitHubAPIError(409,'PUT'),{'sha':'new'},{}])
    monkeypatch.setattr(a,'github',call)
    a.heartbeat(tmp_path/'absent-report')
    assert call.call_args_list[1].args[1]['sha']=='old'
    assert call.call_args_list[3].args[1]['sha']=='new'


def test_only_confirmed_404_allows_creation_and_permissions_fail_closed(monkeypatch,tmp_path):
    environment(monkeypatch)
    call=Mock(side_effect=[a.GitHubAPIError(404,'GET'),{}]);monkeypatch.setattr(a,'github',call)
    a.heartbeat(tmp_path/'absent')
    assert 'sha' not in call.call_args_list[1].args[1]
    call=Mock(side_effect=a.GitHubAPIError(403,'GET'));monkeypatch.setattr(a,'github',call)
    with pytest.raises(a.GitHubAPIError):a.heartbeat(tmp_path/'absent')
    assert call.call_count==1


def test_older_worker_cannot_overwrite_a_newer_public_heartbeat(monkeypatch,tmp_path):
    environment(monkeypatch)
    future=(datetime.now(timezone.utc)+timedelta(seconds=60)).isoformat()
    prior={'sha':'newer','content':base64.b64encode(json.dumps({'at':future}).encode()).decode()}
    call=Mock(return_value=prior);monkeypatch.setattr(a,'github',call)
    a.heartbeat(tmp_path/'absent')
    assert call.call_count==1


def test_conflicts_are_bounded_and_uncertain_heartbeat_writes_do_not_retry(monkeypatch,tmp_path):
    environment(monkeypatch)
    call=Mock(side_effect=[{'sha':'a'},a.GitHubAPIError(409,'PUT')]*3);monkeypatch.setattr(a,'github',call)
    with pytest.raises(a.GitHubAPIError):a.heartbeat(tmp_path/'absent')
    assert call.call_count==6
    call=Mock(side_effect=[{'sha':'a'},a.GitHubAPIError(503,'PUT')]);monkeypatch.setattr(a,'github',call)
    with pytest.raises(a.GitHubAPIError):a.heartbeat(tmp_path/'absent')
    assert call.call_count==2


def test_watchdog_is_disabled_without_operator_enablement_and_never_duplicates_active_runs(monkeypatch):
    environment(monkeypatch)
    call=Mock();monkeypatch.setattr(a,'github',call);monkeypatch.delenv('METACULUS_BOT_ENABLED',raising=False)
    a.watchdog('both');call.assert_not_called()
    monkeypatch.setenv('METACULUS_BOT_ENABLED','true')
    call.return_value={'workflow_runs':[{'event':'schedule','status':'queued','created_at':datetime.now(timezone.utc).isoformat()}]}
    a.watchdog('both')
    assert call.call_count==2 and all(len(c.args)==1 for c in call.call_args_list)


def test_watchdog_dispatch_has_approved_competition_scope_and_uncertain_post_does_not_repeat(monkeypatch):
    environment(monkeypatch);monkeypatch.setenv('METACULUS_BOT_ENABLED','true')
    call=Mock(side_effect=[{'workflow_runs':[]},{}]);monkeypatch.setattr(a,'github',call)
    a.watchdog('forecast')
    path,body,method=call.call_args_list[-1].args
    assert method=='POST' and body['inputs']['scope']=='competitions' and body['inputs']['submit']=='true'
    call=Mock(side_effect=[{'workflow_runs':[]},a.GitHubAPIError(503,'POST')]);monkeypatch.setattr(a,'github',call)
    with pytest.raises(a.GitHubAPIError):a.watchdog('forecast')
    assert call.call_count==2


def test_stalled_worker_is_alerted_instead_of_cancelled_or_duplicated(monkeypatch):
    environment(monkeypatch);monkeypatch.setenv('METACULUS_BOT_ENABLED','true')
    old=(datetime.now(timezone.utc)-timedelta(hours=2)).isoformat()
    call=Mock(return_value={'workflow_runs':[{'event':'schedule','status':'in_progress','created_at':old}]})
    monkeypatch.setattr(a,'github',call)
    with pytest.raises(RuntimeError,match='METACULUS_WORKER_STALLED'):a.watchdog('forecast')
    assert call.call_count==1


def test_queue_restore_reads_only_fresh_correct_bot_artifact_and_validates_archive(monkeypatch,tmp_path):
    environment(monkeypatch)
    queue={'at':datetime.now(timezone.utc).isoformat(),'bot_id':1,'questions':[{'question_id':2,'post_id':3}]}
    payload=io.BytesIO()
    with zipfile.ZipFile(payload,'w') as z:z.writestr('metaculus-queue.json',json.dumps(queue))
    monkeypatch.setattr(a,'github',Mock(side_effect=[{'workflow_runs':[{'event':'pull_request','id':9},{'event':'schedule','id':1}]},
        {'artifacts':[{'name':'metaculus-open-queue','id':2,'expired':False}]}]))
    monkeypatch.setattr(a.subprocess,'run',Mock(return_value=SimpleNamespace(stdout=payload.getvalue())))
    assert a.restore_queue(tmp_path/'queue.json')==([2],[3])
    monkeypatch.setattr(a,'github',Mock(return_value={'workflow_runs':[]}))
    assert a.restore_queue(tmp_path/'empty.json')==([],[])


def test_github_read_timeouts_are_bounded_and_empty_dispatch_response_is_valid(monkeypatch):
    call=Mock(side_effect=subprocess.TimeoutExpired('gh',30));monkeypatch.setattr(a.subprocess,'run',call)
    monkeypatch.setattr(a.time,'sleep',lambda _:None)
    with pytest.raises(a.GitHubAPIError):a.github('safe/read')
    assert call.call_count==3
    call=Mock(return_value=SimpleNamespace(returncode=0,stdout=b'',stderr=b''));monkeypatch.setattr(a.subprocess,'run',call)
    assert a.github('safe/dispatch',{},'POST')=={}
    assert call.call_args.kwargs['input']==b'{}'


@pytest.mark.parametrize('command',['heartbeat','watchdog'])
def test_actual_cli_routes_heartbeat_and_watchdog_without_implicit_forecast_calls(command,monkeypatch,tmp_path):
    handler=Mock();monkeypatch.setattr(a,command,handler)
    args=['automation',command,'--file',str(tmp_path/'report'),'--target','forecast']
    monkeypatch.setattr(sys,'argv',args)
    a.main()
    handler.assert_called_once_with('forecast' if command=='watchdog' else tmp_path/'report')


@pytest.mark.parametrize('deferred',[False,True])
def test_queue_cli_emits_parseable_safe_outputs_for_available_and_deferred_queues(deferred,monkeypatch,tmp_path):
    output=tmp_path/'outputs';monkeypatch.setenv('GITHUB_OUTPUT',str(output))
    monkeypatch.setattr(sys,'argv',['automation','queue','--file',str(tmp_path/'queue')])
    monkeypatch.setattr(a,'restore_queue',Mock(side_effect=ValueError('STALE_OR_WRONG_BOT_QUEUE')) if deferred else Mock(return_value=([2],[3])))
    a.main()
    values=dict(line.split('=',1) for line in output.read_text().splitlines())
    assert values['ready']==('false' if deferred else 'true')
    assert json.loads(values['question_ids'])==([] if deferred else [2])
    assert json.loads(values['post_ids'])==([] if deferred else [3])


def test_queue_cli_works_without_actions_output_and_rejects_missing_file(monkeypatch,tmp_path):
    monkeypatch.delenv('GITHUB_OUTPUT',raising=False)
    monkeypatch.setattr(a,'restore_queue',Mock(return_value=([],[])))
    monkeypatch.setattr(sys,'argv',['automation','queue','--file',str(tmp_path/'queue')]);a.main()
    monkeypatch.setattr(sys,'argv',['automation','queue'])
    with pytest.raises(SystemExit) as error:a.main()
    assert error.value.code==2
