import copy
import json
from collections import Counter
from unittest.mock import patch

import pytest

from market_research import depth_shadow as d


def state():return d.initialize({'mode':'paper'},3500)


def candidate(**change):
    return {'fixture_id':'game','provider':'kalshi','contract_id':'A:yes','symbol':'A','event_id':'e',
        'side':'yes','identity_verified':True,'seven_model_verified':True,'forecast_id':'forecast',
        'published_at':7100,'game_start':5400,'forecast_end':12000,'signal_at':7200,
        'signal_quote_at':7199,'signal_ask':'.59','p01':'.60','final_p01':'.80',**change}


def metadata(**change):
    return {'price_ranges':[{'start':'0','end':'1','step':'.01'}],
        'quantity_step':'1','minimum_quantity':'1','market_close':15000,'fee_coefficient':'.07',**change}


def book(at=7260,**change):
    return {'asks':[['.58','10']],'bids':[['.57','10']],'open':True,
        'requested_at':at-1,'received_at':at,'source_at':None,**change}


def intent(s=None,c=None,m=None,b=None):
    s=state() if s is None else s
    d.choose(s,[{'candidate':c or candidate(),'metadata':m or metadata()}],7200,lambda _:b or book(),lambda:7260)
    return s


def test_forward_only_activation_and_separate_cash():
    parent={'mode':'paper','orders':[{'existing':'untouched'}]};before=copy.deepcopy(parent['orders'])
    s=d.initialize(parent,3500)
    assert d.initialize(parent,4000) is s and s['first_signal_at']==3600
    assert parent['orders']==before and d.cash(s)==100
    with pytest.raises(ValueError):d.initialize({'mode':'live'},3500)
    with pytest.raises(ValueError):d.initialize({'mode':'paper','tight_depth_shadow':{'schema':2}},3500)
    assert d.choose(s,[],0,lambda _:book(),lambda:1)=={}


def test_actual_depth_fill_after_latency_and_no_double_fills():
    s=intent();o=s['orders'][0];assert o['limit_price']=='.59' or o['limit_price']=='0.59'
    initial=d.cash(s);assert initial<100
    d.arrive(o,book(7262),7262)
    assert o['filled']=='1' and o['entry_cost']=='0.58' and o['entry_fee']=='0.02'
    assert o['observed_arrival_latency_seconds']==2 and d.cash(s)==d.amount('99.4')
    saved=copy.deepcopy(o);d.arrive(o,book(7263),7263);assert o==saved
    assert d.trades(s)[0]['decision_at']==7262
    o.update(payout='1',settlement_at=8000)
    report=d.report(s,{},8000)
    assert report['net_pnl_usd']==pytest.approx(.4) and report['live_orders']==0
    assert report['wins']==1 and d.cash(s)==d.amount('100.4')


def test_partial_ioc_cancels_residual_and_charges_each_observed_level():
    s=intent(m=metadata(quantity_step='.01',minimum_quantity='.01'));o=s['orders'][0]
    d.arrive(o,book(7262,asks=[['.58','.50'],['.59','.25'],['.60','100']]),7262)
    assert o['filled']=='0.75' and len(o['fills'])==2
    assert o['entry_cost']=='0.4375' and o['reserved']=='0'
    assert d.report(s,{'forecast':{'at':7263,'bid':'.57'}},7263)['partial_fills']==1
    before=o['filled'];d.arrive(o,book(7264),7264);assert o['filled']==before


def test_no_fill_when_price_moves_above_cap_or_equals_p01():
    s=intent();o=s['orders'][0]
    d.arrive(o,book(7262,asks=[['.60','100']]),7262)
    assert o['filled']=='0' and d.cash(s)==100
    assert d.report(s,{},7263)['unfilled_intents']==1


def test_early_stale_and_unobserved_arrivals_never_fill():
    s=intent();o=s['orders'][0]
    d.arrive(o,book(),7260.5);assert o['state']=='awaiting_arrival'
    with pytest.raises(ValueError,match='PRE_DECISION'):
        d.arrive(o,book(7262,requested_at=7260),7262)
    with pytest.raises(ValueError,match='STALE'):
        d.arrive(o,book(7261,requested_at=7250),7262)
    d.arrive(o,{},7280)
    assert o['state']=='arrival_unobserved' and d.cash(s)==100
    assert d.report(s,{},7280)['unobserved_arrivals']==1


def test_first_choice_veto_never_replaced_by_later_better_looking_candidate():
    s=state();a=candidate(signal_ask='.56',contract_id='A:yes');b=candidate(contract_id='B:yes')
    d.choose(s,[{'candidate':a,'metadata':metadata()},{'candidate':b,'metadata':metadata()}],7200,lambda _:book(),lambda:7260)
    assert not s['orders'] and s['first_choices']['game']['contract_id']=='A:yes'
    assert s['coverage']['7200']['first_choice_vetoed_outside_2c']==1
    d.choose(s,[{'candidate':b,'metadata':metadata()}],7200,lambda _:book(),lambda:7260)
    assert not s['orders']


@pytest.mark.parametrize('change',[{'published_at':7201},{'game_start':1000},{'final_p01':'.5'},
    {'identity_verified':False},{'seven_model_verified':False},{'signal_quote_at':7000}])
def test_invalid_provenance_signal_or_already_started_game_excluded(change):
    s=intent(c=candidate(**change));assert not s['orders']
    assert s['coverage']['7200']['unavailable_or_ineligible_candidate']==1


def test_parent_region_exclusion_and_insufficient_cash_are_visible():
    assert not intent(c=candidate(signal_ask='.40'))['orders']
    s=state();s['initial_cash']='.1';intent(s=s)
    assert not s['orders'] and 'game' in s['first_choices']
    assert s['coverage']['7200']['first_choice_rejected_no_replacement']==1


@pytest.mark.parametrize('m',[
    metadata(price_ranges=[{'start':'0','end':'1','step':'.03'}]),
    metadata(price_ranges=[{'start':'0','end':'1','step':'0'}]),
    metadata(price_ranges=[]),metadata(quantity_step='0'),metadata(minimum_quantity='100'),
    metadata(market_close=7200)])
def test_exchange_constraints_reject_instead_of_rounding_up(m):
    assert not intent(m=m)['orders']


def test_opposite_side_tick_lattice_is_mirrored():
    s=intent(c=candidate(side='no',contract_id='A:no'),
        m=metadata(price_ranges=[{'start':'.35','end':'.45','step':'.02'}]))
    assert s['orders'][0]['limit_price']=='0.59'


@pytest.mark.parametrize('change',[
    {'received_at':float('nan')},{'source_at':'bad'},{'source_at':8000},{'open':False},
    {'asks':[]},{'bids':[]},{'asks':[['.58','-1']]},{'asks':[['.58','1'],['.58','2']]},
    {'bids':[['.59','1']]},{'requested_at':7200}])
def test_bad_depth_is_not_executable(change):
    with pytest.raises(ValueError):d._book(book(**change),7260)


def test_market_closing_before_arrival_cancels_and_cash_reserve_is_enforced():
    s=intent();o=s['orders'][0];o['metadata']['market_close']=7261
    d.arrive(o,book(7262),7262);assert o['state']=='canceled_closed'
    s=intent();o=s['orders'][0];o['reserved']='.1'
    with pytest.raises(ValueError,match='CASH_RESERVE'):d.arrive(o,book(7262),7262)
    assert o['filled']=='0'


def test_no_optimistic_bid_mark_when_receipt_missing():
    s=intent();o=s['orders'][0];d.arrive(o,book(7262),7262)
    report=d.report(s,{},7263)
    assert report['equity']['unmarked_positions']==1
    assert 'max_sampled_bid_drawdown_usd' not in report['equity']


def test_worker_depth_comparison_survives_sqlite_restore_without_legacy_edits(tmp_path):
    from market_research.game_p01_limit import Journal
    from market_research.game_p01_limit_worker import Worker
    from market_research import game_p01_limit_worker as worker
    path=tmp_path/'journal.sqlite'
    with patch.object(worker.time,'time',return_value=3500):
        journal=Journal(path,'paper',now=3500);w=Worker(journal,{'kalshi'})
    try:
        c=candidate();prepared={'candidate':c,'metadata':metadata()}
        with patch.object(worker,'source_rows',return_value=[]),patch.object(worker,'signals',return_value=([c],Counter())), \
             patch.object(worker,'prepare',return_value=(prepared,None)), \
             patch.object(worker,'current_plan',return_value=(None,'NO_LEGACY_INTENT')), \
             patch.object(worker.books,'book',return_value=book()),patch.object(worker.time,'time',return_value=7260):
            w.enter_hour(7200)
        assert journal.state['orders']==[] and len(w.depth_state['orders'])==1
        with patch.object(worker.books,'book',return_value=book(7262)),patch.object(worker.time,'time',return_value=7262):
            w.depth_marks=w.reconcile_depth(7262,False)
            w.report(tmp_path,w.depth_marks)
        assert json.loads((tmp_path/'depth-shadow-stats.json').read_text())['entries']==1
        assert json.loads((tmp_path/'stats.json').read_text())['entries']==0
    finally:journal.close()
    j2=Journal(path,'paper',now=8000)
    try:
        w2=Worker(j2,{'kalshi'})
        assert w2.depth_state['activation_at']==3500 and w2.depth_state['orders'][0]['filled']=='1'
        assert j2.state['orders']==[]
    finally:j2.close()
