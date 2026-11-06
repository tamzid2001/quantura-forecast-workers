"""Deadline, process lifetime and partial-evidence regressions; no network/API writes."""
from datetime import datetime, timedelta, timezone
import io
import json
import os
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest
import requests

from metaculus_bot import research, runner


SOURCE = {'url': 'https://example.org/source', 'excerpt': 'Verified source text. ' * 10,
          'sha256': 'a' * 64, 'retrieved_at': '2026-11-06T00:00:00+00:00'}


def question(**kwargs):
    return SimpleNamespace(**{'question_text': 'Will unemployment rise?', 'resolution_criteria': '',
                              'fine_print': None, 'background_info': None, **kwargs})


def wire(source=SOURCE):
    return json.dumps(source).encode() + b'\n'


def test_process_receives_only_research_fields_and_required_credentials(monkeypatch, capsys):
    monkeypatch.setenv('METACULUS_TOKEN', 'private-metaculus')
    monkeypatch.setenv('GITHUB_TOKEN', 'private-github')
    monkeypatch.setenv('NVIDIA_API_KEY', 'private-model')
    monkeypatch.setenv('FRED_API_KEY', 'f' * 32)
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        assert command == [sys.executable, '-m', 'metaculus_bot.research', '--worker']
        assert kwargs['check'] is False and kwargs['stderr'] == subprocess.DEVNULL
        assert kwargs['stdout'] == subprocess.PIPE and kwargs['timeout'] == 60
        assert 'shell' not in kwargs
        assert set(json.loads(kwargs['input'])) == set(research.QUESTION_FIELDS)
        assert kwargs['env']['FRED_API_KEY'] == 'f' * 32
        assert not any(k in kwargs['env'] for k in ['METACULUS_TOKEN', 'GITHUB_TOKEN', 'NVIDIA_API_KEY'])
        return SimpleNamespace(stdout=wire(), returncode=0)
    monkeypatch.setattr(research.subprocess, 'run', run)
    q = question(secret='must-not-leave-parent')
    assert research.evidence(q) == [{'id': 'S1', **SOURCE}]
    assert len(calls) == 1
    log = capsys.readouterr().out
    assert json.loads(log)['status'] == 'completed'
    assert not any(v in log for v in ['private-', SOURCE['excerpt'], SOURCE['url'], 'f' * 32])


def test_expired_budget_starts_no_child(monkeypatch):
    monkeypatch.setattr(research.subprocess, 'run', lambda *a, **k: pytest.fail('No child after deadline'))
    assert research.evidence(question(), deadline=time.monotonic()-1) == []


@pytest.mark.parametrize('deadline', [True, 'tomorrow', float('nan'), float('inf')])
def test_invalid_deadline_rejected(deadline):
    with pytest.raises(ValueError, match='INVALID_RESEARCH_DEADLINE'):
        research.evidence(question(), deadline=deadline)


def test_deadline_clamps_timeout_and_keeps_complete_records_after_timeout(monkeypatch, capsys):
    def run(*args, **kwargs):
        assert 0 < kwargs['timeout'] <= 5
        raise subprocess.TimeoutExpired(args[0], kwargs['timeout'], output=wire() + b'{"unfinished":')
    monkeypatch.setattr(research.subprocess, 'run', run)
    assert research.evidence(question(), deadline=time.monotonic()+5) == [{'id': 'S1', **SOURCE}]
    assert json.loads(capsys.readouterr().out)['status'] == 'deadline_reached'


@pytest.mark.parametrize('returncode', [1, -9])
def test_nonzero_worker_can_only_return_completed_sources(monkeypatch, capsys, returncode):
    monkeypatch.setattr(research.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout=wire(), returncode=returncode))
    assert len(research.evidence(question())) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'worker_failed'


def test_spawn_error_is_sanitized(monkeypatch, capsys):
    def run(*args, **kwargs): raise OSError('secret file or credentials')
    monkeypatch.setattr(research.subprocess, 'run', run)
    assert research.evidence(question()) == []
    assert 'secret' not in capsys.readouterr().out


@pytest.mark.parametrize('q,code', [(question(question_text=12), 'INVALID_RESEARCH_QUESTION'),
                                   (question(background_info='x' * 256_001), 'RESEARCH_QUESTION_TOO_LARGE')])
def test_input_limits_reject_without_truncating_question(q, code):
    with pytest.raises(ValueError, match=code): research.evidence(q)


def test_private_protocol_rejects_invalid_and_uncommitted_records():
    invalid = [None, [], {**SOURCE, 'sha256': 'bad'}, {**SOURCE, 'url': 'http://example.org'},
               {**SOURCE, 'excerpt': ''}, {**SOURCE, 'excerpt': 'x' * 16001},
               {**SOURCE, 'retrieved_at': 1}]
    raw = b''.join(wire(s) for s in invalid) + b'not-json\n' + b'\xff\n' + wire() * 8 + wire().rstrip(b'\n')
    result = research._completed_sources(raw)
    assert len(result) == 6 and [s['id'] for s in result] == ['S1', 'S2', 'S3', 'S4', 'S5', 'S6']
    assert research._completed_sources(None) == []
    assert research._completed_sources(b'x' * (research.MAX_WORKER_OUTPUT_BYTES+1)) == []


def test_worker_serializes_only_valid_complete_source_records(monkeypatch, capsys):
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps({
        k: getattr(question(), k) for k in research.QUESTION_FIELDS}).encode())))
    def collect(q, emit):
        assert q.question_text == question().question_text
        emit({'private': 'never write this'})
        emit({**SOURCE, 'private': 'not included'})
    monkeypatch.setattr(research, '_collect_evidence', collect)
    research._worker()
    assert capsys.readouterr().out == json.dumps(SOURCE) + '\n'


@pytest.mark.parametrize('raw,code', [(b'x' * 256001, 'TOO_LARGE'),
                                     (b'[]', 'INVALID_RESEARCH_QUESTION'),
                                     (b'{"question_text": 1}', 'INVALID_RESEARCH_QUESTION'),
                                     (json.dumps({k: 1 for k in research.QUESTION_FIELDS}).encode(), 'INVALID_RESEARCH_QUESTION')])
def test_child_validates_input_before_network(monkeypatch, raw, code):
    monkeypatch.setattr(sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(raw)))
    with pytest.raises(ValueError, match=code): research._worker()


def test_stalled_real_child_is_killed_reaped_and_retains_partial_evidence(monkeypatch, tmp_path, capsys):
    # Actual subprocess test: no mock is used for timeout, kill, wait or stdout.
    original_run = subprocess.run
    pid_file = tmp_path/'pid'
    script = ('import os,time,sys; from pathlib import Path; '
              f'Path({str(pid_file)!r}).write_text(str(os.getpid())); '
              f'print({json.dumps(SOURCE)!r},flush=True); time.sleep(30)')
    def run(command, **kwargs):
        return original_run([sys.executable, '-c', script], **kwargs)
    monkeypatch.setattr(research.subprocess, 'run', run)
    start = time.monotonic()
    assert research.evidence(question(), deadline=start+2) == [{'id': 'S1', **SOURCE}]
    assert time.monotonic()-start < 10
    assert json.loads(capsys.readouterr().out)['status'] == 'deadline_reached'
    if os.name == 'posix':
        with pytest.raises(ProcessLookupError): os.kill(int(pid_file.read_text()), 0)


def test_no_sources_child_entrypoint_runs_without_credentials(monkeypatch):
    monkeypatch.delenv('FRED_API_KEY', raising=False)
    assert research.evidence(question(question_text='A generic question with no linked sources')) == []


def test_worker_rejects_wrong_entrypoint_argument():
    result = subprocess.run([sys.executable, '-m', 'metaculus_bot.research'], capture_output=True)
    assert result.returncode != 0 and b'RESEARCH_WORKER_ARGUMENT_REQUIRED' in result.stderr


def test_collection_retains_completed_web_sources_when_fred_is_empty(monkeypatch):
    from metaculus_bot import fred
    raw = b'<html><style>ignore</style><script>ignore</script><p>' + b'Actual reference text. ' * 10 + b'</p></html>'
    monkeypatch.setattr(research, 'public_get', lambda u: (raw, u))
    monkeypatch.setattr(fred, 'evidence', lambda q, **kwargs: [])
    values = []
    research._collect_evidence(question(resolution_criteria=' '.join(f'https://example.org/{i}' for i in range(6))), values.append)
    assert len(values) == 3
    assert all('ignore' not in s['excerpt'] and len(s['sha256']) == 64 for s in values)


def test_collection_skips_failed_or_short_sources_and_emits_fred(monkeypatch):
    from metaculus_bot import fred
    def fetch(url):
        if url.endswith('/error'): raise requests.Timeout('private detail')
        return b'<p>Too short</p>', url
    monkeypatch.setattr(research, 'public_get', fetch)
    monkeypatch.setattr(fred, 'evidence', lambda q, *, emit: emit(dict(SOURCE)))
    result = []
    research._collect_evidence(question(resolution_criteria='https://example.org/error https://example.org/short'), result.append)
    assert len(result) == 1 and result[0]['id'] == 'S1'


def test_research_reserves_model_and_submission_time(monkeypatch):
    now = datetime(2026, 11, 6, tzinfo=timezone.utc)
    monkeypatch.setattr(runner.time, 'monotonic', lambda: 1000.)
    monkeypatch.setattr(runner, 'utcnow', lambda: now)
    assert runner.research_deadline(question(close_time=None), 2000) == 1060
    assert runner.research_deadline(question(close_time=None), 1070) == 1010
    assert runner.research_deadline(question(close_time=now+timedelta(seconds=270)), 2000) == 1030
    assert runner.research_deadline(question(close_time=now+timedelta(seconds=200)), 2000) < 1000


class Response:
    def __init__(self, raw=b'reference', redirect=False, location=None, fail=False):
        self.raw, self.is_redirect, self.fail = raw, redirect, fail
        self.headers = {'Location': location} if location else {}
        self.closed = False
    def __enter__(self): return self
    def __exit__(self, *args): self.closed = True
    def raise_for_status(self):
        if self.fail: raise requests.HTTPError('sensitive body')
    def iter_content(self, size):
        yield self.raw


def test_public_download_follows_only_revalidated_redirects_and_bounds_body(monkeypatch):
    allowed, calls, responses = [], [], [Response(redirect=True, location='/final'), Response(raw=b'real source')]
    def check(url): allowed.append(url); return True
    def get(url, **kwargs):
        assert kwargs['allow_redirects'] is False and kwargs['stream'] is True
        assert kwargs['timeout'] == (8, 20)
        calls.append(url);return responses[len(calls)-1]
    session = SimpleNamespace(get=get)
    monkeypatch.setattr(research.requests, 'Session', lambda: session)
    monkeypatch.setattr(research, 'public_url', check)
    assert research.public_get('https://example.org/start') == (b'real source', 'https://example.org/final')
    assert calls == allowed == ['https://example.org/start', 'https://example.org/final']
    assert session.trust_env is False and all(r.closed for r in responses)


@pytest.mark.parametrize('mode,code', [('private', 'NONPUBLIC'), ('oversize', 'TOO_LARGE'), ('loop', 'REDIRECT_LIMIT')])
def test_public_download_rejects_unsafe_or_unbounded_sources(monkeypatch, mode, code):
    response = Response(raw=b'12345', redirect=mode=='loop', location='/loop')
    monkeypatch.setattr(research.requests, 'Session', lambda: SimpleNamespace(get=lambda *a, **kw: response))
    monkeypatch.setattr(research, 'public_url', lambda u: mode != 'private')
    with pytest.raises(ValueError, match=code): research.public_get('https://example.org', maximum=4)
    if mode != 'private': assert response.closed


def test_public_download_status_failure_closes_response(monkeypatch):
    response = Response(fail=True)
    monkeypatch.setattr(research.requests, 'Session', lambda: SimpleNamespace(get=lambda *a, **kw: response))
    monkeypatch.setattr(research, 'public_url', lambda u: True)
    with pytest.raises(requests.HTTPError): research.public_get('https://example.org')
    assert response.closed


def test_public_url_limits_dns_failures_and_private_addresses(monkeypatch):
    for url in ['https://' + 'x' * 4097, 'http://example.org', 'https://u:p@example.org',
                'https://example.org:444', 'https://metaculus.com', 'https://www.metaculus.com']:
        assert research.public_url(url) is False
    monkeypatch.setattr(research.socket, 'getaddrinfo', lambda *a, **k: [(None, None, None, None, ('127.0.0.1', 443))])
    assert research.public_url('https://example.org') is False
    def unavailable(*a, **k): raise OSError('private diagnostic')
    monkeypatch.setattr(research.socket, 'getaddrinfo', unavailable)
    assert research.public_url('https://example.org') is False
