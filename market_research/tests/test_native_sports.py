"""Original synthetic API-shape fixtures; no real provider/model execution."""
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from market_research import native_sports as n
from market_research import pregame_screener as pregame


def poly_event():
    return {'id':'1','slug':'home-away','title':'Home vs Away','startTime':n.iso(10800),
        'seriesSlug':'example','markets':[{'id':'m','slug':'home-away-market','active':True,'closed':False,
            'sportsMarketTypeV2':'SPORTS_MARKET_TYPE_MONEYLINE','marketSides':[
                {'id':'long-id','long':True,'description':'Home','team':{'name':'Home','ordering':'home'}},
                {'id':'short-id','long':False,'description':'Away','team':{'name':'Away','ordering':'away'}}]}]}


def kalshi_page():
    return {'cursor':'','milestones':[{'start_date':n.iso(10800),'primary_event_tickers':['KXGAME-E']}],
        'events':[{'event_ticker':'KXGAME-E','series_ticker':'KXGAME','title':'Home vs Away','markets':[
            {'ticker':'KXGAME-E-HOME','event_ticker':'KXGAME-E','market_type':'binary','status':'active',
             'yes_sub_title':'Home','no_sub_title':'Not Home'}]}]}


def test_poly_side_identity_teams_and_missing_start_are_not_invented():
    e=poly_event();rows=n.polymarket_contracts([e],'premarket',7200)
    assert [(r['contractId'],r['side']) for r in rows]==[('long-id','long'),('short-id','short')]
    assert rows[0]['homeTeam']=='Home' and rows[0]['awayTeam']=='Away'
    assert rows[0]['eventStart']==n.iso(10800)
    assert n.polymarket_contracts([e],'live',7200)==[]
    assert n.polymarket_contracts([e],'premarket',10800)==[]
    assert len(n.polymarket_contracts([e],'live',10801))==2
    assert n.polymarket_contracts([e],'live',10800+19*3600)==[]
    e.pop('startTime');assert n.polymarket_contracts([e],'premarket',7200)==[]


@pytest.mark.parametrize('change',[{'sportsMarketTypeV2':'SPORTS_MARKET_TYPE_SPREAD'},{'closed':True},{'active':False}])
def test_poly_excludes_non_games_or_closed_markets(change):
    e=poly_event();e['markets'][0].update(change)
    assert n.polymarket_contracts([e],'premarket',7200)==[]


def test_poly_skips_ambiguous_side_and_uses_market_start():
    e=poly_event();m=e['markets'][0];m['gameStartTime']=n.iso(14000);m['marketSides'].append({'id':'unknown'})
    assert len(n.polymarket_contracts([e],'premarket',7200))==2
    assert n.polymarket_contracts([e],'premarket',7200)[0]['eventStart']==n.iso(14000)


def test_native_poly_discovery_pagination_uses_only_fixed_public_get():
    e=poly_event()
    with patch.object(n,'public_json',side_effect=[{'events':[e]*100},{'events':[]}]) as get,patch.object(n.time,'time',return_value=7200):
        rows,coverage=n.NativePolymarketSports().discover('premarket',2)
    assert len(rows)==2 and coverage['next_cursor'] is None and coverage['events_scanned']==100
    assert all(c.args[0].startswith(n.POLY+'/v1/events?') for c in get.call_args_list)
    assert 'offset=100' in get.call_args_list[1].args[0]
    with patch.object(n,'public_json',return_value={'events':[e]*100}),patch.object(n.time,'time',return_value=7200):
        assert n.NativePolymarketSports().discover('premarket',1)[1]['discovery_truncated']


@pytest.mark.parametrize('args',[('historical',1,'0'),('live',0,'0'),('live',101,'0'),('live',1,'-1'),('live',1,'100001')])
def test_native_poly_discovery_invalid_bounds(args):
    with pytest.raises(ValueError):n.NativePolymarketSports().discover(*args)


def test_native_poly_rejects_bad_page():
    with patch.object(n,'public_json',return_value={'events':None}),pytest.raises(ValueError):
        n.NativePolymarketSports().discover('live')


def test_poly_hourly_has_no_future_bucket_or_forward_fill():
    raw={'history':[{'timestamp':3599,'longPrice':'.4','shortPrice':'.6'},
        {'timestamp':100,'longPrice':'.3','shortPrice':'.7'},
        {'timestamp':7199,'longPrice':'.5','shortPrice':'.5'},
        {'timestamp':7200,'longPrice':'.8','shortPrice':'.2'},
        {'timestamp':14399,'longPrice':'.9','shortPrice':'.1'}]}
    c={'providerSymbol':'home-away','side':'short'}
    with patch.object(n,'public_json',return_value=raw):rows=n.NativePolymarketSports().hourly_history(c,0,7200)
    assert [(q.timestamp,q.ask) for q in rows]==[(3600,.6),(7200,.5)]
    # The point at exactly 7200 belongs to the following, uncompleted bucket.
    assert all(q.bid==q.ask for q in rows)


@pytest.mark.parametrize('raw',[{'history':None},{'history':[{'timestamp':float('nan'),'longPrice':'.4'}]},
    {'history':[{'timestamp':1,'longPrice':'.4'},{'timestamp':1,'longPrice':'.5'}]}])
def test_poly_invalid_or_conflicting_observations_fail(raw):
    with patch.object(n,'public_json',return_value=raw),pytest.raises(ValueError):
        n.NativePolymarketSports().hourly_history({'providerSymbol':'a','side':'long'},0,7200)


def test_native_kalshi_keeps_milestone_schedule_and_series_cursor():
    inventory={'series':[{'ticker':'KXGAME','category':'Sports','title':'Game'},
        {'ticker':'KXOTHERGAME','categories':['Sports'],'title':'Game'},
        {'ticker':'KXFAKEGAME','category':'Other','title':'Other'}]}
    with patch.object(n,'public_json',side_effect=[inventory,kalshi_page()]) as get,patch.object(n.time,'time',return_value=7200):
        rows,coverage=n.NativeKalshiSports('KXGAME').discover('premarket',1)
    assert [r['side'] for r in rows]==['yes','no'] and rows[0]['eventStart']==n.iso(10800)
    assert rows[0]['contractId']=='KXGAME-E-HOME:yes'
    assert coverage['next_cursor'] is None
    assert all(c.args[0].startswith(n.KALSHI+'/') for c in get.call_args_list)


@pytest.mark.parametrize('change',[{'milestones':[]},{'events':[]},
    {'milestones':[{'start_date':n.iso(10800),'primary_event_tickers':['KXGAME-E']},
                   {'start_date':n.iso(14400),'related_event_tickers':['KXGAME-E']}]}])
def test_native_kalshi_no_missing_or_conflicting_schedule(change):
    page=kalshi_page();page.update(change)
    with patch.object(n,'public_json',return_value=page),patch.object(n.time,'time',return_value=7200):
        assert n.NativeKalshiSports().request('/api/sports/prediction-markets/research-catalog?series_ticker=KXGAME')['items']==[]


def test_kalshi_rejects_wrong_series_repeated_cursor_and_invalid_pages():
    page=kalshi_page();page['events'][0]['series_ticker']='WRONG'
    for data in [page,{'events':None}, {'events':[],'cursor':'repeat'}]:
        with patch.object(n,'public_json',return_value=data),pytest.raises(ValueError):
            n.NativeKalshiSports().request('/api/sports/prediction-markets/research-catalog?series_ticker=KXGAME&cursor=repeat')
    with patch.object(n,'public_json',return_value={'series':None}),pytest.raises(ValueError):
        n.NativeKalshiSports().request('/api/sports/prediction-markets/research-catalog?source=kalshi')


@pytest.mark.parametrize('path,body',[
    ('https://evil.example/api/sports/prediction-markets/research-catalog',None),('/orders',None),
    ('/api/sports/prediction-markets/research-catalog',{}),
    ('/api/sports/prediction-markets/research-catalog?series_ticker=KXGAME&mode=historical',None),
    ('/api/sports/prediction-markets/research-catalog?series_ticker=KXGAME&cursor='+('x'*2049),None)])
def test_native_get_allowlist(path,body):
    with pytest.raises(ValueError):n.NativeKalshiSports().request(path,body)


def test_kalshi_completed_hours_preserve_no_side_ask():
    data={'ticker':'KXGAME-E-HOME','candlesticks':[
        {'end_period_ts':3600,'yes_bid':{'close_dollars':'.4'},'yes_ask':{'close_dollars':'.5'}},
        {'end_period_ts':7200,'yes_bid':{},'yes_ask':{}},
        {'end_period_ts':10800,'yes_bid':{'close_dollars':'.8'},'yes_ask':{'close_dollars':'.9'}}]}
    with patch.object(n,'public_json',return_value=data):
        rows=n.NativeKalshiSports().hourly_history({'providerSymbol':'KXGAME-E-HOME','side':'no','contractId':'KXGAME-E-HOME:no'},0,7200)
    assert [(q.timestamp,q.ask) for q in rows]==[(3600,.6)]


@pytest.mark.parametrize('data',[
    {'ticker':'wrong','candlesticks':[]},
    {'ticker':'A','candlesticks':[{'end_period_ts':3599}]},
    {'ticker':'A','candlesticks':[{'end_period_ts':3600,'yes_ask':{'close_dollars':'.2'}},
                                {'end_period_ts':3600,'yes_ask':{'close_dollars':'.3'}}]}])
def test_kalshi_history_rejects_identity_boundary_conflicts(data):
    with patch.object(n,'public_json',return_value=data),pytest.raises(ValueError):
        n.NativeKalshiSports().hourly_history({'providerSymbol':'A','side':'yes','contractId':'A:yes'},0,7200)


@pytest.mark.parametrize('start,end',[(1,1),(-1,3600),(0,32*86400),(0,float('nan'))])
def test_history_is_bounded(start,end):
    with pytest.raises(ValueError):n._history_window(start,end)


@pytest.mark.parametrize('value',[None,True,'nan','1.1','-0.1','bad'])
def test_invalid_native_probability(value):
    with pytest.raises(ValueError):n.probability(value)


def test_factory_identifier_and_economic_side_guards():
    assert isinstance(n.provider('kalshi'),n.NativeKalshiSports)
    assert isinstance(n.provider('polymarket_us'),n.NativePolymarketSports)
    for value in ['other',None]:
        with pytest.raises(ValueError):n.provider(value)
    for value in ['../secrets','a/b',None]:
        with pytest.raises(ValueError):n.identifier(value)
    with pytest.raises(ValueError):n.NativePolymarketSports().hourly_history({'providerSymbol':'a','side':'no'},0,3600)
    with pytest.raises(ValueError):n.NativeKalshiSports().hourly_history({'providerSymbol':'a','side':'yes','contractId':'wrong'},0,3600)
    with patch.object(n,'public_json',return_value={'ok':True}) as get:
        assert n.NativeKalshiSports()._schedule_request('https://api.elections.kalshi.com/trade-api/v2/events/A')=={'ok':True}
        assert get.call_args.args[0]==n.KALSHI+'/events/A'
    with pytest.raises(ValueError):n.NativeKalshiSports()._schedule_request('https://elsewhere.invalid/events')


def test_forward_catalog_recovery_explicit_and_not_retrospective(tmp_path):
    fake=Mock();fake.discover.return_value=([],{'events_scanned':0,'next_cursor':None})
    with patch.dict('sys.modules',{'market_research.forecast':SimpleNamespace(forecast_window=Mock())}), \
         patch.object(pregame,'previous',side_effect=ValueError('PUBLIC_SNAPSHOT_NOT_PUBLISHED')), \
         patch.object(n,'provider',return_value=fake),patch.object(pregame,'write_public') as write, \
         patch.dict('os.environ',{'QUANTURA_RESEARCH_DIR':str(tmp_path)}):
        pregame.run('kalshi',1,transport='native',recover_forward_catalog=True)
    report=json.loads((tmp_path/pregame.REPORT_FILENAME).read_text())
    assert report['catalog_recovery']=='new_forward_only_no_historical_receipts'
    assert report['data_transport']=='native' and report['successful']==0
    assert write.call_args.args[1]['items']==[]
    with pytest.raises(ValueError):pregame.run('kalshi',1,refresh_published=True,recover_forward_catalog=True)
    with pytest.raises(ValueError):pregame.run('kalshi',1,transport='unapproved')
