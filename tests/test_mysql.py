"""Opt-in tests for real MySQL syntax, UTC, transactions and concurrent deduplication.

Use an EMPTY, DISPOSABLE database whose name begins with test_:
  MYSQL_TEST_DB=test_case_webhook MYSQL_TEST_ALLOW_RESET=1 \
  MYSQL_TEST_HOST=127.0.0.1 MYSQL_TEST_USER=root MYSQL_TEST_PASSWORD=... \
  .venv/bin/python -m unittest discover -s tests -p test_mysql.py -v

These tests DELETE all rows from support_list, cases and app_settings in that DB.
They are skipped by default and never use the application's MYSQL_* configuration.
"""
import os
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import pymysql

from database import DEFAULT_CASE_URL, DatabaseManager, SECRET_STORAGE_PREFIX

ENABLED = bool(os.getenv('MYSQL_TEST_DB')) and os.getenv('MYSQL_TEST_ALLOW_RESET') == '1'


class MySQLTestDatabase(DatabaseManager):
    def get_connection(self):
        name = os.environ['MYSQL_TEST_DB']
        if not name.startswith('test_'):
            raise ValueError('MYSQL_TEST_DB must begin with test_ and be disposable.')
        return pymysql.connect(
            host=os.getenv('MYSQL_TEST_HOST', '127.0.0.1'),
            port=int(os.getenv('MYSQL_TEST_PORT', '3306')),
            user=os.getenv('MYSQL_TEST_USER', 'root'),
            password=os.getenv('MYSQL_TEST_PASSWORD', ''), database=name,
            charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
            init_command="SET time_zone = '+00:00'",
        )


@unittest.skipUnless(ENABLED, 'Requires a disposable MYSQL_TEST_DB and MYSQL_TEST_ALLOW_RESET=1.')
class MySQLIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.db = MySQLTestDatabase()
        self.db.initialize()
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                for table in ('cases', 'support_list', 'app_settings'):
                    cur.execute('DELETE FROM ' + table)
                cur.execute("INSERT INTO app_settings (id, webhook_path, slack_webhook_url, pagerduty_webhook_secret) VALUES (1, '', '', '')")
            conn.commit()
        self.db.add_support(dict(id='A', name='Alice', status='online', priority=100))
        self.db.add_support(dict(id='B', name='Bob', status='offline', priority=100))

    def test_schema_settings_utc_and_support_operations(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT @@session.time_zone AS zone, NOW() AS now, UTC_TIMESTAMP() AS utc')
                result = cur.fetchone()
        self.assertEqual(result['zone'], '+00:00')
        self.assertEqual(result['now'], result['utc'])
        self.db.save_settings({'webhook_path': '/webhooks/pd/v3/test-token', 'pagerduty_webhook_secret': 'fake-secret'})
        self.assertEqual(self.db.settings()['webhook_path'], '/webhooks/pd/v3/test-token')
        self.db.edit_support('A', {'updated_at': datetime(2020, 1, 1), 'priority': 99})
        self.assertEqual(self.db.list_rows('support_list')[0]['updated_at'], datetime(2020, 1, 1))

    def test_legacy_settings_adds_token_and_preserves_other_settings(self):
        self.db.save_settings({'slack_webhook_url': 'https://slack.example/workflow',
                               'pagerduty_webhook_secret': 'fake-secret'})
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('ALTER TABLE app_settings DROP COLUMN pagerduty_api_token')
            conn.commit()
        self.db.initialize()
        self.assertEqual(self.db.settings()['pagerduty_api_token'], '')
        self.assertEqual(self.db.settings()['slack_webhook_url'], 'https://slack.example/workflow')
        self.assertEqual(self.db.settings()['pagerduty_webhook_secret'], 'fake-secret')
        self.db.save_settings({'pagerduty_api_token': 'fake-api-token'})
        self.db.initialize()
        self.assertEqual(self.db.settings()['pagerduty_api_token'], 'fake-api-token')
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT pagerduty_api_token FROM app_settings WHERE id = 1')
                self.assertTrue(cur.fetchone()['pagerduty_api_token'].startswith(SECRET_STORAGE_PREFIX))

    def test_secret_encoding_and_legacy_migration(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('UPDATE app_settings SET pagerduty_webhook_secret = %s WHERE id = 1', ('legacy-signing-secret',))
            conn.commit()
        self.db.initialize()
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT pagerduty_webhook_secret FROM app_settings WHERE id = 1')
                stored = cur.fetchone()['pagerduty_webhook_secret']
        self.assertTrue(stored.startswith(SECRET_STORAGE_PREFIX))
        self.assertNotIn('legacy-signing-secret', stored)
        self.assertEqual(self.db.settings()['pagerduty_webhook_secret'], 'legacy-signing-secret')
        self.db.save_settings({'pagerduty_webhook_secret': 'new-signing-secret'})
        self.db.initialize()
        self.assertEqual(self.db.settings()['pagerduty_webhook_secret'], 'new-signing-secret')

    def test_concurrent_duplicate_rotates_only_once(self):
        self.db.edit_support('A', {'priority': 103})
        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.db.create_case('same-case'), range(6)))
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual(len(self.db.list_rows('cases')), 1)
        self.assertEqual(next(row for row in self.db.list_rows('support_list') if row['id'] == 'A')['priority'], 102)

    def test_legacy_case_url_migration_and_default(self):
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('ALTER TABLE cases DROP COLUMN url')
                cur.execute("INSERT INTO cases (id, support_name, support_id) VALUES ('0001', 'Alice', 'A')")
            conn.commit()
        self.db.initialize()
        original = self.db.list_rows('cases')[0]
        self.assertEqual(original['url'], DEFAULT_CASE_URL)
        custom_url = DEFAULT_CASE_URL + '/lightning/r/Case/example/view'
        self.db.create_case('0002', url=custom_url)
        self.db.reassign_case('0002', 'B', False)
        before = self.db.list_rows('cases')
        self.db.initialize()
        self.assertEqual(self.db.list_rows('cases'), before)
        self.assertEqual(next(row for row in before if row['id'] == '0002')['url'], custom_url)
        with self.db.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO cases (id, support_name, support_id) VALUES ('0003', 'Bob', 'B')")
            conn.commit()
        self.assertEqual(next(row for row in self.db.list_rows('cases') if row['id'] == '0003')['url'], DEFAULT_CASE_URL)

    def test_manual_reassign_delete_preserve_times(self):
        before = {row['id']: row['updated_at'] for row in self.db.list_rows('support_list')}
        self.db.create_case('manual', automatic=False, support_id='B')
        self.db.reassign_case('manual', 'A', True)
        people = self.db.list_rows('support_list')
        self.assertEqual({row['id']: row['updated_at'] for row in people}, before)
        self.assertEqual({row['id']: row['priority'] for row in people}, {'A': 99, 'B': 101})
        self.assertEqual(self.db.list_rows('cases')[0]['support_id'], 'A')
        self.db.delete_case('manual')
        self.assertEqual(self.db.list_rows('cases'), [])
        self.assertEqual(self.db.list_rows('support_list'), people)

    def test_delete_support_reassign_history_and_bulk_delete(self):
        self.db.create_case('001', automatic=False, support_id='A')
        self.db.create_case('002', automatic=False, support_id='A')
        self.db.create_case('003', automatic=False, support_id='B')
        history = self.db.list_rows('cases')
        self.db.delete_support('A')
        self.assertEqual(self.db.list_rows('cases'), history)
        before = self.db.list_rows('support_list')[0]
        self.db.reassign_case('001', 'B', True)
        after = self.db.list_rows('support_list')[0]
        self.assertEqual(after['priority'], 99)
        self.assertEqual(after['updated_at'], before['updated_at'])
        self.assertEqual(self.db.delete_cases(['001', '002']), 2)
        self.assertEqual([row['id'] for row in self.db.list_rows('cases')], ['003'])
        self.assertEqual(self.db.list_rows('support_list'), [after])
