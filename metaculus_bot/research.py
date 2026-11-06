from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import requests

from .api import utcnow

RESEARCH_TIMEOUT_SECONDS = 60
REASONING_RESERVE_SECONDS = 60
MAX_QUESTION_BYTES = 256_000
MAX_WORKER_OUTPUT_BYTES = 1_600_000  # Six records, including worst-case JSON Unicode escapes.
QUESTION_FIELDS = ('question_text', 'resolution_criteria', 'fine_print', 'background_info')


class Text(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def public_url(url: str) -> bool:
    if len(url) > 4096:
        return False
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.port not in (None, 443):
        return False
    if parsed.hostname == "metaculus.com" or parsed.hostname.endswith(".metaculus.com"):
        return False
    try:
        addresses = socket.getaddrinfo(parsed.hostname, 443, type=socket.SOCK_STREAM)
        return bool(addresses) and all(ipaddress.ip_address(row[4][0]).is_global for row in addresses)
    except (OSError, ValueError):
        return False


def public_get(url: str, *, maximum=1_500_000) -> tuple[bytes, str]:
    # No authenticated session, cookies, ambient proxy credentials, or unrestricted
    # redirect. Question links are untrusted data, never instructions to a tool.
    session = requests.Session()
    session.trust_env = False
    for _ in range(4):
        if not public_url(url):
            raise ValueError("NONPUBLIC_SOURCE_URL")
        with session.get(url, headers={"User-Agent": "Quantura-Research/1.0"}, timeout=(8, 20),
                         allow_redirects=False, stream=True) as response:
            if response.is_redirect:
                from urllib.parse import urljoin
                url = urljoin(url, response.headers["Location"])
                continue
            response.raise_for_status()
            raw = bytearray()
            for chunk in response.iter_content(65536):
                raw.extend(chunk)
                if len(raw) > maximum:
                    raise ValueError("SOURCE_TOO_LARGE")
            return bytes(raw), url
    raise ValueError("SOURCE_REDIRECT_LIMIT")


def _collect_evidence(question, emit) -> None:
    text = "\n".join(filter(None, [question.resolution_criteria, question.fine_print, question.background_info]))
    urls = list(dict.fromkeys(unescape(u).rstrip(".,;:") for u in re.findall(r'https://[^\s<>"\)]+', text)))
    results = []
    for url in urls[:6]:
        if len(results) == 3:
            break
        try:
            raw, final = public_get(url)
            parser = Text()
            parser.feed(raw.decode("utf-8", errors="replace"))
            excerpt = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()[:14000]
            if len(excerpt) < 100:
                continue
            results.append({"id": f"S{len(results) + 1}", "url": final, "sha256": hashlib.sha256(raw).hexdigest(),
                            "retrieved_at": utcnow().isoformat(),
                            "excerpt": excerpt})
            emit(results[-1])
        except (requests.RequestException, ValueError, OSError):
            continue
    from .fred import evidence as fred_evidence
    def completed_fred(result):
        result = dict(result)
        result["id"] = f"S{len(results) + 1}"
        result["retrieved_at"] = utcnow().isoformat()
        results.append(result)
        emit(result)
    fred_evidence(question, emit=completed_fred)


def _source_record(value):
    """Validate the bounded private child protocol without another DNS lookup."""
    if not isinstance(value, dict):
        return None
    limits = {'url': 4096, 'excerpt': 16000, 'sha256': 64, 'retrieved_at': 64}
    if any(not isinstance(value.get(k), str) or not 0 < len(value[k]) <= n for k, n in limits.items()):
        return None
    if not value['url'].startswith('https://') or not re.fullmatch('[a-f0-9]{64}', value['sha256']):
        return None
    return {k: value[k] for k in limits}


def _completed_sources(raw):
    # A timeout can interrupt JSON mid-record. Only newline-committed complete
    # observations survive. Do not log malformed bytes or partial excerpts.
    if not raw or len(raw) > MAX_WORKER_OUTPUT_BYTES:
        return []
    results = []
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b'\n'):
            continue
        try:
            source = _source_record(json.loads(line))
        except (ValueError, UnicodeDecodeError):
            continue
        if source and len(results) < 6:
            results.append({'id': f'S{len(results)+1}', **source})
    return results


def evidence(question, *, deadline=None) -> list[dict]:
    """Collect research in a killable child; never block model time indefinitely.

    Requests' read timeout measures socket inactivity, not wall-clock download
    time. Process supervision also bounds slow streams, DNS and optional FRED
    work. Python process creation itself is subject to platform scheduling.
    """
    now = time.monotonic()
    if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                                 or not math.isfinite(deadline)):
        raise ValueError('INVALID_RESEARCH_DEADLINE')
    timeout = min(RESEARCH_TIMEOUT_SECONDS, deadline-now) if deadline is not None else RESEARCH_TIMEOUT_SECONDS
    if timeout <= 0:
        return []
    document = {k: getattr(question, k, None) for k in QUESTION_FIELDS}
    if any(v is not None and not isinstance(v, str) for v in document.values()):
        raise ValueError('INVALID_RESEARCH_QUESTION')
    raw_input = json.dumps(document).encode()
    if len(raw_input) > MAX_QUESTION_BYTES:
        raise ValueError('RESEARCH_QUESTION_TOO_LARGE')
    # Research needs no forecasting, GitHub or Metaculus credentials. Preserve
    # only platform/certificate configuration and the optional FRED credential.
    env = {k: os.environ[k] for k in ('PATH', 'SYSTEMROOT', 'SSL_CERT_FILE', 'SSL_CERT_DIR',
           'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE', 'FRED_API_KEY') if k in os.environ}
    env['PYTHONUNBUFFERED'] = '1'
    state = 'completed'
    try:
        result = subprocess.run([sys.executable, '-m', 'metaculus_bot.research', '--worker'],
            input=raw_input, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=timeout, check=False, cwd=Path(__file__).resolve().parents[1], env=env)
        raw = result.stdout
        if result.returncode:
            state = 'worker_failed'
    except subprocess.TimeoutExpired as error:
        # run() kills and reaps the child before raising. No orphan background
        # fetch or concurrent request survives into the next question.
        raw, state = error.stdout, 'deadline_reached'
    except OSError:
        raw, state = None, 'worker_unavailable'
    sources = _completed_sources(raw)
    print(json.dumps({'event': 'metaculus_research_budget', 'status': state,
                      'completed_sources': len(sources), 'budget_seconds': round(timeout, 3)}), flush=True)
    return sources


def _worker():
    raw = sys.stdin.buffer.read(MAX_QUESTION_BYTES+1)
    if len(raw) > MAX_QUESTION_BYTES:
        raise ValueError('RESEARCH_QUESTION_TOO_LARGE')
    document = json.loads(raw)
    if (not isinstance(document, dict) or set(document) != set(QUESTION_FIELDS)
            or any(v is not None and not isinstance(v, str) for v in document.values())):
        raise ValueError('INVALID_RESEARCH_QUESTION')
    def emit(value):
        record = _source_record(value)
        if record:
            print(json.dumps(record, ensure_ascii=True), flush=True)
    _collect_evidence(SimpleNamespace(**document), emit)


if __name__ == '__main__':
    if sys.argv[1:] != ['--worker']:
        raise SystemExit('RESEARCH_WORKER_ARGUMENT_REQUIRED')
    _worker()
