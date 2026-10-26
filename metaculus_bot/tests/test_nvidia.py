"""Provider control-flow tests; no live tournament questions or paid calls."""
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from metaculus_bot import llm
from metaculus_bot.api import ApiError


NOW = datetime(2026, 10, 26, tzinfo=timezone.utc)


def question(minutes=60):
    return SimpleNamespace(question_type="binary", question_text="Will the fair coin land heads?",
                           background_info="The next independent fair toss has two equally likely outcomes.",
                           resolution_criteria="YES if heads, NO if tails.", fine_print="No conditioning events.",
                           unit_of_measure=None, close_time=NOW + timedelta(minutes=minutes))


def completion(**changes):
    answer = {"abstain": False, "probability_yes": .5, "source_ids": [],
              "reasoning": "The question specifies an independent fair coin, so symmetry gives a one-half probability. "
                           "There is no supplied evidence of bias or dependence on prior tosses. The decisive "
                           "assumption is that the stated coin and toss procedure are actually fair; a changed "
                           "procedure or evidence of a biased mechanism would invalidate this base rate."}
    value = {"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(answer)}}],
             "usage": {"prompt_tokens": 200, "completion_tokens": 80}, "model": llm.NVIDIA_MODEL}
    return {**value, **changes}


class Session:
    def __init__(self, response=None, status=200, catalog=None):
        self.headers, self.posts = {}, []
        self.status, self.response = status, response or completion()
        self.catalog = catalog if catalog is not None else [{"id": llm.NVIDIA_MODEL}]

    def get(self, url, **kwargs):
        assert url == llm.NVIDIA_BASE + "/models"
        assert kwargs["allow_redirects"] is False
        return SimpleNamespace(status_code=200, json=lambda: {"data": self.catalog})

    def post(self, url, **kwargs):
        assert url == llm.NVIDIA_BASE + "/chat/completions"
        self.posts.append(kwargs)
        return SimpleNamespace(status_code=self.status, json=lambda: self.response)


@pytest.fixture(autouse=True)
def policy(monkeypatch):
    monkeypatch.setenv("METACULUS_NVIDIA_FREE_APPROVED", "true")
    monkeypatch.setattr(llm, "utcnow", lambda: NOW)


def test_nvidia_uses_approved_host_model_high_reasoning_and_no_paid_router():
    session = Session()
    client = llm.FreeNvidiaLLM("test-only", session)
    answer = client.forecast(question(), [])
    assert answer["provider_service"] == "NVIDIA_BUILD"
    assert answer["cost_usd"] is None  # Missing provider prices are not invented zeros.
    body = session.posts[0]["json"]
    assert body["model"] == llm.NVIDIA_MODEL
    assert body["chat_template_kwargs"]["reasoning_effort"] == "high" and body["max_tokens"] == 8192
    assert answer["reasoning_effort_requested"] == "high"
    assert body["response_format"] == {"type": "json_object"}
    assert "provider" not in body and "plugins" not in body and "tools" not in body
    assert session.posts[0]["allow_redirects"] is False
    assert session.posts[0]["timeout"] == (8, 240)


def test_missing_approval_or_missing_model_prevents_generation(monkeypatch):
    session = Session(catalog=[{"id": "unapproved-model"}])
    with pytest.raises(llm.FreeQuota, match="APPROVED_MODEL_UNAVAILABLE"):
        llm.FreeNvidiaLLM("test-only", session)
    monkeypatch.delenv("METACULUS_NVIDIA_FREE_APPROVED")
    with pytest.raises(llm.FreeQuota, match="NOT_APPROVED"):
        llm.FreeNvidiaLLM("test-only", session)
    assert session.posts == []


def test_quota_rejection_falls_back_once_and_keeps_original_question_and_evidence():
    primary = SimpleNamespace(model={"id": "primary:free"}, service="PRIMARY",
                              forecast=Mock(side_effect=llm.FreeQuota("FREE_MODEL_429_DEFERRED")))
    fallback = SimpleNamespace(model={"id": llm.NVIDIA_MODEL}, service="NVIDIA_BUILD",
                               forecast=Mock(return_value={"probability_yes": .5}))
    factory = Mock(return_value=fallback)
    client = llm.RejectedRequestFallback(primary, factory)
    q, sources = question(), [{"id": "S1", "excerpt": "evidence"}]
    first = client.forecast(q, sources)
    client.forecast(q, sources)
    assert first["provider_attempts"][0]["outcome"] == "quota_rejected_before_answer"
    primary.forecast.assert_called_once_with(q, sources)
    factory.assert_called_once()
    assert fallback.forecast.call_count == 2 and client.service == "NVIDIA_BUILD"


@pytest.mark.parametrize("error", [ApiError("OPENROUTER_GENERATION_UNCERTAIN"),
                                    llm.InvalidModelOutput("EMPTY_MODEL_ANSWER", {}),
                                    llm.CostPolicyViolation("FREE_MODEL_REPORTED_NONZERO_COST")])
def test_uncertain_or_accepted_answers_never_trigger_another_model(error):
    primary = SimpleNamespace(model={"id": "primary:free"}, service="PRIMARY",
                              forecast=Mock(side_effect=error))
    factory = Mock()
    with pytest.raises(type(error)):
        llm.RejectedRequestFallback(primary, factory).forecast(question(), [])
    factory.assert_not_called()


def test_short_window_stops_before_generation_and_near_close_bounds_read_timeout():
    session = Session()
    client = llm.FreeNvidiaLLM("test-only", session)
    with pytest.raises(llm.GenerationDeadline):
        client.forecast(question(minutes=1), [])
    assert session.posts == []
    client.forecast(question(minutes=4.5), [])
    assert session.posts[-1]["timeout"] == (8, 90)


@pytest.mark.parametrize("value", [1, "0.001", float("nan"), "unknown"])
def test_any_nonzero_or_unverifiable_reported_charge_halts(value):
    client = llm.FreeNvidiaLLM("test-only", Session(response=completion(usage={"cost": value})))
    with pytest.raises(llm.CostPolicyViolation):
        client.forecast(question(), [])


def test_accepted_truncated_answer_is_retained_instead_of_regenerated():
    response = completion(choices=[{"finish_reason": "length", "message": {"content": "partial"}}])
    session = Session(response=response)
    with pytest.raises(llm.InvalidModelOutput, match="INCOMPLETE_LLM_RESPONSE") as error:
        llm.FreeNvidiaLLM("test-only", session).forecast(question(), [])
    assert error.value.response is response and len(session.posts) == 1


def test_explicit_nvidia_factory_and_catalog_failure_before_generation(monkeypatch):
    monkeypatch.setenv("NVIDIA_API_KEY", "test-only")
    monkeypatch.setenv("METACULUS_LLM_PROVIDER", "nvidia")
    sentinel = SimpleNamespace(model={"id": llm.NVIDIA_MODEL}, service="NVIDIA_BUILD")
    monkeypatch.setattr(llm, "FreeNvidiaLLM", lambda key, **kwargs: sentinel)
    assert llm.free_llm_factory("").primary is sentinel
    monkeypatch.setenv("METACULUS_LLM_PROVIDER", "auto")
    monkeypatch.delenv("AI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(llm, "FreeLLM", Mock(side_effect=ApiError("OPENROUTER_MODELS", 503)))
    assert llm.free_llm_factory("test-openrouter").primary is sentinel


@pytest.mark.parametrize("model_id,effort,template", [
    ("z-ai/glm-5.3-flash", "high", {"clear_thinking": True}),
    ("deepseek-ai/deepseek-v4.1-flash", 100, None),
])
def test_each_nvidia_model_uses_its_native_high_reasoning_contract(model_id, effort, template):
    session = Session(catalog=[{"id": model_id}])
    answer = llm.FreeNvidiaLLM("test-only", session, model_id=model_id).forecast(question(), [])
    body = session.posts[0]["json"]
    assert body["reasoning_effort"] == effort
    assert body.get("chat_template_kwargs") == template
    assert answer["reasoning_effort_requested"] == effort


def test_explicit_transport_failover_preserves_evidence_and_records_uncertainty():
    primary = SimpleNamespace(model={"id": "primary:free"}, service="OPENROUTER",
                              forecast=Mock(side_effect=ApiError("OPENROUTER_GENERATION_UNCERTAIN")))
    second = SimpleNamespace(model={"id": "secondary:free"}, service="SECONDARY",
                             forecast=Mock(side_effect=llm.FreeQuota("FREE_MODEL_429_DEFERRED")))
    final = SimpleNamespace(model={"id": llm.NVIDIA_MODEL}, service="NVIDIA_BUILD",
                            forecast=Mock(return_value={"probability_yes": .5}))
    client = llm.RejectedRequestFallback(primary, [lambda: second, lambda: final], allow_transport_fallback=True)
    q, sources = question(), [{"id": "S1", "excerpt": "unchanged evidence"}]
    answer = client.forecast(q, sources)
    assert [r["outcome"] for r in answer["provider_attempts"]] == [
        "transport_unconfirmed_no_answer_received", "quota_rejected_before_answer"]
    for provider in [primary, second, final]:
        provider.forecast.assert_called_once_with(q, sources)


@pytest.mark.parametrize("error", [llm.InvalidModelOutput("INVALID_MODEL_JSON", {}),
                                    llm.CostPolicyViolation("COST_POLICY")])
def test_failover_never_replaces_an_accepted_invalid_or_charged_response(error):
    primary = SimpleNamespace(model={"id": "primary:free"}, service="PRIMARY",
                              forecast=Mock(side_effect=error))
    factory = Mock()
    with pytest.raises(type(error)):
        llm.RejectedRequestFallback(primary, factory, allow_transport_fallback=True).forecast(question(), [])
    factory.assert_not_called()


def test_discovery_includes_free_reasoning_chat_models_but_excludes_paid_and_safety():
    catalog = [{"id": name, "pricing": {"prompt": price, "completion": price},
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "supported_parameters": ["reasoning", "response_format"]}
               for name, price in [("new-reasoner:free", "0"), ("paid-reasoner:free", "0.001"),
                                   ("content-safety:free", "0"), ("no-free-suffix", "0")]]
    assert [m["id"] for m in llm.select_models(catalog)] == ["new-reasoner:free"]


@pytest.mark.parametrize("changes", [{"error": "malformed"}, {"provider_metadata": "malformed"},
                                     {"provider_metadata": {"gateway": "malformed"}}])
def test_malformed_accepted_provider_envelope_is_preserved(changes):
    with pytest.raises(llm.InvalidModelOutput):
        llm.FreeNvidiaLLM("test-only", Session(response=completion(**changes))).forecast(question(), [])
