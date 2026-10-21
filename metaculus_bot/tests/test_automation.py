from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from metaculus_bot.automation import dispatch_needed, queue_ids
from metaculus_bot.checkpoint import Checkpoint
from metaculus_bot.readiness import community_baseline


NOW = datetime(2026, 10, 21, 20, tzinfo=timezone.utc)


def test_fresh_queue_preserves_unique_post_ids():
    queue = {"bot_id": 310211, "at": NOW.isoformat(), "questions": [
        {"question_id": 1, "post_id": 7}, {"question_id": 2, "post_id": 7}]}
    assert queue_ids(queue, 310211, NOW) == ([1, 2], [7])


def test_small_forecast_batches_do_not_download_the_entire_backlog():
    queue = {"bot_id": 310211, "at": NOW.isoformat(), "questions": [
        {"question_id": i, "post_id": 1000+i} for i in range(1,101)]}
    ids,posts = queue_ids(queue,310211,NOW,maximum=6)
    assert ids == list(range(1,7)) and len(posts) == 6


@pytest.mark.parametrize("bot,age", [(310227, 0), (310211, 1801), (310211, -1)])
def test_wrong_bot_stale_and_future_queues_are_rejected(bot, age):
    queue = {"bot_id": bot, "at": (NOW-timedelta(seconds=age)).isoformat(), "questions": []}
    with pytest.raises(ValueError, match="STALE_OR_WRONG_BOT_QUEUE"):
        queue_ids(queue, 310211, NOW)


@pytest.mark.parametrize("value", ["1; unsafe", True, -1, 0])
def test_queue_fields_cannot_be_commands_or_invalid_identifiers(value):
    queue = {"bot_id": 310211, "at": NOW.isoformat(), "questions": [{"question_id": value, "post_id": 7}]}
    with pytest.raises(ValueError, match="INVALID_QUEUE_IDENTIFIERS"):
        queue_ids(queue, 310211, NOW)


@pytest.mark.parametrize("state", ["queued", "in_progress", "waiting", "pending", "requested"])
def test_watchdog_never_duplicates_active_work(state):
    assert not dispatch_needed([{"event": "schedule", "status": state, "created_at": (NOW-timedelta(hours=1)).isoformat()}], NOW)


def test_watchdog_dispatches_only_when_missing_or_twenty_minutes_stale():
    assert dispatch_needed([], NOW)
    run = {"event": "workflow_dispatch", "status": "completed", "created_at": (NOW-timedelta(minutes=19)).isoformat()}
    assert not dispatch_needed([run], NOW)
    run["created_at"] = (NOW-timedelta(minutes=20)).isoformat()
    assert dispatch_needed([run], NOW)
    assert dispatch_needed([{**run, "event": "pull_request", "status": "in_progress"}], NOW)


def test_private_audit_is_encrypted_and_keeps_actual_receipt_status(tmp_path):
    state = Checkpoint(tmp_path/"checkpoint.enc", "test-token")
    record = {"question_id": 12, "answer": {"probability_yes": .2, "reasoning": "private-reasoning"}}
    state.audit(record, "submitted")
    raw = (tmp_path/"metaculus-audit.enc").read_bytes()
    assert b"private-reasoning" not in raw
    import json
    rows = json.loads(state.cipher.decrypt(raw))
    assert rows[0]["record"] == record and rows[0]["status"] == "submitted"
    assert (tmp_path/"metaculus-audit.enc").stat().st_mode & 0o777 == 0o600


def test_community_comparator_requires_a_preclose_timestamp():
    latest = {"centers": [.4], "start_time": (NOW-timedelta(minutes=1)).isoformat()}
    assert community_baseline({"aggregations": {"recency_weighted": {"latest": latest}}}, NOW) == .4
    latest["start_time"] = NOW.isoformat()
    assert community_baseline({"aggregations": {"recency_weighted": {"latest": latest}}}, NOW) is None
    assert community_baseline({}, NOW) is None


def test_dry_run_switch_blocks_live_requests(monkeypatch):
    from metaculus_bot.runner import run
    monkeypatch.setenv("DRY_RUN", "1")
    args = SimpleNamespace(submit=True)
    with pytest.raises(RuntimeError, match="DRY_RUN_BLOCKS_SUBMISSION"):
        run(args, SimpleNamespace(identity=lambda: pytest.fail("No network request is allowed")))


def test_operator_review_block_survives_restart(monkeypatch, tmp_path):
    from metaculus_bot.runner import run
    path = tmp_path/"checkpoint.enc"
    Checkpoint(path, "test-token").block("METACULUS_403")
    monkeypatch.setenv("METACULUS_TOKEN", "test-token")
    args = SimpleNamespace(submit=False, checkpoint=str(path))
    with pytest.raises(RuntimeError, match="METACULUS_OPERATOR_REVIEW_REQUIRED"):
        run(args, SimpleNamespace(identity=lambda: pytest.fail("A blocked worker must not retry API writes")))
