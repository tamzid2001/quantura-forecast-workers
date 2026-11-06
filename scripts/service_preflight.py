"""Read-only gateway availability check before dependent worker fan-out.

HTTP 200 is an availability precondition, not proof of workload correctness.
No credentials, paid inference, billing flush or alternate backend is invoked.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.error
import urllib.request

HEALTH_URL = 'https://quantura.studio/api/health'
SAFE_PLATFORM_ERRORS = {'DEPLOYMENT_DISABLED','DEPLOYMENT_NOT_FOUND','FUNCTION_INVOCATION_TIMEOUT'}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe(*, timeout=15, opener=None):
    if type(timeout) is not int or not 1 <= timeout <= 30:
        raise ValueError('INVALID_PREFLIGHT_TIMEOUT')
    opener = opener or urllib.request.build_opener(NoRedirect())
    request = urllib.request.Request(HEALTH_URL,method='HEAD',headers={'User-Agent':'Quantura-worker-availability/1'})
    status, platform, transport = None, None, None
    try:
        with opener.open(request,timeout=timeout) as response:
            status = response.status
            platform = response.headers.get('x-vercel-error')
    except urllib.error.HTTPError as error:
        status = error.code
        platform = error.headers.get('x-vercel-error') if error.headers else None
    except OSError:
        transport = 'network_unavailable'
    return {'event':'quantura_gateway_availability','url':HEALTH_URL,
            'available':status==200,'http_status':status,
            'platform_error':platform if platform in SAFE_PLATFORM_ERRORS else None,
            'transport_error':transport}


def main():
    result = probe()
    print(json.dumps(result,sort_keys=True))
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as summary:
            summary.write('## Backend availability\n\n'+json.dumps(result,sort_keys=True)+'\n\n')
            if not result['available']:
                summary.write('Dependent work was not started. No forecasts, billing deliveries or publication are claimed.\n')
    if not result['available']:
        raise SystemExit('QUANTURA_GATEWAY_UNAVAILABLE_HTTP_'+str(result['http_status'] or 'NETWORK'))


if __name__=='__main__':
    main()
