"""MySQL storage and atomic assignment operations. All DATETIME values are UTC."""
import base64
import binascii
import os
from datetime import datetime, timezone

import pymysql


class DomainError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


SECRET_STORAGE_PREFIX = 'base64:v1:'


def encode_webhook_secret(secret):
    """Versioned Base64 storage, not encryption. Empty values remain empty."""
    if not secret:
        return ''
    return SECRET_STORAGE_PREFIX + base64.b64encode(secret.encode('utf-8')).decode('ascii')


def decode_webhook_secret(stored):
    if not stored or not stored.startswith(SECRET_STORAGE_PREFIX):
        # Older installations stored plaintext; initialization migrates it.
        return stored
    try:
        return base64.b64decode(stored[len(SECRET_STORAGE_PREFIX):], validate=True).decode('utf-8')
    except (binascii.Error, ValueError, UnicodeError):
        raise DomainError('Stored PagerDuty signing secret is invalid. Save it again in Settings.', 503) from None


class DatabaseManager:
    def get_connection(self):
        return pymysql.connect(
            host=os.getenv('MYSQL_HOST', '127.0.0.1'),
            port=int(os.getenv('MYSQL_PORT', '3306')),
            user=os.getenv('MYSQL_USER', 'root'),
            password=os.getenv('MYSQL_PASSWORD', ''),
            database=os.getenv('MYSQL_DB', 'case_system'),
            charset='utf8mb4', cursorclass=pymysql.cursors.DictCursor,
            init_command="SET time_zone = '+00:00'",
        )

    def initialize(self):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""CREATE TABLE IF NOT EXISTS support_list (
                    name VARCHAR(30) NOT NULL, id VARCHAR(20) NOT NULL PRIMARY KEY,
                    status ENUM('online','offline') NOT NULL DEFAULT 'online',
                    priority INT DEFAULT 100, updated_at DATETIME DEFAULT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
                cur.execute("""CREATE TABLE IF NOT EXISTS cases (
                    id VARCHAR(30) NOT NULL PRIMARY KEY, support_name VARCHAR(30) NOT NULL,
                    support_id VARCHAR(20) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""")
                cur.execute("""CREATE TABLE IF NOT EXISTS app_settings (
                    id INT NOT NULL PRIMARY KEY,
                    webhook_path VARCHAR(255) NOT NULL DEFAULT '',
                    slack_webhook_url TEXT NOT NULL,
                    pagerduty_webhook_secret TEXT NOT NULL
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
                cur.execute("""INSERT IGNORE INTO app_settings
                    (id, webhook_path, slack_webhook_url, pagerduty_webhook_secret)
                    VALUES (1, '', '', '')""")
            conn.commit()
        self.migrate_webhook_secret()

    def migrate_webhook_secret(self):
        """Upgrade only the existing secret value; preserve other settings."""
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT pagerduty_webhook_secret FROM app_settings WHERE id = 1 FOR UPDATE')
                row = cur.fetchone()
                stored = row['pagerduty_webhook_secret'] if row else ''
                if stored and not stored.startswith(SECRET_STORAGE_PREFIX):
                    cur.execute('UPDATE app_settings SET pagerduty_webhook_secret = %s WHERE id = 1',
                                (encode_webhook_secret(stored),))
            conn.commit()

    def settings(self):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT webhook_path, slack_webhook_url, pagerduty_webhook_secret FROM app_settings WHERE id = 1')
                row = cur.fetchone()
                row['pagerduty_webhook_secret'] = decode_webhook_secret(row['pagerduty_webhook_secret'])
                return row

    def save_settings(self, values):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                for key, value in values.items():
                    if key not in {'webhook_path', 'slack_webhook_url', 'pagerduty_webhook_secret'}:
                        raise ValueError('Unknown setting')
                    if key == 'pagerduty_webhook_secret':
                        value = encode_webhook_secret(value)
                    cur.execute('UPDATE app_settings SET ' + key + ' = %s WHERE id = 1', (value,))
            conn.commit()

    def list_rows(self, table):
        if table not in {'support_list', 'cases'}:
            raise ValueError('Unknown table')
        order = 'updated_at ASC, id ASC' if table == 'support_list' else 'created_at DESC, id ASC'
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute('SELECT * FROM ' + table + ' ORDER BY ' + order)
                return cur.fetchall()

    @staticmethod
    def lock_queue(cur):
        # Serialize case mutations across UI requests and PagerDuty workers.
        cur.execute('SELECT id FROM app_settings WHERE id = 1 FOR UPDATE')
        cur.fetchone()

    def add_support(self, values):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                self.lock_queue(cur)
                cur.execute("""INSERT INTO support_list (id, name, status, priority, updated_at, created_at)
                    VALUES (%s, %s, %s, %s, NOW(), NOW())""",
                    (values['id'], values['name'], values['status'], values['priority']))
            conn.commit()

    def edit_support(self, support_id, values):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                self.lock_queue(cur)
                cur.execute('SELECT id FROM support_list WHERE id = %s FOR UPDATE', (support_id,))
                if not cur.fetchone():
                    raise DomainError('Support not found.', 404)
                for key, value in values.items():
                    if key not in {'status', 'priority', 'updated_at'}:
                        raise ValueError('Unknown support field')
                    # Preserve updated_at on legacy tables with ON UPDATE.
                    suffix = '' if key == 'updated_at' else ', updated_at = updated_at'
                    cur.execute('UPDATE support_list SET ' + key + ' = %s' + suffix + ' WHERE id = %s', (value, support_id))
            conn.commit()

    def delete_support(self, support_id):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                self.lock_queue(cur)
                cur.execute('DELETE FROM support_list WHERE id = %s', (support_id,))
                if not cur.rowcount:
                    raise DomainError('Support not found.', 404)
            conn.commit()

    @staticmethod
    def select_support(cur, support_id):
        cur.execute('SELECT * FROM support_list WHERE id = %s FOR UPDATE', (support_id,))
        support = cur.fetchone()
        if not support:
            raise DomainError('Support not found.', 404)
        return support

    def create_case(self, case_id, automatic=True, support_id=None):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                self.lock_queue(cur)
                cur.execute('SELECT id FROM cases WHERE id = %s FOR UPDATE', (case_id,))
                if cur.fetchone():
                    return None
                if automatic:
                    cur.execute("""SELECT * FROM support_list WHERE status = 'online'
                        ORDER BY updated_at ASC, id ASC FOR UPDATE""")
                    queue = list(cur.fetchall())
                    if not queue:
                        raise DomainError('No online support found.', 409)
                    support = next((row for row in queue if (row['priority'] if row['priority'] is not None else 100) > 100), None)
                    if support:
                        cur.execute('UPDATE support_list SET priority = priority - 1, updated_at = updated_at WHERE id = %s', (support['id'],))
                    else:
                        # Fast-forward complete skipped rounds rather than looping
                        # billions of times for a very low manually set priority.
                        priorities = [row['priority'] if row['priority'] is not None else 100 for row in queue]
                        if max(priorities) < 100:
                            rounds = 100 - max(priorities)
                            for row, priority in zip(queue, priorities):
                                cur.execute('UPDATE support_list SET priority = %s, updated_at = NOW() WHERE id = %s', (priority + rounds, row['id']))
                                row['priority'] = priority + rounds
                        # Deterministic even when NOW() has second precision.
                        while True:
                            support = queue.pop(0)
                            priority = support['priority'] if support['priority'] is not None else 100
                            if priority >= 100:
                                break
                            cur.execute('UPDATE support_list SET priority = %s, updated_at = NOW() WHERE id = %s', (priority + 1, support['id']))
                            support['priority'] = priority + 1
                            queue.append(support)
                        cur.execute('UPDATE support_list SET updated_at = NOW() WHERE id = %s', (support['id'],))
                else:
                    support = self.select_support(cur, support_id)
                cur.execute("""INSERT INTO cases (id, support_name, support_id, created_at)
                    VALUES (%s, %s, %s, NOW())""", (case_id, support['name'], support['id']))
            conn.commit()
        return support

    def reassign_case(self, case_id, support_id, adjust_priority):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                self.lock_queue(cur)
                cur.execute('SELECT * FROM cases WHERE id = %s FOR UPDATE', (case_id,))
                case = cur.fetchone()
                if not case:
                    raise DomainError('Case not found.', 404)
                support = self.select_support(cur, support_id)
                if case['support_id'] == support['id']:
                    raise DomainError('Select a different support.')
                if adjust_priority:
                    cur.execute('SELECT * FROM support_list WHERE id = %s FOR UPDATE', (case['support_id'],))
                    old = cur.fetchone()
                    old_priority = (old['priority'] if old['priority'] is not None else 100) if old else None
                    new_priority = support['priority'] if support['priority'] is not None else 100
                    if (old_priority is not None and old_priority >= 2147483647) or new_priority <= -2147483648:
                        raise DomainError('Priority adjustment would exceed the supported integer range.')
                    if old:
                        cur.execute('UPDATE support_list SET priority = COALESCE(priority, 100) + 1, updated_at = updated_at WHERE id = %s', (old['id'],))
                    cur.execute('UPDATE support_list SET priority = COALESCE(priority, 100) - 1, updated_at = updated_at WHERE id = %s', (support['id'],))
                cur.execute('UPDATE cases SET support_name = %s, support_id = %s WHERE id = %s', (support['name'], support['id'], case_id))
            conn.commit()
        return case, support

    def delete_case(self, case_id):
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                self.lock_queue(cur)
                cur.execute('DELETE FROM cases WHERE id = %s', (case_id,))
                if not cur.rowcount:
                    raise DomainError('Case not found.', 404)
            conn.commit()

    def delete_cases(self, case_ids):
        case_ids = list(dict.fromkeys(case_ids))
        if not case_ids:
            raise DomainError('Select at least one case.')
        placeholders = ', '.join(['%s'] * len(case_ids))
        with self.get_connection() as conn:
            with conn.cursor() as cur:
                self.lock_queue(cur)
                cur.execute('SELECT id FROM cases WHERE id IN (' + placeholders + ') FOR UPDATE', tuple(case_ids))
                if len(cur.fetchall()) != len(case_ids):
                    raise DomainError('Some selected cases no longer exist. Refresh the list and select again.', 409)
                cur.execute('DELETE FROM cases WHERE id IN (' + placeholders + ')', tuple(case_ids))
                deleted_count = cur.rowcount
            conn.commit()
        return deleted_count


def serialize_row(row):
    return {key: value.replace(tzinfo=timezone.utc).isoformat().replace('+00:00', 'Z')
            if isinstance(value, datetime) else value for key, value in row.items()}
