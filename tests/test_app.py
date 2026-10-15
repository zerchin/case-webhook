import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

TEST_WEBHOOK_PATH = '/webhooks/pd/v3/' + ('a' * 64)
os.environ.setdefault('WEBHOOK_PATH', TEST_WEBHOOK_PATH)

from app import WEBHOOK_PATH, app, load_webhook_path, verify_pagerduty_signature


SECRET = 'pagerduty-test-secret'


def sign(payload, secret=SECRET):
    return 'v1=' + hmac.new(
        secret.encode('utf-8'),
        payload,
        hashlib.sha256
    ).hexdigest()


class PagerDutySignatureTests(unittest.TestCase):
    def test_matches_known_hmac_sha256_vector(self):
        payload = b'The quick brown fox jumps over the lazy dog'
        signature = (
            'v1=f7bc83f430538424b13298e6aa6fb143'
            'ef4d59a14946175997479dbc2d1a3cd8'
        )

        self.assertTrue(
            verify_pagerduty_signature(payload, signature, 'key')
        )

    def test_accepts_valid_signature(self):
        payload = b'{"event":{"data":{"title":"Case 1"}}}'

        self.assertTrue(
            verify_pagerduty_signature(payload, sign(payload), SECRET)
        )

    def test_accepts_matching_signature_during_secret_rotation(self):
        payload = b'{"event":{"data":{"title":"Case 1"}}}'
        signatures = f'v1={"0" * 64}, {sign(payload)}'

        self.assertTrue(
            verify_pagerduty_signature(payload, signatures, SECRET)
        )

    def test_rejects_invalid_signature(self):
        payload = b'{"event":{"data":{"title":"Case 1"}}}'

        self.assertFalse(
            verify_pagerduty_signature(payload, f'v1={"0" * 64}', SECRET)
        )


class WebhookPathTests(unittest.TestCase):
    def test_loads_valid_webhook_path(self):
        with patch.dict(
            os.environ,
            {'WEBHOOK_PATH': '/webhooks/pd/v3/' + ('b' * 64)},
            clear=True
        ):
            self.assertEqual(
                load_webhook_path(),
                '/webhooks/pd/v3/' + ('b' * 64)
            )

    def test_rejects_missing_or_invalid_webhook_path(self):
        invalid_paths = [
            '',
            '/webhooks/pd/v3/short',
            '/webhooks/pd/v3/' + ('G' * 64),
            '/other/' + ('a' * 64),
        ]

        for invalid_path in invalid_paths:
            with self.subTest(path=invalid_path):
                with patch.dict(
                    os.environ,
                    {'WEBHOOK_PATH': invalid_path},
                    clear=True
                ):
                    with self.assertRaises(RuntimeError):
                        load_webhook_path()

    def test_old_exposed_path_returns_not_found(self):
        client = app.test_client()

        response = client.post(
            '/5c2df3d1-3371-47bd-a9cf-1983e9adc18b',
            json={}
        )

        self.assertEqual(response.status_code, 404)

    def test_unconfigured_random_path_returns_not_found(self):
        client = app.test_client()

        response = client.post(
            '/webhooks/pd/v3/' + ('b' * 64),
            json={}
        )

        self.assertEqual(response.status_code, 404)


class WebhookAuthenticationTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.payload = json.dumps({
            'event': {
                'data': {
                    'title': 'Case 01602520 - Medium - Customer‘s Company'
                }
            }
        }, separators=(',', ':')).encode('utf-8')

    @patch.dict(os.environ, {'PAGERDUTY_WEBHOOK_SECRET': SECRET})
    def test_rejects_request_without_signature(self):
        response = self.client.post(
            WEBHOOK_PATH,
            data=self.payload,
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 400)

    @patch.dict(os.environ, {'PAGERDUTY_WEBHOOK_SECRET': SECRET})
    def test_rejects_request_with_invalid_signature(self):
        response = self.client.post(
            WEBHOOK_PATH,
            data=self.payload,
            content_type='application/json',
            headers={'X-PagerDuty-Signature': f'v1={"0" * 64}'}
        )

        self.assertEqual(response.status_code, 403)

    @patch.dict(os.environ, {}, clear=True)
    def test_fails_closed_when_secret_is_not_configured(self):
        response = self.client.post(
            WEBHOOK_PATH,
            data=self.payload,
            content_type='application/json',
            headers={'X-PagerDuty-Signature': sign(self.payload)}
        )

        self.assertEqual(response.status_code, 503)

    @patch('app.DatabaseManager')
    @patch.dict(os.environ, {'PAGERDUTY_WEBHOOK_SECRET': SECRET})
    def test_valid_signature_reaches_webhook_processing(self, database_manager):
        database_manager.return_value.case_exists.return_value = True

        response = self.client.post(
            WEBHOOK_PATH,
            data=self.payload,
            content_type='application/json',
            headers={'X-PagerDuty-Signature': sign(self.payload)}
        )

        self.assertEqual(response.status_code, 200)
        database_manager.return_value.case_exists.assert_called_once_with('01602520')

    @patch.dict(os.environ, {'PAGERDUTY_WEBHOOK_SECRET': SECRET})
    def test_logs_ip_headers_and_body_with_sensitive_headers_redacted(self):
        signature = sign(self.payload)

        with self.assertLogs('app', level='INFO') as captured_logs:
            self.client.post(
                WEBHOOK_PATH,
                data=self.payload,
                content_type='application/json',
                headers={
                    'X-PagerDuty-Signature': signature,
                    'X-Test-Header': 'visible-value'
                }
            )

        logs = '\n'.join(captured_logs.output)
        self.assertIn('"remote_ip":"127.0.0.1"', logs)
        self.assertIn('"X-Test-Header":"visible-value"', logs)
        self.assertIn('"body":', logs)
        self.assertIn('Customer‘s Company', logs)
        self.assertIn('[REDACTED]', logs)
        self.assertNotIn(signature, logs)


class SupplementAuthenticationTests(unittest.TestCase):
    def setUp(self):
        app.config['TESTING'] = True
        self.client = app.test_client()
        self.payload = json.dumps({
            'event': {
                'data': {
                    'title': 'Supplementary Case',
                    'case_id': '01602520',
                    'new_support_name': 'Support B',
                    'new_support_id': 'U00000002',
                    'old_support_name': 'Support A',
                    'supplement': 'true'
                }
            }
        }, separators=(',', ':')).encode('utf-8')

    @patch.dict(os.environ, {}, clear=True)
    def test_rejects_unsigned_supplement_from_untrusted_ip(self):
        response = self.client.post(
            WEBHOOK_PATH,
            data=self.payload,
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 403)

    @patch('app.SlackNotifier')
    @patch('app.DatabaseManager')
    @patch.dict(
        os.environ,
        {'SUPPLEMENT_TRUSTED_IPS': '10.0.0.0/8, 127.0.0.1'},
        clear=True
    )
    def test_allows_unsigned_supplement_from_trusted_ip(
        self,
        database_manager,
        slack_notifier
    ):
        slack_notifier.return_value.send_message.return_value = True

        response = self.client.post(
            WEBHOOK_PATH,
            data=self.payload,
            content_type='application/json'
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            'status': 'success',
            'case_id': '01602520'
        })
        database_manager.return_value.update_support_priority.assert_any_call(
            'Support A',
            1
        )
        database_manager.return_value.update_support_priority.assert_any_call(
            'Support B',
            -1
        )


if __name__ == '__main__':
    unittest.main()
