import copy
import gzip
import json
import hashlib
from unittest.mock import patch

import pytest

from market_research import sports_recovery as r
from market_research import game_p01_paper as p


def source(hour,catalog):
    if hour==3600:raise RuntimeError('MISSING_POINT_IN_TIME_FEED:games-kalshi-0')
    return [{'publication_ts':7200}],{'games-kalshi-0':1}


def state():return p.fresh_state(3500)


def test_explicit_epoch_records_gap_and_preserves_existing_ledger():
    s=state();s['portfolios']['example']={'trades':[{'fixture_id':'old','payout':'1','entry_fee':'.02'}]}
    before=copy.deepcopy(s['portfolios']);digest=r.ledger_digest(s)
    gap=r.resume_forward(s,9000,{},source)
    assert s['next_hour']==10800 and s['active_observation_epoch']==10800
    assert s['portfolios']==before and r.ledger_digest(s)==digest
    assert gap['start']==3600 and gap['unobserved_hours']==2 and gap['status']=='unobserved_not_backfilled'
    r.validate_recovery(s)


def test_does_not_skip_recoverable_history():
    s=state();before=copy.deepcopy(s)
    with pytest.raises(ValueError,match='EVIDENCE_AVAILABLE'):
        r.resume_forward(s,9000,{},lambda *_:([],{'feed':1}))
    assert s==before


@pytest.mark.parametrize('error',['UNTRUSTED_ARTIFACT_PROVENANCE','GITHUB_READ_FAILED'])
def test_other_failures_never_authorize_gap_recovery(error):
    def fail(*_):raise RuntimeError(error)
    with pytest.raises(RuntimeError,match=error):r.resume_forward(state(),9000,{},fail)


def test_no_fresh_publication_or_future_rows_cannot_start_epoch():
    for value in [([],{}),([{'publication_ts':10000}],{'feed':1})]:
        def snapshots(hour,_):
            if hour==3600:raise RuntimeError('STALE_POINT_IN_TIME_FEED:feed')
            return value
        s=state();before=copy.deepcopy(s)
        with pytest.raises(ValueError):r.resume_forward(s,9000,{},snapshots)
        assert s==before


@pytest.mark.parametrize('now',[-1,1.2,3600])
def test_invalid_or_current_recovery_time(now):
    with pytest.raises(ValueError):r.resume_forward(state(),now,{},source)


def test_recovery_new_games_only_and_separate_cohorts():
    s=state();r.resume_forward(s,9000,{},source)
    rows=[{'game_start':p.iso(10000)},{'game_start':p.iso(10800)},{'game_start':p.iso(12000)}]
    assert r.epoch_rows(s,rows,p.stamp)==(rows[1:],1)
    assert r.epoch_rows(state(),rows,p.stamp)==(rows,0)
    portfolio={'trades':[{'fixture_id':'old'},{'fixture_id':'new'}]}
    s['portfolios']={'sample':portfolio};r.tag_new_trades(s,{'sample':1})
    assert 'observation_epoch' not in portfolio['trades'][0]
    assert r.by_epoch(portfolio,len)=={'original_observed':1,'10800':1}


@pytest.mark.parametrize('change',[{'status':'complete'},{'start':3601},{'end_exclusive':9000},
    {'source_artifact_ids':{}},{'preserved_ledger_sha256':'bad'},{'recorded_at':11000}])
def test_forged_gap_metadata_fails_checkpoint_validation(change):
    s=state();r.resume_forward(s,9000,{},source);s['observation_gaps'][0].update(change)
    with pytest.raises(ValueError):p.validate_state(s)


def test_epoch_must_match_recorded_gap():
    s=state();r.resume_forward(s,9000,{},source);s['active_observation_epoch']=7200
    with pytest.raises(ValueError):r.validate_recovery(s)


def test_run_explicit_recovery_saves_gap_and_does_not_fabricate_trades(tmp_path):
    s=state()
    with patch.object(p,'restore',return_value=s),patch.object(p,'artifacts',return_value=[]), \
         patch.object(p,'snapshots_at',side_effect=source),patch.object(p,'update_settlements',return_value={}):
        p.run(tmp_path,now=9000,resume_after_gap=True)
    saved=json.loads(gzip.decompress((tmp_path/'paper-state.json.gz').read_bytes()))
    assert saved['next_hour']==10800 and not saved['catchup_pending']
    assert len(saved['observation_gaps'])==1 and not any(v['trades'] for v in saved['portfolios'].values())
    report=json.loads((tmp_path/'comparison-stats.json').read_text())
    assert not report['continuous_observation_coverage'] and report['active_observation_epoch']==10800
    assert 'Coverage is discontinuous' in (tmp_path/'comparison-summary.md').read_text()


def test_artifact_hash_and_original_workflow_sha_must_match():
    blob=b'original archive'
    with patch.object(p,'github',return_value=blob),patch.object(p,'compressed_json',return_value={'ok':True}):
        assert p.read_artifact({'id':1,'digest':'sha256:'+hashlib.sha256(blob).hexdigest()},'snapshot.json.gz')=={'ok':True}
        with pytest.raises(ValueError,match='DIGEST'):
            p.read_artifact({'id':1,'digest':'sha256:wrong'},'snapshot.json.gz')
    run={'path':'workflow','head_branch':'main','repository':{'full_name':p.REPO},'event':'schedule','head_sha':'same'}
    with patch.object(p,'github',return_value=run):
        p.validate_run({'workflow_run':{'id':1,'head_sha':'same'}},'workflow')
        with pytest.raises(ValueError,match='PROVENANCE'):
            p.validate_run({'workflow_run':{'id':1,'head_sha':'changed'}},'workflow')


def test_forecast_input_generation_and_publication_order_is_enforced():
    row={'schema_version':3,'models':sorted(p.MODELS),'weight_mode':'auto',
        'weight_policy':'rolling_loss_selection_v2','generated_at':p.iso(3400),'input_cutoff':p.iso(3000),
        'provider':'kalshi','contract_id':'a','predictions':[]}
    snapshot={'schema_version':'quantura-public-screener-v1','feed':'feed','published_at':p.iso(3450),'data':{'items':[row]}}
    artifact={'id':1,'created_at':p.iso(3500),'workflow_run':{'id':1,'head_sha':'same'}}
    with patch.object(p,'FEEDS',['feed']),patch.object(p,'validate_run'),patch.object(p,'read_artifact',return_value=snapshot):
        assert p.snapshots_at(3600,{'feed':[artifact]})[0][0]['publication_ts']==3500
        row['input_cutoff']=p.iso(3550)
        with pytest.raises(ValueError,match='TIMESTAMP_ORDER'):p.snapshots_at(3600,{'feed':[artifact]})


def test_trade_epoch_cannot_be_relabelled_to_hide_an_old_game():
    s=state();r.resume_forward(s,9000,{},source)
    s['portfolios']['example']={'trades':[{'observation_epoch':10800,'game_start':5000,'signal_at':12000,'decision_at':12060}]}
    with pytest.raises(ValueError,match='TRADE_OBSERVATION'):r.validate_recovery(s)
