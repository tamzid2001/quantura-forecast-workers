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
    assert result['next_round']=={'id':None,'start_date':'2026-11-02T00:00:00+00:00',
                                  'source':'14_day_cadence_estimate'}


def test_round_audit_rejects_wrong_account_before_reading_questions(monkeypatch):
    monkeypatch.setenv('METACULUS_BOT_ID', '310211')
    with pytest.raises(RuntimeError, match='BOT_ID_MISMATCH'):
        minibench.audit(SimpleNamespace(identity=lambda: {'id': 99}), NOW)


def test_all_sixty_minibench_questions_outrank_saved_practice_answer():
    from datetime import timedelta
    from metaculus_bot.runner import question_order
    questions=[SimpleNamespace(id_of_question=i,close_time=NOW+timedelta(hours=3)) for i in range(1,61)]
    practice=SimpleNamespace(id_of_question=100,close_time=NOW+timedelta(hours=1))
    memberships={i:{1} for i in range(1,61)};memberships[100]={2}
    result=question_order([practice]+questions,memberships,set(),{100},NOW,priorities={1:0,2:3})
    assert [q.id_of_question for q in result]==list(range(1,61))+[100]


def test_published_next_round_and_urgent_open_question(monkeypatch):
    from datetime import timedelta
    monkeypatch.setenv('METACULUS_BOT_ID','310211')
    q=SimpleNamespace(id_of_question=1,open_time=None,close_time=NOW+timedelta(minutes=30),
                      state=SimpleNamespace(value='open'))
    monkeypatch.setattr(minibench,'all_questions',lambda p:[q])
    class API:
        def identity(self):return {'id':310211}
        def own_forecasts(self,ids):return set()
        def get(self,path,**kwargs):
            if path=='projects/minibenches/':
                return [{'id':1,'start_date':'2026-10-19'}, {'id':2,'start_date':'2026-11-02'}]
            return {'results':[{}],'next':None}
    result=minibench.audit(API(),NOW)
    assert result['next_round']=={'id':2,'start_date':'2026-11-02','source':'published_catalog'}
    assert result['rounds'][0]['urgent_unsubmitted_question_ids']==[1]
    assert result['rounds'][0]['counts']=={'open_unsubmitted':1}


def test_audit_warns_about_urgent_questions_without_forecasting(monkeypatch,tmp_path,capsys):
    monkeypatch.setenv('METACULUS_TOKEN','test-token')
    monkeypatch.delenv('GITHUB_STEP_SUMMARY',raising=False)
    monkeypatch.setattr('sys.argv',['audit','--out',str(tmp_path/'coverage.json')])
    monkeypatch.setattr(minibench,'audit',lambda api:{'rounds':[{'urgent_unsubmitted_question_ids':[1]}]})
    minibench.main()
    assert '::warning::MiniBench has 1 unanswered questions' in capsys.readouterr().out
