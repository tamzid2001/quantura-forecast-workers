"""Independent, forward-only 2c execution comparison; never submits orders.

One marketable-limit IOC proxy uses the first fresh depth sample observed after
the configured decision latency. It cannot claim an exchange fill or recover
unobserved queue priority. Its cash, intents, fills and statistics are separate
from both the frozen quote replay and the legacy passive-limit experiment.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_DOWN
import hashlib
import json
import math

from .game_p01_portfolio import amount, fee, performance, sample_equity


@dataclass(frozen=True)
class Rules:
    name: str = 'p01_hour_2c_observed_first_choice_depth_v2'
    notional: str = '1.00'
    signal_band: str = '.02'
    parent_band: str = '.10'
    max_slippage: str = '.01'
    minimum_latency_seconds: int = 1
    maximum_arrival_seconds: int = 15
    max_book_age_seconds: int = 5
    max_signal_age_seconds: int = 120

    @property
    def fingerprint(self):
        return hashlib.sha256(json.dumps(asdict(self),sort_keys=True).encode()).hexdigest()


RULES=Rules()


def _book(book,now):
    for name in ['requested_at','received_at']:
        if type(book.get(name)) not in (int,float) or not math.isfinite(book[name]):
            raise ValueError('INVALID_DEPTH_TIMESTAMP')
    source=book.get('source_at')
    if source is not None and (type(source) not in (int,float) or not math.isfinite(source)):
        raise ValueError('INVALID_DEPTH_SOURCE_TIMESTAMP')
    if (book.get('open') is not True or not 0<=now-book['received_at']<=RULES.max_book_age_seconds or
            not 0<=book['received_at']-book['requested_at']<=RULES.max_book_age_seconds or
            (book.get('source_at') is not None and not book['source_at']<=now+1)):
        raise ValueError('STALE_OR_CLOSED_DEPTH')
    if not book.get('asks') or not book.get('bids'):
        raise ValueError('TWO_SIDED_DEPTH_REQUIRED')
    for side in ['asks','bids']:
        if len({amount(p) for p,_ in book[side]})!=len(book[side]):
            raise ValueError('DUPLICATE_DEPTH_PRICE')
        if any(not 0<amount(p)<1 or amount(q)<=0 for p,q in book[side]):
            raise ValueError('INVALID_DEPTH_LEVEL')
    ask=min(amount(p) for p,_ in book['asks'])
    bid=max(amount(p) for p,_ in book['bids'])
    if bid>ask:
        raise ValueError('CROSSED_DEPTH')
    return ask,bid


def initialize(parent,now):
    if parent.get('mode')!='paper':
        raise ValueError('DEPTH_SHADOW_PAPER_ONLY')
    value=parent.setdefault('tight_depth_shadow',{'schema':1,'mode':'paper_only','rules_hash':RULES.fingerprint,
        'activation_at':now,'first_signal_at':(int(now)//3600+1)*3600,'initial_cash':'100',
        'first_choices':{},'orders':[],'coverage':{},'equity':{}})
    if value.get('schema')!=1 or value.get('mode')!='paper_only' or value.get('rules_hash')!=RULES.fingerprint:
        raise ValueError('INVALID_DEPTH_SHADOW_CHECKPOINT')
    return value


def cash(state):
    paid=sum((amount(o.get('entry_cost','0'))+amount(o.get('entry_fee','0')) for o in state['orders']),Decimal(0))
    credits=sum((amount(o['filled'])*amount(o['payout']) for o in state['orders'] if o.get('payout') is not None),Decimal(0))
    reserved=sum((amount(o['reserved']) for o in state['orders'] if o['state']=='awaiting_arrival'),Decimal(0))
    return amount(state['initial_cash'])-paid+credits-reserved


def choose(state,prepared,hour,book_reader,clock):
    """Choose once from observed parent-band candidates, then apply the 2c veto.

    This uses actual REST observation availability within the decision window;
    it does not claim the old minute-quote cohort's identical candidate universe.
    """
    counts=Counter();ranked=[]
    if hour<state['first_signal_at']:
        return {}
    for value in prepared:
        c,m=value['candidate'],value['metadata']
        if c['fixture_id'] in state['first_choices']:
            continue
        try:
            now=clock()
            if (not c.get('identity_verified') or not c.get('seven_model_verified') or
                    not c['published_at']<=hour<now<=hour+RULES.max_signal_age_seconds or
                    not state['activation_at']<=c['game_start']<hour<=c['forecast_end'] or
                    not hour-60<c['signal_quote_at']<=hour or amount(c['final_p01'])<=Decimal('.5')):
                raise ValueError('SHADOW_INELIGIBLE_TIMING_OR_PROVENANCE')
            discount=amount(c['p01'])-amount(c['signal_ask'])
            if not 0<discount<=amount(RULES.parent_band):
                continue
            book=book_reader(c);now=clock();ask,bid=_book(book,now)
            if now>hour+RULES.max_signal_age_seconds:
                raise ValueError('SHADOW_DECISION_DEADLINE')
            edge=amount(c['p01'])-ask-fee(c['provider'],1,ask,m['fee_coefficient'])
            ranked.append((book['received_at'],-edge,c['provider'],c['contract_id'],c,m,book,now,discount))
        except (ValueError,RuntimeError,KeyError,ArithmeticError):
            counts['unavailable_or_ineligible_candidate']+=1
    for *_,c,m,book,now,discount in sorted(ranked,key=lambda r:r[:4]):
        fixture=c['fixture_id']
        if fixture in state['first_choices']:
            continue
        state['first_choices'][fixture]={'signal_at':hour,'provider':c['provider'],'contract_id':c['contract_id'],
            'decision_at':now,'hourly_discount':str(discount)}
        if discount>amount(RULES.signal_band):
            counts['first_choice_vetoed_outside_2c']+=1
            continue
        try:
            ask,_=_book(book,now)
            if ask>=amount(c['p01']) or m['market_close']<=now:
                raise ValueError('ENTRY_PRICE_OR_MARKET_CLOSED')
            # Never round a limit up past the declared slippage cap. Select the
            # highest permitted exchange tick inside that cap.
            cap=min(Decimal(1),amount(c['p01']),ask+amount(RULES.max_slippage))
            limits=[]
            for r in m['price_ranges']:
                step=amount(r['step']);start=amount(r['start']);end=amount(r['end'])
                if step<=0 or not 0<=start<=end<=1:
                    raise ValueError('INVALID_TICK_RANGE')
                if c['side'] in {'no','short'}:
                    # Mirror the YES tick lattice into the economic NO book.
                    start,end=1-end,1-start
                bound=min(cap,end)
                if bound>=start:
                    tick=start+((bound-start)/step).to_integral_value(rounding=ROUND_DOWN)*step
                    if tick>=amount(c['p01']) or tick>=1:
                        tick-=step
                    limits.append(tick)
            limit=max((p for p in limits if ask<=p<=cap and p<amount(c['p01'])),default=None)
            if limit is None:
                raise ValueError('NO_VALID_MARKETABLE_TICK')
            step=amount(m['quantity_step']);minimum=amount(m['minimum_quantity'])
            if not 0<step<=minimum:
                raise ValueError('INVALID_QUANTITY_INCREMENT')
            quantity=(amount(RULES.notional)/limit/step).to_integral_value(rounding=ROUND_DOWN)*step
            if quantity<minimum:
                raise ValueError('NOTIONAL_BELOW_MINIMUM')
            reserve=quantity*limit+fee(c['provider'],quantity,limit,m['fee_coefficient'])+Decimal('.01')*quantity/step
            if cash(state)<reserve:
                raise ValueError('INSUFFICIENT_SHADOW_CASH')
            state['orders'].append({**c,'metadata':m,'mode':'paper_only','rules_hash':RULES.fingerprint,
                'decision_at':now,'limit_price':str(limit),'quantity':str(quantity),'filled':'0',
                'quantity_step':str(step),'reserved':str(reserve),'state':'awaiting_arrival',
                'decision_book':book,'entry_cost':'0','entry_fee':'0','fills':[]})
            counts['intents']+=1
        except (ValueError,RuntimeError,KeyError,ArithmeticError):
            counts['first_choice_rejected_no_replacement']+=1
    state['coverage'][str(hour)]=dict(counts)
    return dict(counts)


def arrive(order,book,now):
    if order['state']!='awaiting_arrival':
        return
    age=now-order['decision_at']
    if age<RULES.minimum_latency_seconds:
        return
    if age>RULES.maximum_arrival_seconds:
        order.update(state='arrival_unobserved',reserved='0')
        return
    _book(book,now)
    if book['requested_at']<order['decision_at']+RULES.minimum_latency_seconds:
        raise ValueError('PRE_DECISION_DEPTH_CANNOT_FILL')
    if order['metadata']['market_close']<=now:
        order.update(state='canceled_closed',reserved='0')
        return
    step=amount(order['quantity_step']);left=amount(order['quantity']);cost=paid=filled=Decimal(0)
    fills=[]
    for p,q in sorted((amount(p),amount(q)) for p,q in book['asks']):
        if p>amount(order['limit_price']):
            continue
        take=(min(left,q)/step).to_integral_value(rounding=ROUND_DOWN)*step
        if take<=0:
            continue
        charge=fee(order['provider'],take,p,order['metadata']['fee_coefficient'])
        fills.append({'price':str(p),'quantity':str(take),'fee':str(charge)})
        filled+=take;cost+=take*p;paid+=charge;left-=take
        if left==0:
            break
    if cost+paid>amount(order['reserved']):
        raise ValueError('DEPTH_FILL_EXCEEDS_CASH_RESERVE')
    order.update(state='ioc_observed',reserved='0',filled=str(filled),entry_cost=str(cost),entry_fee=str(paid),
        fills=fills,arrival_book=book,arrival_at=now,observed_arrival_latency_seconds=age,
        accounting='first_arrival_displayed_depth_ioc_proxy_not_exchange_fill')
    if filled:
        order.update(price=str(cost/filled),first_fill_at=now)


def trades(state):
    return [{**o,'quantity':o['filled'],'decision_at':o['first_fill_at']} for o in state['orders'] if amount(o['filled'])>0]


def sample_liquidation(values,marks,now,tracking):
    """Depth-and-exit-fee estimate, separate from a top-bid portfolio mark.

    A partial or unavailable book cannot value the whole position. Preserve
    prior complete samples for drawdown, but never present them as current.
    No order is sent, and a REST snapshot is not an executable sale receipt.
    """
    tracking=dict(tracking)
    tracking.update(checks=tracking.get('checks',0)+1,last_checked_at=now,
                    current_complete=False,current_pnl_usd=None,unpriced_positions=[])
    unrealized=Decimal(0);receipts=[];used=set()
    for trade in values:
        if trade.get('payout') is not None:
            continue
        try:
            key=trade['provider'],trade['contract_id']
            if key in used:
                raise ValueError('SHARED_DEPTH_CANNOT_BE_REUSED')
            used.add(key)
            book=marks.get(trade['forecast_id'],{}).get('book',{})
            _book(book,now)
            left=amount(trade['quantity']);proceeds=charges=Decimal(0)
            step=amount(trade['quantity_step'])
            for price,available in sorted(((amount(p),amount(q)) for p,q in book['bids']),reverse=True):
                take=(min(left,available)/step).to_integral_value(rounding=ROUND_DOWN)*step
                if take<=0:
                    continue
                proceeds+=take*price
                charges+=fee(trade['provider'],take,price,trade['metadata']['fee_coefficient'])
                left-=take
                if left==0:
                    break
            receipts.append({'fixture_id':trade['fixture_id'],'book':book,'unliquidated_quantity':str(left),
                             'gross_proceeds_usd':str(proceeds),'estimated_exit_fees_usd':str(charges)})
            if left:
                raise ValueError('INSUFFICIENT_DISPLAYED_BID_DEPTH')
            unrealized+=proceeds-charges-amount(trade['entry_cost'])-amount(trade['entry_fee'])
        except (ValueError,RuntimeError,KeyError,ArithmeticError) as error:
            tracking['unpriced_positions'].append({'fixture_id':trade['fixture_id'],'reason':str(error)})
    tracking['mark_evidence']=receipts
    if tracking['unpriced_positions']:
        return tracking
    pnl=amount(performance(values)['net_pnl_usd'])+unrealized
    peak=max(amount(tracking.get('peak_usd','0')),pnl)
    tracking.update(current_complete=True,current_pnl_usd=str(pnl),last_complete_at=now,
                    samples=tracking.get('samples',0)+1,peak_usd=str(peak),
                    max_sampled_drawdown_usd=str(max(amount(tracking.get('max_sampled_drawdown_usd','0')),peak-pnl)))
    return tracking


def report(state,marks,now):
    values=trades(state)
    state['equity']=sample_equity(values,marks,now,state['equity'])
    state['liquidation_equity']=sample_liquidation(values,marks,now,state.get('liquidation_equity',{}))
    return {**performance(values),'strategy':RULES.name,'rules_hash':RULES.fingerprint,
        'mode':'paper_only','live_orders':0,'activation_at':state['activation_at'],'first_signal_at':state['first_signal_at'],
        'execution':'first_fresh_post_latency_depth_sample_marketable_limit_ioc_proxy',
        'cash_available_usd':str(cash(state)),'intents':len(state['orders']),
        'unfilled_intents':sum(o['state']=='ioc_observed' and amount(o['filled'])==0 for o in state['orders']),
        'unobserved_arrivals':sum(o['state']=='arrival_unobserved' for o in state['orders']),
        'partial_fills':sum(0<amount(o['filled'])<amount(o['quantity']) for o in state['orders']),
        'equity':state['equity'],'depth_liquidation_equity':state['liquidation_equity'],
        'liquidation_basis':'sampled_displayed_bids_less_modeled_exit_fees_not_exchange_fills_or_continuous_drawdown',
        'coverage':state['coverage']}
