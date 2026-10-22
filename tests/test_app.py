import base64
import hashlib
import hmac
import json
import os
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

import requests

from werkzeug.security import generate_password_hash

import app as server
from database import DEFAULT_CASE_URL, DomainError, SECRET_STORAGE_PREFIX, decode_webhook_secret, encode_webhook_secret
from storage import TestDatabase
from test_pagerduty import CASE_URL, EMAIL_BODY, EXPECTED_MESSAGE, INCIDENT_URL, responses

TEST_WEBHOOK_PATH = '/webhooks/pd/v3/test-token'


def sign(payload, secret='test-secret'):
    return 'v1=' + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        server.app.config.update(TESTING=True, ADMIN_PASSWORD_HASH=generate_password_hash('test-password', method='pbkdf2:sha256:600000'))

    def setUp(self):
        self.db = TestDatabase()
        self.addCleanup(self.db.cleanup)
        patcher = patch.object(server, 'db', self.db)
        patcher.start(); self.addCleanup(patcher.stop)
        patcher = patch.object(server, 'schema_ready', True)
        patcher.start(); self.addCleanup(patcher.stop)
        self.client = server.app.test_client()
        self.token = 'test-csrf-token'
        with self.client.session_transaction() as session:
            session.update(admin=True, csrf_token=self.token)
        self.headers = {'X-CSRF-Token': self.token}

    def post(self, path, data):
        return self.client.post(path, json=data, headers=self.headers)

    def stored_secret(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT pagerduty_webhook_secret FROM app_settings WHERE id = 1')
                return cur.fetchone()['pagerduty_webhook_secret']

    def webhook(self, details=None, signature=True, path=TEST_WEBHOOK_PATH):
        payload = json.dumps({'event': {'data': details or {
            'title': 'Case 001 - Medium - Account', 'self': INCIDENT_URL}}}).encode()
        headers = {'X-PagerDuty-Signature': sign(payload)} if signature else {}
        return self.client.post(path, data=payload, content_type='application/json', headers=headers)

    def test_authentication_and_csrf_required_for_every_mutation(self):
        paths = [('/api/support', 'post'), ('/api/support/A', 'patch'), ('/api/cases', 'post'),
                 ('/api/support/A', 'delete'), ('/api/cases/bulk-delete', 'post'),
                 ('/api/cases/001/reassign', 'post'), ('/api/cases/001', 'delete'), ('/api/settings', 'put'), ('/api/logout', 'post')]
        anonymous = server.app.test_client()
        for path, method in paths:
            with self.subTest(path=path):
                self.assertEqual(getattr(anonymous, method)(path, json={}).status_code, 401)
                self.assertEqual(getattr(self.client, method)(path, json={}).status_code, 403)
        self.assertEqual(anonymous.get('/api/settings').status_code, 401)
        for page in ('/', '/support', '/cases', '/settings'):
            self.assertEqual(anonymous.get(page).status_code, 302)

    def test_login_and_logout(self):
        client = server.app.test_client()
        self.assertEqual(client.get('/login').status_code, 200)
        with client.session_transaction() as session:
            token = session['csrf_token']
        self.assertEqual(client.post('/login', data={'username': 'admin', 'password': 'test-password'}).status_code, 403)
        self.assertEqual(client.post('/login', data={'username': 'admin', 'password': 'wrong', 'csrf_token': token}).status_code, 400)
        response = client.post('/login', data={'username': 'admin', 'password': 'test-password', 'csrf_token': token})
        self.assertEqual(response.status_code, 302)
        self.assertEqual(client.get('/settings').status_code, 200)
        with client.session_transaction() as session:
            new_token = session['csrf_token']
        self.assertNotEqual(token, new_token)
        self.assertEqual(client.post('/api/logout', headers={'X-CSRF-Token': new_token}).status_code, 200)
        self.assertEqual(client.get('/api/support').status_code, 401)

    def test_no_default_admin_password(self):
        with patch.dict(server.app.config, {'ADMIN_PASSWORD_HASH': None}):
            response = self.client.post('/login', data={'csrf_token': self.token, 'username': 'admin', 'password': 'admin'})
            self.assertEqual(response.status_code, 400)

    def test_pages_and_assets_share_the_same_app(self):
        for path in ('/support', '/cases', '/settings', '/static/app.js', '/static/app.css'):
            with self.client.get(path) as response:
                self.assertEqual(response.status_code, 200)
        response = self.client.get('/support')
        self.assertIn('lang="en"', response.text)
        self.assertIn("script-src 'self'", response.headers['Content-Security-Policy'])

    def test_add_support_defaults_and_utc_serialization(self):
        self.assertEqual(self.post('/api/support', {'name': 'Alice', 'id': 'A'}).status_code, 201)
        row = self.client.get('/api/support').json[0]
        self.assertEqual(row['status'], 'online')
        self.assertEqual(row['priority'], 100)
        self.assertEqual(row['updated_at'], '2030-01-01T12:00:00Z')
        self.assertEqual(row['created_at'], '2030-01-01T12:00:00Z')
        self.assertEqual(set(row), {'name', 'id', 'status', 'priority', 'updated_at', 'created_at'})
        self.assertEqual(self.post('/api/support', {'name': 'Alice', 'id': 'A'}).status_code, 409)

    def test_support_validation(self):
        for data in ({'id': 'A'}, {'name': 'A'}, {'id': 'A', 'name': 'A', 'priority': True},
                     {'id': 'A', 'name': 'A', 'priority': 1.5}, {'id': 'A', 'name': 'A', 'status': 'away'}):
            self.assertEqual(self.post('/api/support', data).status_code, 400)

    def test_local_timestamp_is_converted_to_utc(self):
        self.db.seed_support()
        response = self.client.patch('/api/support/A', json={'updated_at': '2026-10-21T20:30:15+08:00'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.client.get('/api/support').json[0]['updated_at'], '2026-10-21T12:30:15Z')
        for value in ('2026-10-21T20:30:00', '', None, 'nonsense'):
            self.assertEqual(self.client.patch('/api/support/A', json={'updated_at': value}, headers=self.headers).status_code, 400)

    def test_support_defaults_to_oldest_first_and_extra_columns_are_returned(self):
        self.db.seed_support('B', updated_at='2021-01-01 00:00:00')
        self.db.seed_support('A')
        self.assertEqual([row['id'] for row in self.client.get('/api/support').json], ['A', 'B'])
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('ALTER TABLE support_list ADD COLUMN extra TEXT')
            conn.commit()
        self.assertIn('extra', self.client.get('/api/support').json[0])

    def test_settings_use_database_only_and_mask_credentials(self):
        with patch.dict(os.environ, {'WEBHOOK_PATH': '/env-path', 'PAGERDUTY_WEBHOOK_SECRET': 'env-secret',
                                    'SLACK_Webhook_URL': 'https://env.example', 'PAGERDUTY_API_TOKEN': 'env-token'}):
            response = self.client.get('/api/settings')
            self.assertEqual(response.json['webhook_path'], TEST_WEBHOOK_PATH)
            self.assertNotIn('test-secret', response.text)
            self.assertNotIn('slack.example', response.text)
            self.assertFalse(response.json['pagerduty_api_token_configured'])
            self.assertEqual(self.db.settings()['pagerduty_api_token'], '')
            response = self.client.put('/api/settings', json={'webhook_path': '/webhooks/pd/v3/old-uuid-address'}, headers=self.headers)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(self.db.settings()['pagerduty_webhook_secret'], 'test-secret')
            self.assertEqual(self.webhook().status_code, 404)
        self.client.put('/api/settings', json={'slack_webhook_url': '', 'pagerduty_webhook_secret': ''}, headers=self.headers)
        self.assertEqual(self.db.settings()['slack_webhook_url'], '')
        self.assertFalse(self.client.get('/api/settings').json['pagerduty_webhook_secret_configured'])

    def test_invalid_setting_does_not_partially_save(self):
        for path in ('/api', '/login', '/api/foo', 'https://example.com/hook', '/hook?x=1', '/a//b', '/',
                     '/legacy-webhook', '/webhooks/pd/v3/', '/webhooks/pd/v3/a/b',
                     '/webhooks/pd/v3/token?x=1', '/webhooks/pd/v3/token%2Fpart',
                     '/WEBHOOKS/pd/v3/token', '/webhooks/pd/v3/' + ('a' * 240)):
            response = self.client.put('/api/settings', json={'webhook_path': path}, headers=self.headers)
            self.assertEqual(response.status_code, 400, path)
        response = self.client.put('/api/settings', json={'webhook_path': '/webhooks/pd/v3/new', 'slack_webhook_url': 'bad'}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.settings()['webhook_path'], TEST_WEBHOOK_PATH)

    def test_fixed_webhook_prefix_accepts_generated_and_custom_suffixes(self):
        for suffix in ('a' * 64, '5c2df3d1-3371-47bd-a9cf-1983e9adc18b', 'Custom_Token-2026', 'a' * 239):
            with self.subTest(suffix=suffix):
                path = '/webhooks/pd/v3/' + suffix
                response = self.client.put('/api/settings', json={'webhook_path': path}, headers=self.headers)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json['webhook_path'], path)
                self.assertEqual(self.db.settings()['webhook_path'], path)

    def test_nonstandard_stored_path_is_disabled_without_automatic_rewrite(self):
        self.db.save_settings({'webhook_path': '/legacy-webhook'})
        self.db.initialize()
        self.assertEqual(self.client.get('/api/settings').json['webhook_path'], '/legacy-webhook')
        self.assertEqual(self.webhook(path='/legacy-webhook').status_code, 404)
        self.assertEqual(self.webhook(path='/webhooks/pd/v3/legacy-webhook').status_code, 404)
        self.assertEqual(self.db.settings()['webhook_path'], '/legacy-webhook')
        new_path = '/webhooks/pd/v3/new-token'
        self.db.seed_case()
        self.assertEqual(self.client.put('/api/settings', json={'webhook_path': new_path}, headers=self.headers).status_code, 200)
        self.assertEqual(self.webhook(path=new_path).status_code, 200)
        self.assertEqual(self.webhook(path='/legacy-webhook').status_code, 404)

    def test_empty_suffix_disables_webhook_and_other_prefixed_paths_do_not_match(self):
        self.assertEqual(self.webhook(path='/webhooks/pd/v3/other-token').status_code, 404)
        self.assertEqual(self.client.put('/api/settings', json={'webhook_path': ''}, headers=self.headers).status_code, 200)
        self.assertEqual(self.db.settings()['webhook_path'], '')
        self.assertEqual(self.webhook().status_code, 404)

    def test_secret_saved_as_base64_without_exposing_raw_or_encoded_value(self):
        secret = 'updated-signing-secret-\u4e2d\u6587'
        response = self.client.put('/api/settings', json={'pagerduty_webhook_secret': secret}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        stored = self.stored_secret()
        self.assertTrue(stored.startswith(SECRET_STORAGE_PREFIX))
        self.assertEqual(base64.b64decode(stored[len(SECRET_STORAGE_PREFIX):]).decode('utf-8'), secret)
        self.assertEqual(self.db.settings()['pagerduty_webhook_secret'], secret)
        for result in (response, self.client.get('/api/settings')):
            self.assertTrue(result.json['pagerduty_webhook_secret_configured'])
            self.assertNotIn(secret, result.text)
            self.assertNotIn(stored, result.text)

    def test_legacy_secret_migration_is_idempotent_and_preserves_other_settings(self):
        original = self.db.settings()
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE app_settings SET pagerduty_webhook_secret = %s WHERE id = 1', ('legacy-test-secret',))
            conn.commit()
        self.assertEqual(self.db.settings()['pagerduty_webhook_secret'], 'legacy-test-secret')
        self.db.initialize()
        stored = self.stored_secret()
        self.assertTrue(stored.startswith(SECRET_STORAGE_PREFIX))
        self.assertEqual(base64.b64decode(stored[len(SECRET_STORAGE_PREFIX):]), b'legacy-test-secret')
        self.db.initialize()
        self.assertEqual(self.stored_secret(), stored)
        settings = self.db.settings()
        self.assertEqual(settings['webhook_path'], original['webhook_path'])
        self.assertEqual(settings['slack_webhook_url'], original['slack_webhook_url'])

    def test_saving_unrelated_settings_keeps_encoded_secret_and_clearing_removes_it(self):
        before = self.stored_secret()
        self.assertTrue(before.startswith(SECRET_STORAGE_PREFIX))
        self.client.put('/api/settings', json={'webhook_path': '/webhooks/pd/v3/new-path'}, headers=self.headers)
        self.assertEqual(self.stored_secret(), before)
        response = self.client.put('/api/settings', json={'pagerduty_webhook_secret': ''}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.stored_secret(), '')
        self.assertFalse(response.json['pagerduty_webhook_secret_configured'])
        self.db.initialize()
        self.assertEqual(self.stored_secret(), '')

    @patch('app.SlackNotifier.send_message', return_value=True)
    def test_webhook_verifies_with_decoded_secret_after_save_and_restart(self, notify):
        self.db.seed_support()
        secret = 'replacement-signing-secret'
        self.client.put('/api/settings', json={'pagerduty_webhook_secret': secret}, headers=self.headers)
        self.db.initialize()
        payload = b'{"event":{"data":{"title":"Case 001 - Medium - Acme"}}}'
        self.assertEqual(self.client.post(TEST_WEBHOOK_PATH, data=payload, content_type='application/json',
                                         headers={'X-PagerDuty-Signature': sign(payload)}).status_code, 403)
        response = self.client.post(TEST_WEBHOOK_PATH, data=payload, content_type='application/json',
                                    headers={'X-PagerDuty-Signature': sign(payload, secret)})
        self.assertEqual(response.status_code, 200)
        notify.assert_called_once()

    def test_invalid_encoded_secret_fails_closed_without_leaking_it(self):
        self.db.seed_support()
        for stored in ('base64:v1:%%%corrupt-secret%%%', 'base64:v1:/w=='):
            with self.db.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute('UPDATE app_settings SET pagerduty_webhook_secret = %s WHERE id = 1', (stored,))
                conn.commit()
            response = self.webhook()
            self.assertEqual(response.status_code, 503)
            self.assertNotIn(stored, response.text)
            self.assertEqual(self.db.list_rows('cases'), [])
        response = self.client.put('/api/settings', json={'pagerduty_webhook_secret': 'fixed-secret'}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.settings()['pagerduty_webhook_secret'], 'fixed-secret')

    def test_secret_literal_with_storage_prefix_and_unicode_round_trips(self):
        for secret in ('', 'ascii-signing-secret', '\u7b7e\u540d\u5bc6\u94a5', 'base64:v1:dGVzdA=='):
            with self.subTest(secret=secret):
                self.db.save_settings({'pagerduty_webhook_secret': secret})
                self.db.initialize()
                self.assertEqual(self.db.settings()['pagerduty_webhook_secret'], secret)
                self.assertEqual(decode_webhook_secret(encode_webhook_secret(secret)), secret)

    @patch('app.SlackNotifier.send_message', return_value=True)
    def test_ui_case_notification_uses_three_lines_and_existing_queue(self, notify):
        self.db.seed_support()
        response = self.post('/api/cases', {'id': '001', 'account_name': 'Acme'})
        self.assertEqual(response.status_code, 201)
        notify.assert_called_once_with('Case: 001\nPriority: Medium\nAccount: Acme', DEFAULT_CASE_URL, 'A')
        self.assertEqual(self.client.get('/api/cases').json[0]['support_id'], 'A')
        self.assertEqual(self.client.get('/api/cases').json[0]['url'], DEFAULT_CASE_URL)
        self.assertEqual(self.post('/api/cases', {'id': '001', 'account_name': 'Acme'}).status_code, 409)
        self.assertEqual(notify.call_count, 1)

    @patch('app.SlackNotifier.send_message', return_value=True)
    def test_ui_advanced_fields_enrich_message_and_save_only_url(self, notify):
        self.db.seed_support(); self.db.seed_support('B', 'Support B', status='offline')
        response = self.post('/api/cases', {'id': '001', 'account_name': '客户公司', 'priority': 'High',
                                          'subject': '  controller plane组件流量分析  ',
                                          'product': 'SUSE Rancher Prime: RKE2', 'url': CASE_URL})
        self.assertEqual(response.status_code, 201)
        notify.assert_called_once_with('Case: 001\nPriority: High\nSubject: controller plane组件流量分析\n'
                                      'Account: 客户公司\nProduct: SUSE Rancher Prime: RKE2', CASE_URL, 'A')
        case = self.client.get('/api/cases').json[0]
        self.assertEqual(case['url'], CASE_URL)
        self.assertNotIn('subject', case)
        self.assertNotIn('product', case)
        self.assertEqual(self.post('/api/cases/001/reassign', {'support_id': 'B'}).status_code, 200)
        notify.assert_called_with('Case: 001\nNote: Backfilled case, Owner Changed From Support A to Support B',
                                  CASE_URL, 'B')

    @patch('app.SlackNotifier.send_message', return_value=True)
    def test_ui_advanced_fields_are_individually_optional(self, notify):
        self.db.seed_support()
        examples = [({'subject': '组件分析'}, 'Case: 001\nPriority: Medium\nSubject: 组件分析\nAccount: Acme'),
                    ({'product': 'RKE2'}, 'Case: 002\nPriority: Medium\nAccount: Acme\nProduct: RKE2'),
                    ({'subject': ' ', 'product': '', 'url': ' '}, 'Case: 003\nPriority: Medium\nAccount: Acme')]
        for index, (extra, message) in enumerate(examples, 1):
            with self.subTest(extra=extra):
                response = self.post('/api/cases', {'id': f'{index:03}', 'account_name': 'Acme', **extra})
                self.assertEqual(response.status_code, 201)
                notify.assert_called_with(message, DEFAULT_CASE_URL, 'A')

    @patch('app.SlackNotifier.send_message')
    def test_ui_notification_disabled_saves_url_and_ignores_subject_product(self, notify):
        self.db.seed_support(status='offline')
        before = self.db.list_rows('support_list')
        response = self.post('/api/cases', {'id': '001', 'notify': False, 'support_id': 'A',
                                          'subject': 'Subject not saved', 'product': 'Product not saved', 'url': CASE_URL})
        self.assertEqual(response.status_code, 201)
        case = self.client.get('/api/cases').json[0]
        self.assertEqual(case['url'], CASE_URL)
        self.assertNotIn('subject', case)
        self.assertNotIn('product', case)
        self.assertEqual(self.db.list_rows('support_list'), before)
        notify.assert_not_called()

    def test_manual_case_url_is_validated_even_without_notification(self):
        self.db.seed_support(status='offline')
        for url in ('javascript:alert(1)', 'https://', None):
            with self.subTest(url=url):
                response = self.post('/api/cases', {'id': '001', 'notify': False, 'support_id': 'A', 'url': url})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.list_rows('cases'), [])
        response = self.post('/api/cases', {'id': '001', 'notify': False, 'support_id': 'A', 'url': ''})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.db.list_rows('cases')[0]['url'], DEFAULT_CASE_URL)

    def test_ui_invalid_advanced_values_do_not_change_queue(self):
        self.db.seed_support(priority=101)
        before = self.db.list_rows('support_list')
        for extra in ({'subject': None}, {'subject': 'x' * 1001}, {'product': []}, {'product': 'x' * 256},
                      {'url': False}, {'url': 'x' * 2049}, {'url': 'javascript:alert(1)'},
                      {'url': 'https://'}, {'url': 'https://example.com:invalid/'},
                      {'url': 'https://user:password@example.com/'}, {'url': 'https://example.com/a b'}):
            with self.subTest(extra=extra):
                response = self.post('/api/cases', {'id': '001', 'account_name': 'Acme', **extra})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.list_rows('cases'), [])
        self.assertEqual(self.db.list_rows('support_list'), before)

    @patch('app.SlackNotifier.send_message')
    def test_manual_case_needs_only_id_and_offline_support(self, notify):
        self.db.seed_support(status='offline')
        before = self.db.list_rows('support_list')
        response = self.post('/api/cases', {'id': '002', 'notify': False, 'support_id': 'A'})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(self.db.list_rows('support_list'), before)
        self.assertEqual(self.db.list_rows('cases')[0]['url'], DEFAULT_CASE_URL)
        notify.assert_not_called()

    def test_case_validation_and_no_online_support(self):
        for data in ({}, {'id': '001'}, {'id': '001', 'notify': False},
                     {'id': '001', 'notify': 'false'}, {'id': '001', 'priority': 'Urgent', 'account_name': 'Acme'}):
            self.assertEqual(self.post('/api/cases', data).status_code, 400)
        self.db.seed_support(status='offline')
        self.assertEqual(self.post('/api/cases', {'id': '001', 'account_name': 'Acme'}).status_code, 409)
        self.assertEqual(self.db.list_rows('cases'), [])

    @patch('app.SlackNotifier.send_message', return_value=False)
    def test_slack_failure_keeps_case_and_reports_partial_success(self, notify):
        self.db.seed_support()
        response = self.post('/api/cases', {'id': '001', 'account_name': 'Acme'})
        self.assertEqual(response.status_code, 207)
        self.assertEqual(response.json['status'], 'partial_success')
        self.assertEqual(len(self.db.list_rows('cases')), 1)

    @patch('app.SlackNotifier.send_message', return_value=True)
    def test_reassign_defaults_adjust_priorities_notify_and_preserve_times(self, notify):
        self.db.seed_support()
        self.db.seed_support('B', 'Support B', status='offline')
        self.db.seed_case()
        before = {row['id']: row['updated_at'] for row in self.db.list_rows('support_list')}
        self.assertEqual(self.post('/api/cases/001/reassign', {'support_id': 'B'}).status_code, 200)
        people = self.db.list_rows('support_list')
        self.assertEqual({row['id']: row['priority'] for row in people}, {'A': 101, 'B': 99})
        self.assertEqual({row['id']: row['updated_at'] for row in people}, before)
        self.assertEqual(self.db.list_rows('cases')[0]['support_id'], 'B')
        notify.assert_called_once_with('Case: 001\nNote: Backfilled case, Owner Changed From Support A to Support B',
                                     DEFAULT_CASE_URL, 'B')

    @patch('app.SlackNotifier.send_message', return_value=True)
    def test_reassign_uses_saved_case_url_historical_owner_and_new_support_uid(self, notify):
        self.db.seed_support(); self.db.seed_support('B', 'Support B')
        self.db.seed_case(url=CASE_URL)
        self.db.delete_support('A')
        self.assertEqual(self.post('/api/cases/001/reassign', {'support_id': 'B'}).status_code, 200)
        notify.assert_called_once_with('Case: 001\nNote: Backfilled case, Owner Changed From Support A to Support B',
                                     CASE_URL, 'B')
        self.assertEqual(self.db.list_rows('cases')[0]['url'], CASE_URL)

    def test_api_token_settings_are_encoded_masked_preserved_and_clearable(self):
        token = 'fake-pagerduty-api-token'
        response = self.client.put('/api/settings', json={'pagerduty_api_token': token}, headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json['pagerduty_api_token_configured'])
        self.assertEqual(self.db.settings()['pagerduty_api_token'], token)
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT pagerduty_api_token FROM app_settings WHERE id = 1')
                stored = cur.fetchone()['pagerduty_api_token']
        self.assertTrue(stored.startswith(SECRET_STORAGE_PREFIX))
        self.assertNotIn(token, stored)
        for result in (response, self.client.get('/api/settings'), self.client.get('/settings')):
            self.assertNotIn(token, result.text)
            self.assertNotIn(stored, result.text)
        self.client.put('/api/settings', json={'webhook_path': TEST_WEBHOOK_PATH}, headers=self.headers)
        self.db.initialize()
        self.assertEqual(self.db.settings()['pagerduty_api_token'], token)
        self.assertTrue(self.client.get('/api/settings').json['pagerduty_api_token_configured'])
        response = self.client.put('/api/settings', json={'pagerduty_api_token': ''}, headers=self.headers)
        self.assertFalse(response.json['pagerduty_api_token_configured'])
        self.assertEqual(self.db.settings()['pagerduty_api_token'], '')
        for value in (True, None, 123, 'x' * 8193):
            response = self.client.put('/api/settings', json={'pagerduty_api_token': value}, headers=self.headers)
            self.assertEqual(response.status_code, 400)

    @patch('app.requests.post', return_value=Mock(status_code=200))
    @patch('pagerduty.requests.get')
    def test_webhook_enriches_email_and_sends_exact_slack_payload(self, get, post):
        self.db.seed_support('U123456789', 'Selected Support')
        self.db.save_settings({'pagerduty_api_token': 'fake-api-token'})
        get.side_effect = responses()
        response = self.webhook({'title': 'Case 02083291 - Medium - HEFEI LCFC INFORMATION TECHNOLOGY CO LTD',
                                 'self': INCIDENT_URL})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(get.call_count, 2)
        post.assert_called_once_with('https://slack.example/workflow', json={
            'message': EXPECTED_MESSAGE, 'url': CASE_URL, 'uid': 'U123456789'}, timeout=10)
        case = self.client.get('/api/cases').json[0]
        self.assertEqual(case['id'], '02083291')
        self.assertEqual(case['url'], CASE_URL)
        self.assertEqual(case['support_id'], 'U123456789')

    @patch('app.SlackNotifier.send_message', return_value=True)
    @patch('pagerduty.requests.get')
    def test_webhook_falls_back_to_three_fields_on_each_enrichment_failure(self, get, notify):
        self.db.seed_support('U123456789', 'Selected Support', priority=110)
        self.db.save_settings({'pagerduty_api_token': 'fake-api-token'})
        failures = [[requests.Timeout()], [Mock(status_code=401)],
                    [Mock(status_code=200, json=Mock(side_effect=ValueError('bad JSON')))],
                    [Mock(status_code=200, json=Mock(return_value={}))],
                    [responses()[0], requests.Timeout()],
                    [responses()[0], Mock(status_code=200, json=Mock(return_value={'log_entry': {}}))],
                    responses(EMAIL_BODY.replace('Product:', 'Other:')),
                    responses(EMAIL_BODY.replace(CASE_URL, '')),
                    responses(EMAIL_BODY.replace('02083291', '99999999'))]
        for index, failure in enumerate(failures):
            case_id = f'00{index}'
            with self.subTest(failure=index):
                get.side_effect = failure
                response = self.webhook({'title': f'Case {case_id} - High - Customer - APAC', 'self': INCIDENT_URL})
                self.assertEqual(response.status_code, 200)
                notify.assert_called_with(f'Case: {case_id}\nPriority: High\nAccount: Customer - APAC',
                                          DEFAULT_CASE_URL, 'U123456789')
        self.assertEqual(len(self.db.list_rows('cases')), len(failures))
        self.assertTrue(all(row['url'] == DEFAULT_CASE_URL for row in self.db.list_rows('cases')))
        self.assertEqual(self.db.list_rows('support_list')[0]['priority'], 110 - len(failures))

    @patch('app.SlackNotifier.send_message', return_value=True)
    @patch('pagerduty.requests.get')
    def test_webhook_missing_token_or_self_uses_fallback_without_api_request(self, get, notify):
        self.db.seed_support()
        self.assertEqual(self.webhook().status_code, 200)
        notify.assert_called_once_with('Case: 001\nPriority: Medium\nAccount: Account', DEFAULT_CASE_URL, 'A')
        self.db.save_settings({'pagerduty_api_token': 'fake-api-token'})
        self.assertEqual(self.webhook({'title': 'Case 002 - Low - Account'}).status_code, 200)
        notify.assert_called_with('Case: 002\nPriority: Low\nAccount: Account', DEFAULT_CASE_URL, 'A')
        get.assert_not_called()

    @patch('app.SlackNotifier.send_message')
    @patch('pagerduty.requests.get')
    def test_duplicate_is_skipped_before_pagerduty_and_queue_changes(self, get, notify):
        self.db.seed_support(priority=102); self.db.seed_case()
        before = self.db.list_rows('support_list')
        response = self.webhook()
        self.assertEqual(response.json['status'], 'skipped')
        get.assert_not_called(); notify.assert_not_called()
        self.assertEqual(self.db.list_rows('support_list'), before)

    @patch('app.SlackNotifier.send_message')
    @patch('pagerduty.requests.get')
    def test_duplicate_created_during_enrichment_is_not_assigned_again(self, get, notify):
        self.db.seed_support(priority=102)
        self.db.save_settings({'pagerduty_api_token': 'fake-api-token'})
        get.side_effect = responses()
        original = self.db.create_case

        def competing_case(*args, **kwargs):
            self.db.seed_case('02083291', url=CASE_URL)
            return original(*args, **kwargs)

        with patch.object(self.db, 'create_case', side_effect=competing_case):
            response = self.webhook({'title': 'Case 02083291 - Medium - Account', 'self': INCIDENT_URL})
        self.assertEqual(response.json['status'], 'skipped')
        self.assertEqual(self.db.list_rows('support_list')[0]['priority'], 102)
        notify.assert_not_called()

    @patch('app.SlackNotifier.send_message', return_value=False)
    @patch('pagerduty.requests.get')
    def test_enriched_case_survives_slack_failure_and_retry_is_skipped(self, get, notify):
        self.db.seed_support()
        self.db.save_settings({'pagerduty_api_token': 'fake-api-token'})
        get.side_effect = responses()
        details = {'title': 'Case 02083291 - Medium - Account', 'self': INCIDENT_URL}
        response = self.webhook(details)
        self.assertEqual(response.status_code, 207)
        self.assertEqual(self.db.list_rows('cases')[0]['url'], CASE_URL)
        self.assertEqual(self.webhook(details).json['status'], 'skipped')
        self.assertEqual(get.call_count, 2)
        notify.assert_called_once_with(EXPECTED_MESSAGE, CASE_URL, 'A')

    @patch('app.SlackNotifier.send_message')
    def test_reassign_flags_are_independent(self, notify):
        self.db.seed_support(); self.db.seed_support('B', 'Support B'); self.db.seed_case()
        before = self.db.list_rows('support_list')
        self.assertEqual(self.post('/api/cases/001/reassign', {'support_id': 'B', 'notify': False, 'adjust_priority': False}).status_code, 200)
        self.assertEqual(self.db.list_rows('support_list'), before)
        notify.assert_not_called()

    def test_delete_does_not_change_support(self):
        self.db.seed_support(); self.db.seed_case()
        before = self.db.list_rows('support_list')
        self.assertEqual(self.client.delete('/api/cases/001', headers=self.headers).status_code, 200)
        self.assertEqual(self.db.list_rows('cases'), [])
        self.assertEqual(self.db.list_rows('support_list'), before)
        self.assertEqual(self.client.delete('/api/cases/001', headers=self.headers).status_code, 404)

    def test_case_id_accepts_only_ascii_digits_and_keeps_leading_zeros(self):
        self.db.seed_support()
        for case_id in ('abc', '001x', '1e3', '+1', '-1', '1.5', ' 001', '001 ', '1\n', '\uff11\uff12', 123, True, '1' * 31):
            with self.subTest(case_id=case_id):
                response = self.post('/api/cases', {'id': case_id, 'notify': False, 'support_id': 'A'})
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.db.list_rows('cases'), [])
        case_id = '000000000000000000000000000001'
        self.assertEqual(self.post('/api/cases', {'id': case_id, 'notify': False, 'support_id': 'A'}).status_code, 201)
        self.assertEqual(self.db.list_rows('cases')[0]['id'], case_id)

    def test_support_deletion_keeps_historical_cases_and_other_support(self):
        self.db.seed_support(); self.db.seed_support('B', 'Support B'); self.db.seed_case()
        cases = self.db.list_rows('cases')
        other = next(row for row in self.db.list_rows('support_list') if row['id'] == 'B')
        self.assertEqual(self.client.delete('/api/support/A', headers=self.headers).status_code, 200)
        self.assertEqual(self.db.list_rows('cases'), cases)
        self.assertEqual(self.db.list_rows('support_list'), [other])
        self.assertEqual(self.client.delete('/api/support/A', headers=self.headers).status_code, 404)

    @patch('app.SlackNotifier.send_message')
    def test_reassign_after_support_deletion_only_decrements_new_support(self, notify):
        self.db.seed_support(); self.db.seed_support('B', 'Support B', status='offline'); self.db.seed_case()
        before = next(row for row in self.db.list_rows('support_list') if row['id'] == 'B')
        created = self.db.list_rows('cases')[0]['created_at']
        self.db.delete_support('A')
        response = self.post('/api/cases/001/reassign', {'support_id': 'B', 'notify': False})
        self.assertEqual(response.status_code, 200)
        after = self.db.list_rows('support_list')[0]
        self.assertEqual(after['priority'], 99)
        self.assertEqual(after['updated_at'], before['updated_at'])
        self.assertEqual(self.db.list_rows('cases')[0]['support_id'], 'B')
        self.assertEqual(self.db.list_rows('cases')[0]['created_at'], created)
        notify.assert_not_called()

    def test_reassign_after_support_deletion_can_skip_priority_adjustment(self):
        self.db.seed_support(); self.db.seed_support('B'); self.db.seed_case()
        self.db.delete_support('A')
        before = self.db.list_rows('support_list')
        response = self.post('/api/cases/001/reassign', {'support_id': 'B', 'notify': False, 'adjust_priority': False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.db.list_rows('support_list'), before)

    def test_bulk_delete_only_requested_cases_preserves_support(self):
        self.db.seed_support()
        for case_id in ('001', '002', '003'):
            self.db.seed_case(case_id)
        before = self.db.list_rows('support_list')
        response = self.post('/api/cases/bulk-delete', {'ids': ['001', '003', '001']})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json['deleted_count'], 2)
        self.assertEqual([row['id'] for row in self.db.list_rows('cases')], ['002'])
        self.assertEqual(self.db.list_rows('support_list'), before)

    def test_bulk_delete_rejects_invalid_payload_without_mutation(self):
        self.db.seed_support(); self.db.seed_case()
        for ids in (None, [], '001', [1], [''], ['1' * 31], ['001', {}]):
            with self.subTest(ids=ids):
                self.assertEqual(self.post('/api/cases/bulk-delete', {'ids': ids}).status_code, 400)
                self.assertEqual(len(self.db.list_rows('cases')), 1)

    def test_stale_or_injected_bulk_ids_cannot_delete_other_cases(self):
        self.db.seed_support(); self.db.seed_case()
        for case_ids in (['001', 'MISSING'], ['001', "001') OR 1=1 --"]):
            response = self.post('/api/cases/bulk-delete', {'ids': case_ids})
            self.assertEqual(response.status_code, 409)
            self.assertEqual([row['id'] for row in self.db.list_rows('cases')], ['001'])

    def test_bulk_delete_failure_rolls_back_all_cases(self):
        self.db.seed_support(); self.db.seed_case('001'); self.db.seed_case('002')
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TRIGGER fail_delete BEFORE DELETE ON cases WHEN OLD.id = '002' BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            conn.commit()
        response = self.post('/api/cases/bulk-delete', {'ids': ['001', '002']})
        self.assertEqual(response.status_code, 409)
        self.assertEqual({row['id'] for row in self.db.list_rows('cases')}, {'001', '002'})

    @patch('app.SlackNotifier.send_message', return_value=True)
    def test_webhook_authentication_duplicate_and_live_config(self, notify):
        self.db.seed_support()
        anonymous = server.app.test_client()
        self.assertEqual(anonymous.post(TEST_WEBHOOK_PATH, json={'event': {'data': {'title': 'Case 001'}}}).status_code, 400)
        payload = b'{"event":{"data":{"title":"Case 001 - High - Acme"}}}'
        self.assertEqual(anonymous.post(TEST_WEBHOOK_PATH, data=payload, content_type='application/json', headers={'X-PagerDuty-Signature': 'v1=bad'}).status_code, 403)
        response = anonymous.post(TEST_WEBHOOK_PATH, data=payload, content_type='application/json', headers={'X-PagerDuty-Signature': sign(payload)})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.webhook().json['status'], 'skipped')
        notify.assert_called_once()
        self.db.save_settings({'webhook_path': '/webhooks/pd/v3/new-path', 'pagerduty_webhook_secret': ''})
        self.assertEqual(self.webhook().status_code, 404)
        self.assertEqual(self.webhook(path='/webhooks/pd/v3/new-path').status_code, 503)

    def test_external_supplement_is_disabled_even_when_signed_or_trusted(self):
        with patch.dict(os.environ, {'SUPPLEMENT_TRUSTED_IPS': '127.0.0.1'}):
            for value in (True, 'true', 'TRUE', False, 1):
                self.assertEqual(self.webhook({'supplement': value}).status_code, 403)
                self.assertEqual(self.webhook({'supplement': value}, signature=False).status_code, 403)
        self.assertEqual(self.db.list_rows('cases'), [])

    def test_malformed_webhook_and_signature_vectors(self):
        for value in ([], None, {'event': []}, {'event': {'data': []}}):
            response = self.client.post(TEST_WEBHOOK_PATH, data=json.dumps(value), content_type='application/json')
            self.assertEqual(response.status_code, 400)
        payload = b'The quick brown fox jumps over the lazy dog'
        vector = 'v1=f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8'
        self.assertTrue(server.verify_pagerduty_signature(payload, vector, 'key'))
        self.assertTrue(server.verify_pagerduty_signature(payload, 'v1=bad, ' + vector, 'key'))
        self.assertFalse(server.verify_pagerduty_signature(payload, 'v1=\u4e2d\u6587', 'key'))

    def test_logs_redact_signature_and_cookie(self):
        with self.assertLogs('app', level='INFO') as logs:
            self.webhook(signature=False)
        self.assertIn('[REDACTED]', '\n'.join(logs.output))
        self.assertNotIn('session=', '\n'.join(logs.output))


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.db = TestDatabase(); self.addCleanup(self.db.cleanup)

    def test_oldest_online_and_skip_round(self):
        self.db.seed_support('OFF', status='offline', updated_at='2019-01-01 00:00:00')
        self.db.seed_support('A', priority=99)
        self.db.seed_support('B', priority=100, updated_at='2021-01-01 00:00:00')
        self.assertEqual(self.db.create_case('001')['id'], 'B')
        self.assertEqual({row['id']: row['priority'] for row in self.db.list_rows('support_list')}, {'OFF': 100, 'A': 100, 'B': 100})

    def test_high_priority_supplement_keeps_time_and_beats_oldest(self):
        self.db.seed_support('A')
        self.db.seed_support('B', priority=102, updated_at='2021-01-01 00:00:00')
        self.assertEqual(self.db.create_case('001')['id'], 'B')
        row = next(row for row in self.db.list_rows('support_list') if row['id'] == 'B')
        self.assertEqual(row['priority'], 101)
        self.assertEqual(row['updated_at'], datetime(2021, 1, 1))

    def test_extremely_low_priority_finishes_without_unbounded_loop(self):
        self.db.seed_support(priority=-2147483648)
        self.assertEqual(self.db.create_case('001')['id'], 'A')
        self.assertEqual(self.db.list_rows('support_list')[0]['priority'], 100)

    def test_all_below_standard_preserves_rotation_order(self):
        self.db.seed_support('A', priority=98)
        self.db.seed_support('B', priority=99, updated_at='2021-01-01 00:00:00')
        self.assertEqual(self.db.create_case('001')['id'], 'B')
        self.assertEqual([row['priority'] for row in self.db.list_rows('support_list')], [100, 100])

    def test_insert_failure_rolls_back_queue_changes(self):
        self.db.seed_support(priority=101)
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TRIGGER fail_insert BEFORE INSERT ON cases BEGIN SELECT RAISE(ABORT, 'test failure'); END")
            conn.commit()
        with self.assertRaises(Exception):
            self.db.create_case('001')
        self.assertEqual(self.db.list_rows('support_list')[0]['priority'], 101)
        self.assertEqual(self.db.list_rows('cases'), [])

    def test_duplicate_does_not_rotate_again(self):
        self.db.seed_support(); self.db.seed_case()
        before = self.db.list_rows('support_list')
        self.assertIsNone(self.db.create_case('001'))
        self.assertEqual(self.db.list_rows('support_list'), before)

    def test_case_url_is_persisted_and_preserved_on_reassign(self):
        self.db.seed_support(); self.db.seed_support('B')
        url = 'https://suse.lightning.force.com/lightning/r/Case/example/view'
        self.db.create_case('001', url=url)
        original = self.db.list_rows('cases')[0]
        self.db.reassign_case('001', 'B', False)
        self.assertEqual(self.db.list_rows('cases')[0]['url'], url)
        self.assertEqual(self.db.list_rows('cases')[0]['created_at'], original['created_at'])
        self.db.create_case('002', automatic=False, support_id='A')
        self.assertEqual(next(row for row in self.db.list_rows('cases') if row['id'] == '002')['url'], DEFAULT_CASE_URL)

    def test_legacy_case_url_migration_is_idempotent(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('DROP TABLE cases')
                cur.execute('CREATE TABLE cases (id VARCHAR(30) PRIMARY KEY, support_name VARCHAR(30), '
                            'support_id VARCHAR(20), created_at DATETIME)')
                cur.execute("INSERT INTO cases VALUES ('0001', 'Historical Support', 'DELETED', '2020-01-01 00:00:00')")
            conn.commit()
        self.db.initialize()
        historical = self.db.list_rows('cases')[0]
        self.assertEqual(historical['url'], DEFAULT_CASE_URL)
        self.assertEqual(historical['id'], '0001')
        self.assertEqual(historical['support_id'], 'DELETED')
        self.assertEqual(historical['created_at'], datetime(2020, 1, 1))
        self.db.seed_case('002', url='https://suse.lightning.force.com/custom')
        before = self.db.list_rows('cases')
        self.db.initialize()
        self.assertEqual(self.db.list_rows('cases'), before)

    def test_fresh_database_initializes_with_case_url_and_empty_token(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                for table in ('cases', 'support_list', 'app_settings'):
                    cur.execute('DROP TABLE ' + table)
            conn.commit()
        self.db.initialize()
        self.assertEqual(self.db.settings()['pagerduty_api_token'], '')
        self.db.seed_support()
        self.db.create_case('001')
        self.assertEqual(self.db.list_rows('cases')[0]['url'], DEFAULT_CASE_URL)

    def test_empty_case_url_rejected_before_queue_changes(self):
        self.db.seed_support(priority=101)
        before = self.db.list_rows('support_list')
        for url in ('', None, ' ', 'x' * 2049):
            with self.subTest(url=url), self.assertRaises(DomainError):
                self.db.create_case('001', url=url)
        self.assertEqual(self.db.list_rows('cases'), [])
        self.assertEqual(self.db.list_rows('support_list'), before)

    def test_same_owner_and_missing_records_do_not_mutate(self):
        self.db.seed_support(); self.db.seed_case()
        for support_id in ('A', 'MISSING'):
            with self.assertRaises(DomainError):
                self.db.reassign_case('001', support_id, True)
        with self.assertRaises(DomainError):
            self.db.reassign_case('MISSING', 'A', True)
        self.assertEqual(self.db.list_rows('support_list')[0]['priority'], 100)

    def test_priority_overflow_does_not_partially_reassign(self):
        self.db.seed_support('A', priority=2147483647)
        self.db.seed_support('B')
        self.db.seed_case()
        with self.assertRaises(DomainError):
            self.db.reassign_case('001', 'B', True)
        self.assertEqual(self.db.list_rows('cases')[0]['support_id'], 'A')
        self.assertEqual(self.db.list_rows('support_list')[0]['priority'], 2147483647)

    def test_mysql_connections_force_utc(self):
        from database import DatabaseManager
        with patch('database.pymysql.connect') as connect:
            DatabaseManager().get_connection()
        self.assertEqual(connect.call_args.kwargs['init_command'], "SET time_zone = '+00:00'")


class SlackTests(unittest.TestCase):
    @patch('app.requests.post', return_value=Mock(status_code=200))
    def test_slack_payload_has_exactly_message_url_and_uid(self, post):
        message = 'Case: 02083291\nPriority: Medium\nSubject: 组件流量分析\nAccount: Example\nProduct: RKE2'
        self.assertTrue(server.SlackNotifier('https://slack.example/workflow').send_message(
            message, DEFAULT_CASE_URL, 'U123456789'))
        post.assert_called_once_with('https://slack.example/workflow',
                                     json={'message': message, 'url': DEFAULT_CASE_URL, 'uid': 'U123456789'}, timeout=10)

    @patch('app.time.sleep')
    @patch('app.requests.post')
    def test_retry_reuses_full_payload(self, post, sleep):
        post.side_effect = [requests.Timeout(), Mock(status_code=500), Mock(status_code=200)]
        self.assertTrue(server.SlackNotifier('https://slack.example/workflow').send_message(
            'message', DEFAULT_CASE_URL, 'A', max_retries=3))
        self.assertEqual(post.call_count, 3)
        self.assertEqual(sleep.call_count, 2)
        for call in post.call_args_list:
            self.assertEqual(call.kwargs['json'], {'message': 'message', 'url': DEFAULT_CASE_URL, 'uid': 'A'})

    @patch('app.requests.post')
    def test_unconfigured_slack_does_not_send(self, post):
        self.assertFalse(server.SlackNotifier('').send_message('message', DEFAULT_CASE_URL, 'A'))
        post.assert_not_called()


if __name__ == '__main__':
    unittest.main()
