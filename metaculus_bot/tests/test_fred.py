from datetime import datetime, timezone
from types import SimpleNamespace
import json

import pytest

from metaculus_bot import fred, time_series

INFO = {"id": "UNRATE", "title": "Unemployment Rate", "units": "Percent", "frequency_short": "M", "frequency": "Monthly"}
SPEC = {"kind": "fred", "series_id": "UNRATE", "unit": "Percent", "frequency": "1MS", "target_date": "2027-01-01"}


def fetch(path, params):
    assert params["series_id"] == "UNRATE"
    if path == "series":
        return {"seriess": [INFO]}
    return {"observations": [{"date": "2026-10-01", "value": "."}, {"date": "2026-09-01", "value": "0"}, {"date": "2026-08-01", "value": "-2.5"}]}


def question(text="US unemployment", criteria="", **extra):
    return SimpleNamespace(question_text=text, resolution_criteria=criteria, fine_print="", background_info="",
                           question_type="numeric", **extra)


def test_fred_candidates_prioritize_exact_source_over_context_and_bound_requests():
    q = question("GDP inflation unemployment payroll recession", "Source: https://fred.stlouisfed.org/series/DGS10?abc=1")
    assert fred.candidates(q) == ["DGS10", "UNRATE", "CPIAUCSL"]
    assert fred.candidates(question("unrelated")) == []
    assert fred.candidates(question("unrelated", "https://fred.stlouisfed.org.evil/series/GDP")) == []


def test_fred_history_validates_native_units_keeps_zero_and_excludes_missing():
    rows, metadata = fred.history(SPEC, fetch=fetch, now=datetime(2026, 10, 24, tzinfo=timezone.utc))
    assert [r["target"] for r in rows] == [-2.5, 0]
    assert metadata["unit"] == "Percent"
    assert metadata["vintage_precision"] == "date"
    assert len(metadata["sha256"]) == 64
    assert "api_key" not in json.dumps(metadata)


@pytest.mark.parametrize("edit,code", [({"unit": "Index"}, "UNITS_MISMATCH"), ({"frequency": "1D"}, "FREQUENCY_MISMATCH"), ({"series_id": "../x"}, "INVALID_SERIES"), ({"vintage_date": "2026-02-30"}, "INVALID_VINTAGE"), ({"vintage_date": "2999-01-01"}, "INVALID_VINTAGE")])
def test_fred_reviewed_mapping_rejects_semantic_or_vintage_mismatch(edit, code):
    with pytest.raises(fred.FredError, match=code):
        fred.history({**SPEC, **edit}, fetch=fetch)


@pytest.mark.parametrize("observations", [[{"date": "2026-08-01", "value": "nan"}], [{"date": "bad", "value": "1"}], [{"date": "2026-08-01", "value": "1"}] * 2, [{"date": "2999-01-01", "value": "1"}], [{"date": "2026-08-01", "value": "bad"}]])
def test_fred_history_rejects_invalid_or_duplicate_observations(observations):
    def request(path, params):
        return {"seriess": [INFO]} if path == "series" else {"observations": observations}
    with pytest.raises(fred.FredError, match="INVALID_OBSERVATION"):
        fred.history(SPEC, fetch=request)


def test_fred_context_explains_native_measurement_not_unrequested_growth(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "a" * 32)
    rows = fred.evidence(question(), fetch=fetch)
    assert len(rows) == 1
    data = json.loads(rows[0]["excerpt"])
    assert data["units"] == "Percent"
    assert "Research context" in data["role"]
    assert rows[0]["url"] == "https://fred.stlouisfed.org/series/UNRATE"
    monkeypatch.delenv("FRED_API_KEY")
    assert fred.evidence(question(), fetch=lambda *a: pytest.fail("No key must not make a call")) == []


def test_fred_outage_is_optional_research_context(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "a" * 32)
    def unavailable(*args):
        raise fred.FredError("FRED_RATE_LIMITED")
    assert fred.evidence(question(), fetch=unavailable) == []


class Response:
    status_code = 200

    def __init__(self, data=b'{"seriess": []}'):
        self.data = data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, size):
        yield self.data


def test_fred_transport_fixed_origin_no_redirect_and_no_key_in_errors(monkeypatch):
    monkeypatch.setenv("FRED_API_KEY", "a" * 32)
    def get(url, **kwargs):
        assert url == "https://api.stlouisfed.org/fred/series"
        assert kwargs["allow_redirects"] is False
        assert kwargs["params"]["api_key"] == "a" * 32
        return Response()
    assert fred.get("series", {}, SimpleNamespace(get=get)) == {"seriess": []}
    with pytest.raises(fred.FredError, match="PATH_UNSUPPORTED"):
        fred.get("../evil", {})
    def failing(*args, **kwargs):
        import requests
        raise requests.ConnectionError("private key is " + "a" * 32)
    with pytest.raises(fred.FredError) as error:
        fred.get("series", {}, SimpleNamespace(get=failing))
    assert "a" * 32 not in str(error.value)


@pytest.mark.parametrize("status,raw,code", [(429, b"secret", "RATE_LIMITED"), (403, b"secret", "UPSTREAM_UNAVAILABLE"), (200, b"bad", "UPSTREAM_UNAVAILABLE"), (200, b" " * 4_000_001, "RESPONSE_TOO_LARGE")])
def test_fred_transport_rejects_oversize_and_throttling(monkeypatch, status, raw, code):
    monkeypatch.setenv("FRED_API_KEY", "a" * 32)
    response = Response(raw)
    response.status_code = status
    with pytest.raises(fred.FredError, match=code):
        fred.get("series", {}, SimpleNamespace(get=lambda *a, **k: response))


def test_reviewed_fred_mapping_is_criteria_hash_fenced(monkeypatch):
    import hashlib
    q = question(criteria="Exact reviewed source", id_of_question=10)
    registry = [{**SPEC, "question_id": 10, "resolution_sha256": hashlib.sha256(q.resolution_criteria.encode()).hexdigest()}]
    assert time_series.definition(q, registry)["kind"] == "fred"
    q.question_type = "numeric"
    q.resolution_criteria += " changed"
    assert time_series.definition(q, registry) is None


def test_fred_ensemble_adapter_does_not_fabricate_short_history(monkeypatch):
    monkeypatch.setattr(fred, "history", lambda spec: ([{"timestamp": "2026-09-01", "target": 1}], {}))
    with pytest.raises(ValueError, match="GENUINE_HISTORY_TOO_SHORT"):
        time_series.history(SPEC)


@pytest.mark.parametrize('metadata', [{}, {'seriess': None}, {'seriess': ['bad']}, {'seriess': [{'id': 'GDP'}]}])
def test_fred_metadata_shape_is_validated(metadata):
    with pytest.raises(fred.FredError, match='SERIES_MISMATCH'):
        fred.history(SPEC, fetch=lambda *args: metadata)


def test_fred_response_errors_and_missing_credentials_never_escape_provider_codes(monkeypatch):
    monkeypatch.delenv('FRED_API_KEY', raising=False)
    with pytest.raises(fred.FredError, match='NOT_CONFIGURED'):
        fred.get('series', {})
    monkeypatch.setenv('FRED_API_KEY', 'a' * 32)
    for payload in [b'[]', b'{"error_code": 400}']:
        with pytest.raises(fred.FredError, match='INVALID_RESPONSE'):
            fred.get('series', {}, SimpleNamespace(get=lambda *a, **kw: Response(payload)))


@pytest.mark.parametrize('payload,code', [({}, 'INVALID_RESPONSE'), ({'observations': []}, 'HISTORY_EMPTY'), ({'observations': [None]}, 'INVALID_OBSERVATION')])
def test_fred_observation_envelopes_fail_closed(payload, code):
    with pytest.raises(fred.FredError, match=code):
        fred.history(SPEC, fetch=lambda path, params: {'seriess': [INFO]} if path == 'series' else payload)


def test_malformed_economic_research_does_not_block_other_question_sources(monkeypatch):
    monkeypatch.setenv('FRED_API_KEY', 'a' * 32)
    for payload in [{'observations': None}, {'observations': [None]}]:
        assert fred.evidence(question(), fetch=lambda path, params: {'seriess': [INFO]} if path == 'series' else payload) == []


def test_fred_dynamic_economic_search_uses_provider_ids_only(monkeypatch):
    monkeypatch.setenv('FRED_API_KEY', 'a' * 32)
    calls=[]
    def request(path, params):
        calls.append((path, params))
        if path=='series/search':
            assert params['search_text']=='housing starts'
            return {'seriess':[{'id':'UNRATE'}, {'id':'../bad'}, {'id':'UNRATE'}]}
        return fetch(path, params)
    rows=fred.evidence(question('Will housing starts increase?'), fetch=request)
    assert len(rows)==1
    assert rows[0]['url'].endswith('/UNRATE')
    assert len(calls)==3
    assert 'Research context' in rows[0]['excerpt']
