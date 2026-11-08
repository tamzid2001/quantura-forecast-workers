"""Explicit forward recovery after irrecoverable observation outages.

An outage is not reconstructed trading. The old ledger remains intact, the
unobserved interval remains visible, and games already underway cannot acquire
a replacement 'first' trade in the new observation cohort.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math


def ledger_digest(state):
    return hashlib.sha256(json.dumps({k:p['trades'] for k,p in sorted(state['portfolios'].items())},
                                     sort_keys=True,separators=(',',':'),allow_nan=False).encode()).hexdigest()


def validate_recovery(state):
    previous_end=None
    for gap in state.get('observation_gaps',[]):
        if (gap.get('status')!='unobserved_not_backfilled' or
                any(type(gap.get(k)) is not int for k in ['start','end_exclusive','recorded_at']) or
                not 0<=gap['start']<=gap['recorded_at']<gap['end_exclusive'] or
                gap['start']%3600 or gap['end_exclusive']%3600 or
                (previous_end is not None and gap['start']<previous_end) or
                not gap.get('source_artifact_ids') or
                not isinstance(gap.get('preserved_ledger_sha256'),str) or len(gap['preserved_ledger_sha256'])!=64):
            raise ValueError('INVALID_OBSERVATION_GAP_RECORD')
        previous_end=gap['end_exclusive']
    active=state.get('active_observation_epoch')
    if active is not None and (not state.get('observation_gaps') or type(active) is not int or active!=previous_end or state['next_hour']<active):
        raise ValueError('INVALID_OBSERVATION_EPOCH')
    epochs={g['end_exclusive'] for g in state.get('observation_gaps',[])}
    for portfolio in state.get('portfolios',{}).values():
        for trade in portfolio['trades']:
            epoch=trade.get('observation_epoch')
            if epoch is not None and (type(epoch) is not int or epoch not in epochs or
                    trade['game_start']<epoch or trade['signal_at']<epoch or trade['decision_at']<epoch):
                raise ValueError('INVALID_TRADE_OBSERVATION_EPOCH')
    return state


def resume_forward(state,now,catalog,snapshots):
    validate_recovery(state)
    if type(now) is not int or now<0:
        raise ValueError('INVALID_RECOVERY_TIME')
    old=state['next_hour']
    if old+120>now:
        raise ValueError('NO_HISTORICAL_GAP_TO_RESUME')
    try:
        snapshots(old,catalog)
    except RuntimeError as error:
        source_error=str(error)
        if not source_error.startswith(('MISSING_POINT_IN_TIME_FEED:','STALE_POINT_IN_TIME_FEED:')):
            raise
    else:
        raise ValueError('HISTORICAL_EVIDENCE_AVAILABLE_REPLAY_REQUIRED')
    epoch=(now//3600+1)*3600
    rows,proof=snapshots(epoch,catalog)
    if not proof:
        raise ValueError('FORWARD_RECOVERY_REQUIRES_FRESH_PUBLICATIONS')
    # Checkpoints are validated before calling this function. Snapshot/provider
    # validation remains authoritative; a future upload is never accepted.
    if any(not math.isfinite(float(r['publication_ts'])) or r['publication_ts']>now for r in rows):
        raise ValueError('FUTURE_RECOVERY_PUBLICATION')
    before=ledger_digest(state)
    gap={'status':'unobserved_not_backfilled','start':old,'end_exclusive':epoch,'recorded_at':now,
         'source_error':source_error,'source_artifact_ids':proof,'preserved_ledger_sha256':before,
         'unobserved_hours':(epoch-old)//3600,'policy':'new_games_only_separate_observation_cohort'}
    state.setdefault('observation_gaps',[]).append(gap)
    state['active_observation_epoch']=epoch
    state['next_hour']=epoch
    if ledger_digest(state)!=before:
        raise AssertionError('RECOVERY_MUTATED_EXISTING_TRADES')
    validate_recovery(state)
    return gap


def epoch_rows(state,rows,stamp):
    epoch=state.get('active_observation_epoch')
    if epoch is None:
        return rows,0
    kept=[r for r in rows if stamp(r['game_start'])>=epoch]
    return kept,len(rows)-len(kept)


def tag_new_trades(state,before):
    epoch=state.get('active_observation_epoch')
    if epoch is not None:
        for name,portfolio in state['portfolios'].items():
            for trade in portfolio['trades'][before[name]:]:
                trade['observation_epoch']=epoch


def by_epoch(portfolio,performance):
    groups=defaultdict(list)
    for trade in portfolio['trades']:
        groups[str(trade.get('observation_epoch','original_observed'))].append(trade)
    return {name:performance(trades) for name,trades in groups.items()}
