"""Direct free providers remain usable when every Vercel service is absent."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from metaculus_bot import llm, minibench
from metaculus_bot.runner import record_text


def test_direct_never_initializes_gateway_and_falls_back_only_after_rejection(monkeypatch):
    monkeypatch.setenv('METACULUS_LLM_PROVIDER', 'direct')
    monkeypatch.setenv('NVIDIA_API_KEY', 'test-nvidia')
    monkeypatch.setenv('AI_GATEWAY_API_KEY', 'test-gateway')
    monkeypatch.setattr(llm, 'NVIDIA_MODELS', ['test-nvidia-model'])
    gateway = Mock(side_effect=AssertionError('Vercel must not be contacted'))
    monkeypatch.setattr(llm, 'FreeGatewayLLM', gateway)
    primary = SimpleNamespace(model={'id':'test-nvidia-model'},service='NVIDIA_BUILD',
                              forecast=Mock(side_effect=llm.FreeQuota('QUOTA_REJECTED')))
    fallback = SimpleNamespace(model={'id':'model:free'},service='OPENROUTER',
                               forecast=Mock(return_value={'probability_yes':.6}))
    monkeypatch.setattr(llm, 'FreeNvidiaLLM', lambda *a, **kw: primary)
    factory = Mock(return_value=fallback)
    monkeypatch.setattr(llm, 'FreeLLM', factory)
    client = llm.free_llm_factory('test-openrouter')
    factory.assert_not_called()
    assert client.forecast('question', [])['probability_yes'] == .6
    factory.assert_called_once_with('test-openrouter')
    gateway.assert_not_called()


def test_direct_without_credentials_fails_clearly(monkeypatch):
    monkeypatch.setenv('METACULUS_LLM_PROVIDER','direct')
    monkeypatch.delenv('NVIDIA_API_KEY',raising=False)
    with pytest.raises(llm.FreeQuota,match='DIRECT_FREE_LLM_UNAVAILABLE'):
        llm.free_llm_factory('')


def test_private_note_audit_checks_privacy_author_reasoning_and_preserves_confidentiality():
    record = {'question_id':1,'answer':{'model':'test:free','provider_service':'OPENROUTER',
                                      'probability_yes':.6,'reasoning':'Private explanation never included in exported coverage.'}}
    text = record_text(record)
    assert 'free_endpoint_policy' in text
    good = {'author':{'id':310211},'is_private':True,'text':text}
    comments = [good, {**good,'is_private':False}, {**good,'author':{'id':99}},
                {**good,'text':'malformed'}, {**good,'text':text.replace('"question_id":1','"question_id":2').replace('Private explanation never included in exported coverage.','')}]
    result = minibench.private_reasoning_coverage(comments,310211,{1,2})
    assert result['submitted_with_private_reasoning'] == 1
    assert result['free_endpoint_attested'] == 1
    assert result['submitted_missing_verified_private_reasoning_question_ids'] == [2]
    assert 'Private explanation' not in json.dumps(result)


def test_legacy_note_remains_visible_without_fabricating_free_endpoint_metadata():
    text = record_text({'question_id':3,'answer':{'probability_yes':.5,'reasoning':'Prior explanation.'}})
    result = minibench.private_reasoning_coverage([{'author':{'id':1},'is_private':True,'text':text}],1,{3})
    assert result['submitted_with_private_reasoning'] == 1
    assert result['legacy_free_endpoint_metadata_unavailable'] == 1
    assert result['free_endpoint_attested'] == 0


def test_pending_diagnostic_exports_only_failure_state():
    record = {'answer':{'abstain':True, 'generation_error':'EMPTY_MODEL_ANSWER',
                        'reasoning':'confidential', 'probability_yes':.8}, 'note_write_state':'uncertain'}
    result = minibench.pending_diagnostic(7, SimpleNamespace(get=lambda q:record))
    assert result == {'question_id':7, 'saved_generation_available':True, 'abstained':True,
                      'generation_error':'EMPTY_MODEL_ANSWER', 'private_note_write_state':'uncertain'}
    assert 'confidential' not in json.dumps(result)
    assert minibench.pending_diagnostic(8,None)['saved_generation_available'] is False
    record['answer']['generation_error'] = 'sensitive response text must not be exported'
    assert minibench.pending_diagnostic(7,SimpleNamespace(get=lambda q:record))['generation_error'] is None
