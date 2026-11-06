"""Read-only gateway availability check before dependent worker fan-out.

HTTP 200 is an availability precondition, not proof of workload correctness.
No credentials, paid inference, billing flush or alternate backend is invoked.
"""
from __future__ import annotations

import argparse
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


def decision(result, *, respect_maintenance=False, environ=None):
    """Defer only an acknowledged scheduled hosting suspension, never other faults."""
    environ = os.environ if environ is None else environ
    deferred = (respect_maintenance
                and environ.get('GITHUB_EVENT_NAME') == 'schedule'
                and environ.get('QUANTURA_HOSTING_MAINTENANCE') == 'true'
                and not result['available']
                and result['http_status'] == 402
                and result['platform_error'] == 'DEPLOYMENT_DISABLED'
                and result['transport_error'] is None)
    return {**result, 'deferred': bool(deferred),
            'disposition': ('available' if result['available'] else
                            'hosting_maintenance' if deferred else 'failed')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--respect-hosting-maintenance', action='store_true',
                        help='Scheduled-only deferral for explicitly acknowledged Vercel suspension')
    args = parser.parse_args(argv)
    result = decision(probe(), respect_maintenance=args.respect_hosting_maintenance)
    print(json.dumps(result, sort_keys=True))
    if os.environ.get('GITHUB_OUTPUT'):
        with Path(os.environ['GITHUB_OUTPUT']).open('a') as output:
            output.write('available='+str(result['available']).lower()+'\n')
            output.write('deferred='+str(result['deferred']).lower()+'\n')
    if os.environ.get('GITHUB_STEP_SUMMARY'):
        with Path(os.environ['GITHUB_STEP_SUMMARY']).open('a') as summary:
            summary.write('## Backend availability\n\n'+json.dumps(result,sort_keys=True)+'\n\n')
            if not result['available']:
                summary.write('Dependent work was not started. No forecasts, billing deliveries or publication are claimed.\n')
            if result['deferred']:
                summary.write('**DEFERRED — HOSTING MAINTENANCE. NO PRODUCTION WORK EXECUTED.** '
                              'This is not a healthy-service or successful-delivery result. '
                              'The next scheduled probe resumes work only after HTTP 200; '
                              'manual runs and the separate health alert remain strict.\n')
    if not result['available'] and not result['deferred']:
        raise SystemExit('QUANTURA_GATEWAY_UNAVAILABLE_HTTP_'+str(result['http_status'] or 'NETWORK'))


if __name__=='__main__':
    main()
