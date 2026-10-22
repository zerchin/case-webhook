"""SQL-backed test fixture. SQLite exercises transactions, not MySQL locking semantics."""
import re
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pymysql

from database import DatabaseManager

sqlite3.register_converter('DATETIME', lambda value: datetime.fromisoformat(value.decode()))


class Cursor:
    def __init__(self, cursor):
        self.cursor = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.cursor.close()

    def execute(self, sql, params=()):
        sql = re.sub(r'\s+FOR UPDATE\b', '', sql).replace('%s', '?')
        try:
            self.cursor.execute(sql, params)
        except sqlite3.IntegrityError as error:
            raise pymysql.IntegrityError(1062, str(error)) from error

    def fetchone(self):
        row = self.cursor.fetchone()
        return dict(row) if row is not None else None

    def fetchall(self):
        return [dict(row) for row in self.cursor.fetchall()]

    @property
    def rowcount(self):
        return self.cursor.rowcount


class Connection:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, detect_types=sqlite3.PARSE_DECLTYPES)
        self.connection.row_factory = sqlite3.Row
        self.connection.create_function('NOW', 0, lambda: '2030-01-01 12:00:00')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.connection.rollback()
        self.connection.close()

    def cursor(self):
        return Cursor(self.connection.cursor())

    def commit(self):
        self.connection.commit()


class TestDatabase(DatabaseManager):
    def __init__(self):
        self.directory = tempfile.TemporaryDirectory(prefix='case-webhook-test-')
        self.path = str(Path(self.directory.name) / 'test.db')
        with sqlite3.connect(self.path) as connection:
            connection.executescript('''
                CREATE TABLE support_list (name TEXT NOT NULL, id TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'online', priority INTEGER DEFAULT 100,
                    updated_at DATETIME, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE cases (id TEXT PRIMARY KEY, support_name TEXT NOT NULL,
                    support_id TEXT NOT NULL, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE app_settings (id INTEGER PRIMARY KEY, webhook_path TEXT,
                    slack_webhook_url TEXT, pagerduty_webhook_secret TEXT);
                INSERT INTO app_settings VALUES (1, '/webhooks/pd/v3/test-token', 'https://slack.example/workflow', 'test-secret');
            ''')
        self.initialize()

    def initialize(self):
        self.migrate_webhook_secret()

    def get_connection(self):
        return Connection(self.path)

    def seed_support(self, support_id='A', name='Support A', priority=100,
                     status='online', updated_at='2020-01-01 00:00:00'):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO support_list VALUES (%s,%s,%s,%s,%s,%s)',
                            (name, support_id, status, priority, updated_at, '2020-01-01 00:00:00'))
            conn.commit()

    def seed_case(self, case_id='001', support_id='A', name='Support A'):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('INSERT INTO cases VALUES (%s,%s,%s,%s)',
                            (case_id, name, support_id, '2020-01-01 00:00:00'))
            conn.commit()

    def cleanup(self):
        self.directory.cleanup()
