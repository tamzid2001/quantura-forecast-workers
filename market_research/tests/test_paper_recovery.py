"""Outages must cause bounded catch-up, never synthetic trades or lost hours."""
import gzip
import json
from unittest.mock import patch

import pytest

from market_research import game_p01_paper as paper


def recovered_run(tmp_path, *, max_hours=2, missing=False):
    state = paper.fresh_state(3500)
    with patch.object(paper, 'restore', return_value=state), \
         patch.object(paper, 'artifacts', return_value=[]), \
         patch.object(paper, 'snapshots_at', side_effect=RuntimeError('MISSING_POINT_IN_TIME_FEED:test') if missing else None,
                      return_value=([], {})) as snapshots, \
         patch.object(paper, 'candidates_at', return_value=([], {})), \
         patch.object(paper, 'update_settlements', return_value={}) as settlements:
        if missing or not 1 <= max_hours <= 48:
            with pytest.raises(RuntimeError, match='PAPER_TRACKER_RETRY_REQUIRED'):
                paper.run(tmp_path, now=3600*80, max_hours=max_hours)
        else:
            paper.run(tmp_path, now=3600*80, max_hours=max_hours)
    saved = json.loads(gzip.decompress((tmp_path/'paper-state.json.gz').read_bytes()))
    return saved, snapshots, settlements


def test_old_cursor_replays_consecutive_hours_without_fast_forward(tmp_path):
    saved, snapshots, settlements = recovered_run(tmp_path)
    assert [c.args[0] for c in snapshots.call_args_list] == [3600, 7200]
    assert saved['next_hour'] == 10800 and saved['catchup_pending']
    assert not saved['last_errors'] and settlements.call_count == 1
    report = json.loads((tmp_path/'comparison-stats.json').read_text())
    assert report['catchup_pending']


def test_real_gap_preserves_cursor_and_still_reconciles_existing_trades(tmp_path):
    saved, _, settlements = recovered_run(tmp_path, missing=True)
    assert saved['next_hour'] == 3600 and not saved['coverage']
    assert saved['last_errors'][0]['error'].startswith('MISSING_POINT_IN_TIME_FEED')
    assert settlements.call_count == 1


@pytest.mark.parametrize('value', [0, 49])
def test_invalid_batch_size_never_changes_cursor(tmp_path, value):
    saved, snapshots, _ = recovered_run(tmp_path, max_hours=value)
    assert saved['next_hour'] == 3600 and not snapshots.called


def test_feed_discovery_reaches_an_older_cursor_and_stops_pagination():
    def github(path):
        page = int(path.rsplit('page=', 1)[1])
        return {'artifacts': [{'id': page*100+i, 'name': 'feed', 'created_at': paper.iso(5000-page*100+i),
                               'workflow_run': {'head_branch': 'main'}} for i in range(100)]}
    with patch.object(paper, 'github', side_effect=github) as api:
        found = paper.artifacts('feed', through=4600)
    assert api.call_count == 4 and len(found) == 400


def test_discovery_cap_fails_instead_of_claiming_full_coverage():
    batch = [{'id': 1, 'name': 'feed', 'created_at': paper.iso(5000), 'workflow_run': {'head_branch': 'main'}}]*100
    with patch.object(paper, 'github', return_value={'artifacts': batch}):
        with pytest.raises(RuntimeError, match='ARTIFACT_DISCOVERY_INCOMPLETE'):
            paper.artifacts('feed', through=3600)
