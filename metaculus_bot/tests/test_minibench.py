from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from metaculus_bot import minibench

NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def test_round_coverage_counts_closed_misses_and_deduplicates_questions(monkeypatch):
    monkeypatch.setenv('METACULUS_BOT_ID', '310211')
    questions = [SimpleNamespace(id_of_question=i, close_time=datetime(2026, 10, day, tzinfo=timezone.utc),
                                 open_time=None, state=SimpleNamespace(value='open'))
                 for i, day in [(1, 25), (2, 27), (3, 28)]]
    monkeypatch.setattr(minibench, 'all_questions', lambda p: questions)
    calls = []
    class API:
        def identity(self): return {'id': 310211}
        def get(self, path, **kwargs):
            calls.append((path, kwargs))
            if path == 'projects/minibenches/':
                return [{'id': 1, 'name': 'MiniBench', 'start_date': '2026-10-19', 'is_ongoing': False}]
            return {'results': [{}, {}], 'next': None}
        def own_forecasts(self, ids):
            assert ids == [1, 2, 3]
            return {3}
    result = minibench.audit(API(), NOW)
    assert result['live_submissions'] == 0 and result['rounds'][0]['total_questions'] == 3
    assert result['rounds'][0]['counts'] == {'closed_without_forecast': 1, 'open_unsubmitted': 1, 'submitted': 1}
    assert 'statuses' not in calls[-1][1]


def test_round_audit_rejects_wrong_account_before_reading_questions(monkeypatch):
    monkeypatch.setenv('METACULUS_BOT_ID', '310211')
    with pytest.raises(RuntimeError, match='BOT_ID_MISMATCH'):
        minibench.audit(SimpleNamespace(identity=lambda: {'id': 99}), NOW)
