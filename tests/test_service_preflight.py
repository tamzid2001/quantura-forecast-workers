import io
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error

from scripts import service_preflight as preflight


class Response:
    def __init__(self,status=200,headers=None):self.status=status;self.headers=headers or {}
    def __enter__(self):return self
    def __exit__(self,*a):return False


class PreflightTests(unittest.TestCase):
    def test_probe_uses_read_only_fixed_origin_without_credentials_or_body(self):
        def open_request(request,timeout):
            self.assertEqual(request.full_url,preflight.HEALTH_URL)
            self.assertEqual(request.get_method(),'HEAD')
            self.assertIsNone(request.data)
            self.assertNotIn('Authorization',dict(request.header_items()))
            self.assertEqual(timeout,15)
            return Response()
        result=preflight.probe(opener=SimpleNamespace(open=open_request))
        self.assertTrue(result['available'])

    def test_http_and_transport_errors_stay_unavailable_and_do_not_leak_data(self):
        for code in [301,401,402,403,429,500,503]:
            headers={'x-vercel-error':'DEPLOYMENT_DISABLED' if code==402 else 'private-error-secret'}
            error=urllib.error.HTTPError(preflight.HEALTH_URL,code,'private-body',headers,None)
            opener=SimpleNamespace(open=lambda *a,**k:(_ for _ in ()).throw(error))
            result=preflight.probe(opener=opener)
            self.assertFalse(result['available']);self.assertEqual(result['http_status'],code)
            self.assertNotIn('private',json.dumps(result))
        opener=SimpleNamespace(open=lambda *a,**k:(_ for _ in ()).throw(urllib.error.URLError('secret')))
        self.assertEqual(preflight.probe(opener=opener)['transport_error'],'network_unavailable')

    def test_redirect_is_not_treated_as_a_healthy_gateway(self):
        self.assertIsNone(preflight.NoRedirect().redirect_request(None,None,302,'',{},'https://untrusted.example'))

    def test_timeout_is_bounded(self):
        for value in [0,31,True,1.5]:
            with self.subTest(value=value),self.assertRaises(ValueError):preflight.probe(timeout=value)

    def test_main_fails_and_records_why_downstream_work_was_not_started(self):
        with tempfile.TemporaryDirectory() as temp:
            summary=Path(temp)/'summary.md'
            result={'available':False,'http_status':402,'platform_error':'DEPLOYMENT_DISABLED'}
            with patch.dict('os.environ',{'GITHUB_STEP_SUMMARY':str(summary)}),patch.object(preflight,'probe',return_value=result),patch('sys.stdout',new=io.StringIO()):
                with self.assertRaisesRegex(SystemExit,'HTTP_402'):preflight.main()
            self.assertIn('Dependent work was not started',summary.read_text())
            with patch.dict('os.environ',{},clear=True),patch.object(preflight,'probe',return_value={'available':True}),patch('sys.stdout',new=io.StringIO()):
                preflight.main()
