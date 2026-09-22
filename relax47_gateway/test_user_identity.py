import unittest
from unittest.mock import patch
from urllib.error import HTTPError
import gateway
class IdentityTests(unittest.TestCase):
    def test_default_identity(self):
        with patch.object(gateway,'HA_USER_TOKEN',''),patch.object(gateway,'SUPERVISOR_TOKEN','service-example'),patch.object(gateway,'request_json',return_value={}) as request:
            gateway.ha_request('/config')
            self.assertEqual(request.call_args.args[0],'http://supervisor/core/api/config')
            self.assertEqual(request.call_args.kwargs['headers']['Authorization'],'Bearer service-example')
    def test_user_direct_core(self):
        with patch.object(gateway,'HA_USER_TOKEN','user-example'),patch.object(gateway,'request_json',return_value={}) as request:
            gateway.ha_request('/services/relax47_integrations/route_notification',method='POST',data={'event':{}})
            self.assertEqual(request.call_args.args[0],'http://homeassistant:8123/api/services/relax47_integrations/route_notification')
            self.assertEqual(request.call_args.kwargs['headers']['Authorization'],'Bearer user-example')
    def test_no_fallback(self):
        for code in (400,401,403,500):
            with patch.object(gateway,'HA_USER_TOKEN','user-example'),patch.object(gateway,'SUPERVISOR_TOKEN','service-example'),patch.object(gateway,'request_json',side_effect=HTTPError('url',code,'error',{},None)) as request:
                with self.assertRaises(HTTPError):gateway.ha_request('/config')
                self.assertEqual(request.call_count,1)
    def test_empty_credentials(self):
        with patch.object(gateway,'HA_USER_TOKEN',''),patch.object(gateway,'SUPERVISOR_TOKEN',''),patch.object(gateway,'request_json') as request:
            with self.assertRaises(RuntimeError):gateway.ha_request('/config')
            request.assert_not_called()
    def test_reject_absolute_paths(self):
        for path in ('https://other.example/api','//other.example/api','config'):
            with self.assertRaises(ValueError):gateway.ha_request(path)
    def test_supervisor_separate(self):
        with patch.object(gateway,'HA_USER_TOKEN','user-example'),patch.object(gateway,'SUPERVISOR_TOKEN','service-example'),patch.object(gateway,'request_json',return_value={'data':{'backups':[]}}) as request:
            gateway.supervisor_backup_ids()
            self.assertEqual(request.call_args.kwargs['headers']['Authorization'],'Bearer service-example')
    def test_redirect_rejected(self):
        import urllib.request
        request=urllib.request.Request('http://homeassistant:8123/api/config',headers={'Authorization':'Bearer example'})
        with self.assertRaises(HTTPError):
            gateway.NoCredentialRedirect().redirect_request(request,None,302,'redirect',{},'https://external.example/')
    def test_status_no_secret(self):
        with patch.object(gateway,'HA_USER_TOKEN','user-example'),patch.object(gateway,'maintenance_manager') as manager,patch.object(gateway,'BackupRetention') as retention,patch.object(gateway,'tunnel_status',return_value={}):
            manager.return_value.status.return_value={};retention.return_value.status.return_value={}
            data=gateway.tool_gateway_status({})
            self.assertEqual(data['home_assistant_identity'],'configured_user');self.assertNotIn('user-example',str(data))
if __name__=='__main__':unittest.main()
