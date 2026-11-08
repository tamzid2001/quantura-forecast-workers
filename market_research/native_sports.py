"""Read-only sports discovery/history without the hosted Quantura gateway.

The contracts retain the website adapter's economic sides and completed-hour
conventions. Only fixed public GET endpoints are reachable; no broker keys,
order methods, forward filling, synthetic prices or retrospective publication.
"""
from __future__ import annotations

import math
import re
import time
from urllib.parse import quote, urlencode

from .engine import Quote, iso, stamp
from .game_p01_month import game_series, moneyline
from .game_p01_paper import KALSHI, POLY, candle_price, public_json
from .provider import KalshiProvider, QuanturaProvider


def identifier(value, maximum=220):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,"+str(maximum)+r"}", value):
        raise ValueError("INVALID_NATIVE_IDENTIFIER")
    return value


def probability(value):
    if isinstance(value, bool):
        raise ValueError("INVALID_NATIVE_PRICE")
    try:
        price = float(value)
    except (TypeError, ValueError):
        raise ValueError("INVALID_NATIVE_PRICE") from None
    if not math.isfinite(price) or not 0 <= price <= 1:
        raise ValueError("INVALID_NATIVE_PRICE")
    return price


def teams(event):
    names = {}
    for market in event.get("markets", []):
        for side in market.get("marketSides", []):
            team = side.get("team") or {}
            name = team.get("safeName") or team.get("name")
            if team.get("ordering") in {"home", "away"} and name:
                names[team["ordering"]] = name
    return names.get("home"), names.get("away")


def polymarket_contracts(events, mode, now):
    result = []
    for event in events:
        home, away = teams(event)
        for market in event.get("markets", []):
            market_type = market.get("sportsMarketTypeV2") or market.get("sportsMarketType") or ""
            if not moneyline(market_type) or market.get("closed") is not False or market.get("active") is not True:
                continue
            raw_start = market.get("gameStartTime") or event.get("startTime")
            if not raw_start:
                continue
            start = stamp(raw_start)
            if (mode == "premarket" and start <= now) or (mode == "live" and not start <= now < start+18*3600):
                continue
            symbol = identifier(market.get("slug"))
            for side in market.get("marketSides", []):
                if type(side.get("long")) is not bool or not side.get("id"):
                    continue
                result.append({"source": "polymarket_us", "contractId": str(side['id']),
                    "providerSymbol": symbol, "eventId": str(event['id']), "eventSlug": event.get('slug'),
                    "marketId": str(market['id']), "eventTitle": event.get('title', symbol),
                    "marketTitle": market.get('title') or market.get('question', ''), "marketType": market_type,
                    "outcome": side.get('description') or (side.get('team') or {}).get('name') or ("Long" if side['long'] else "Short"),
                    "side": "long" if side['long'] else "short", "eventStart": iso(start),
                    "homeTeam": home, "awayTeam": away, "league": event.get('seriesSlug', ''), "sport": "Sports",
                    "status": "open", "live": start <= now,
                    "scheduleSource": "Polymarket US market gameStartTime" if market.get('gameStartTime') else "Polymarket US event startTime",
                    "scheduleVerifiedAt": iso(now)})
    return result


class NativePolymarketSports(QuanturaProvider):
    def discover(self, mode, max_pages=20, start_cursor="0"):
        if mode not in {'premarket', 'live'} or type(max_pages) is not int or not 1 <= max_pages <= 100:
            raise ValueError('INVALID_NATIVE_DISCOVERY')
        if not str(start_cursor).isdigit() or not 0 <= int(start_cursor) <= 100000:
            raise ValueError('INVALID_DISCOVERY_CURSOR')
        offset, scanned, contracts = int(start_cursor), 0, {}
        for _ in range(max_pages):
            params = {'limit': 100, 'offset': offset, 'closed': 'false', 'categories': 'sports',
                      'orderBy': 'id', 'orderDirection': 'desc'}
            events = public_json(POLY+'/v1/events?'+urlencode(params)).get('events')
            if not isinstance(events, list):
                raise ValueError('INVALID_NATIVE_EVENT_PAGE')
            scanned += len(events)
            for c in polymarket_contracts(events, mode, int(time.time())):
                contracts[c['contractId']] = c
            offset += 100
            if len(events) < 100:
                offset = None
                break
        return list(contracts.values()), {'events_scanned': scanned, 'next_cursor': str(offset) if offset is not None else None,
            'discovery_truncated': offset is not None, 'contracts_discovered': len(contracts), 'data_transport': 'native_public_get'}

    def hourly_history(self, contract, start, end):
        _history_window(start, end)
        symbol = identifier(contract['providerSymbol'])
        if contract.get('side') not in {'long', 'short'}:
            raise ValueError('INVALID_ECONOMIC_SIDE')
        # Same raw display-quote history endpoint as the website adapter. A
        # genuine last point in a completed bucket becomes its hourly sample.
        params = {'symbol': symbol, 'timestamp.startTimestamp': int(start), 'timestamp.endTimestamp': int(end), 'fidelity': 1}
        data = public_json(POLY+'/v1/price-history?'+urlencode(params))
        raw = data.get('history')
        if not isinstance(raw, list):
            raise ValueError('INVALID_NATIVE_HISTORY')
        key = 'longPrice' if contract['side'] == 'long' else 'shortPrice'
        buckets, seen = {}, {}
        for row in raw:
            at = float(row['timestamp'])
            if not math.isfinite(at):
                raise ValueError('INVALID_NATIVE_TIMESTAMP')
            value = probability(row[key])
            if at in seen and seen[at] != value:
                raise ValueError('CONFLICTING_NATIVE_OBSERVATIONS')
            seen[at] = value
            close = (int(at)//3600+1)*3600
            if start <= at and start < close <= end and (close not in buckets or at > buckets[close][0]):
                buckets[close] = (at, value)
        return [Quote(close, value, value) for close, (_, value) in sorted(buckets.items())]


def _history_window(start, end):
    if not all(type(v) in (int, float) and math.isfinite(v) for v in (start, end)) or not 0 <= start < end or end-start > 31*86400:
        raise ValueError('INVALID_NATIVE_HISTORY_WINDOW')


class NativeKalshiSports(KalshiProvider):
    def _schedule_request(self,url):
        legacy='https://api.elections.kalshi.com/trade-api/v2/'
        if not url.startswith(legacy):
            raise ValueError('UNAPPROVED_NATIVE_SCHEDULE')
        return public_json(KALSHI+'/'+url[len(legacy):])

    def request(self, path, body=None):
        # Keep the mature series cursor behavior while replacing its two
        # read-only gateway requests with equivalent direct provider GETs.
        from urllib.parse import parse_qs, urlsplit
        parsed = urlsplit(path)
        if body is not None or parsed.path != '/api/sports/prediction-markets/research-catalog' or parsed.scheme or parsed.netloc:
            raise ValueError('UNAPPROVED_NATIVE_READ')
        params = {k:v[0] for k,v in parse_qs(parsed.query).items()}
        if not params.get('series_ticker'):
            rows = public_json(KALSHI+'/series?category=Sports').get('series')
            if not isinstance(rows, list):
                raise ValueError('INVALID_NATIVE_SERIES')
            series = [s for s in rows if s.get('category') == 'Sports' or 'Sports' in s.get('categories', [])]
            return {'series': [{**s, 'game_candidate': game_series(s['ticker'], s.get('title',''))} for s in series]}
        series = identifier(params['series_ticker'], 120)
        mode = params.get('mode', 'premarket')
        if mode not in {'premarket','live'}:
            raise ValueError('NATIVE_PREGAME_ONLY')
        cursor = params.get('cursor', '')
        if len(cursor)>2048 or any(ord(c)<32 for c in cursor):
            raise ValueError('INVALID_DISCOVERY_CURSOR')
        query = {'series_ticker': series,'status':'open','limit':200,'with_nested_markets':'true','with_milestones':'true','cursor':cursor}
        data = public_json(KALSHI+'/events?'+urlencode(query))
        events = data.get('events')
        if not isinstance(events,list):
            raise ValueError('INVALID_NATIVE_EVENT_PAGE')
        items=[];now=int(time.time())
        for event in events:
            if event.get('series_ticker')!=series:
                raise ValueError('NATIVE_SERIES_IDENTITY_MISMATCH')
            event_id=identifier(event['event_ticker'],180)
            starts={stamp(m['start_date']) for m in data.get('milestones', []) if m.get('start_date') and
                    event_id in [*m.get('primary_event_tickers',[]),*m.get('related_event_tickers',[])]}
            if len(starts)!=1:
                continue
            start=starts.pop()
            if (mode=='premarket' and start<=now) or (mode=='live' and not start<=now<start+18*3600):
                continue
            for market in event.get('markets',[]):
                symbol=identifier(market['ticker'],180)
                if market.get('market_type')!='binary' or market.get('event_ticker')!=event_id or market.get('status') not in {'active','initialized'}:
                    continue
                for side in ('yes','no'):
                    items.append({'source':'kalshi','providerSymbol':symbol,'marketId':symbol,'contractId':symbol+':'+side,
                        'eventId':event_id,'eventTitle':event.get('title',event_id),'marketTitle':market.get('title',''),
                        'outcome':market.get(side+'_sub_title') or side.title(),'side':side,'eventStart':iso(start),
                        'sport':'Sports','league':series,'status':'open','live':start<=now,
                        'scheduleSource':'Kalshi linked sports milestone','scheduleVerifiedAt':iso(now)})
        next_cursor=data.get('cursor') or None
        if next_cursor and next_cursor==cursor:
            raise ValueError('REPEATED_CURSOR')
        return {'items':items,'events_scanned':len(events),'next_cursor':next_cursor}

    def hourly_history(self, contract, start, end):
        _history_window(start,end)
        symbol=identifier(contract['providerSymbol'],180)
        side=contract.get('side')
        if side not in {'yes','no'} or contract.get('contractId')!=symbol+':'+side:
            raise ValueError('INVALID_ECONOMIC_SIDE')
        params={'start_ts':int(start),'end_ts':int(end),'period_interval':60}
        data=public_json(KALSHI+'/series/'+quote(symbol.split('-',1)[0],safe='')+'/markets/'+quote(symbol,safe='')+'/candlesticks?'+urlencode(params))
        if data.get('ticker')!=symbol or not isinstance(data.get('candlesticks'),list):
            raise ValueError('NATIVE_HISTORY_IDENTITY_MISMATCH')
        values={}
        for row in data['candlesticks']:
            at=row['end_period_ts']
            if type(at) is not int or at%3600:
                raise ValueError('INVALID_NATIVE_HOUR_BOUNDARY')
            price=candle_price(row.get('yes_ask' if side=='yes' else 'yes_bid',{}))
            if price is None:
                continue
            price=probability(price if side=='yes' else 1-price)
            if at in values and values[at]!=price:
                raise ValueError('CONFLICTING_NATIVE_OBSERVATIONS')
            if start<at<=end:
                values[at]=price
        return [Quote(at,value,value) for at,value in sorted(values.items())]


def provider(source):
    if source=='kalshi':
        return NativeKalshiSports()
    if source=='polymarket_us':
        return NativePolymarketSports()
    raise ValueError('UNSUPPORTED_NATIVE_PROVIDER')
