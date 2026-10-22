import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlsplit

import pymysql
import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from database import DatabaseManager, DomainError, serialize_row

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)
WEBHOOK_PATH_PREFIX = '/webhooks/pd/v3/'
WEBHOOK_SUFFIX_MAX_LENGTH = 255 - len(WEBHOOK_PATH_PREFIX)
WEBHOOK_SUFFIX_PATTERN = re.compile(r'[A-Za-z0-9_-]{1,' + str(WEBHOOK_SUFFIX_MAX_LENGTH) + r'}')
app = Flask(__name__)
app.jinja_env.globals['webhook_path_prefix'] = WEBHOOK_PATH_PREFIX
app.config.update(
    SECRET_KEY=secrets.token_bytes(32), SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE='Lax', PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    MAX_CONTENT_LENGTH=1024 * 1024,
    ADMIN_PASSWORD_HASH=generate_password_hash(os.environ['ADMIN_PASSWORD'], method='pbkdf2:sha256:600000') if os.getenv('ADMIN_PASSWORD') else None,
)
db = DatabaseManager()
schema_ready = False
schema_lock = threading.Lock()
SENSITIVE_HEADERS = {'authorization', 'cookie', 'proxy-authorization', 'set-cookie', 'x-api-key', 'x-pagerduty-signature'}


@app.before_request
def prepare_request():
    global schema_ready
    if request.endpoint == 'static' or request.path == '/health':
        return
    if not schema_ready:
        with schema_lock:
            if not schema_ready:
                db.initialize()
                schema_ready = True


@app.after_request
def security_headers(response):
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; frame-ancestors 'none'; form-action 'self'"
    if request.endpoint != 'static':
        response.headers['Cache-Control'] = 'no-store'
    return response


@app.errorhandler(DomainError)
def domain_error(error):
    return jsonify(error=str(error)), error.status


@app.errorhandler(pymysql.IntegrityError)
def duplicate_record(error):
    logger.warning('Database constraint rejected a record: %s', error.args[0])
    return jsonify(error='A record with this ID already exists or violates a database constraint.'), 409


@app.errorhandler(pymysql.MySQLError)
def database_error(error):
    logger.error('Database operation failed: %s', type(error).__name__)
    return jsonify(error='Database unavailable. Check the server database configuration.'), 503


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get('admin'):
            if request.path.startswith('/api/'):
                return jsonify(error='Please sign in.'), 401
            return redirect(url_for('login'))
        if request.method not in {'GET', 'HEAD'}:
            expected = session.get('csrf_token', '')
            supplied = request.headers.get('X-CSRF-Token', '')
            if not expected or not hmac.compare_digest(expected.encode(), supplied.encode()):
                return jsonify(error='Invalid session token. Reload the page.'), 403
        return view(*args, **kwargs)
    return wrapped


@app.route('/login', methods=['GET', 'POST'])
def login():
    session.setdefault('csrf_token', secrets.token_hex(32))
    error = None
    if request.method == 'POST':
        if not hmac.compare_digest(session['csrf_token'].encode(), request.form.get('csrf_token', '').encode()):
            abort(403)
        password_hash = app.config['ADMIN_PASSWORD_HASH']
        if not password_hash:
            error = 'Set ADMIN_PASSWORD on the server to enable sign in.'
        elif request.form.get('username') == 'admin' and check_password_hash(password_hash, request.form.get('password', '')):
            session.clear()
            session.update(admin=True, csrf_token=secrets.token_hex(32))
            session.permanent = True
            return redirect(url_for('support_page'))
        else:
            error = 'Invalid username or password.'
    return render_template('login.html', error=error), (400 if error else 200)


@app.post('/api/logout')
@admin_required
def logout():
    session.clear()
    return jsonify(status='success')


@app.get('/')
@admin_required
def index():
    return redirect(url_for('support_page'))


@app.get('/support')
@admin_required
def support_page():
    return render_template('dashboard.html', page='support')


@app.get('/cases')
@admin_required
def cases_page():
    return render_template('dashboard.html', page='cases')


@app.get('/settings')
@admin_required
def settings_page():
    return render_template('dashboard.html', page='settings')


def body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise DomainError('Expected a JSON object.')
    return data


def text_field(data, key, maximum):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise DomainError(f'{key.replace("_", " ").title()} is required (maximum {maximum} characters).')
    return value.strip()


def boolean_field(data, key, default=True):
    value = data.get(key, default)
    if not isinstance(value, bool):
        raise DomainError(f'{key} must be true or false.')
    return value


def priority_field(data):
    value = data.get('priority', 100)
    if isinstance(value, bool) or not isinstance(value, int) or not -2147483648 <= value <= 2147483647:
        raise DomainError('Priority must be a 32-bit integer.')
    return value


def status_field(data):
    value = data.get('status', 'online')
    if value not in ('online', 'offline'):
        raise DomainError('Status must be online or offline.')
    return value


def utc_datetime(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if parsed.tzinfo is None:
            raise ValueError()
        utc_value = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        if not 1000 <= utc_value.year <= 9999:
            raise ValueError()
        return utc_value
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise DomainError('Updated At must be an ISO 8601 date with a timezone.')


def validate_webhook_path(path):
    if not path:
        return
    if (not isinstance(path, str) or not path.startswith(WEBHOOK_PATH_PREFIX)
            or not WEBHOOK_SUFFIX_PATTERN.fullmatch(path[len(WEBHOOK_PATH_PREFIX):])):
        raise DomainError(f'Webhook path must use {WEBHOOK_PATH_PREFIX} followed by 1–{WEBHOOK_SUFFIX_MAX_LENGTH} letters, digits, hyphens or underscores.')


@app.route('/api/settings', methods=['GET', 'PUT'])
@admin_required
def settings_api():
    if request.method == 'PUT':
        data = body()
        values = {}
        if 'webhook_path' in data:
            path = data['webhook_path']
            if not isinstance(path, str):
                raise DomainError('Webhook path must be a string.')
            path = path.strip()
            validate_webhook_path(path)
            values['webhook_path'] = path
        for key in ('slack_webhook_url', 'pagerduty_webhook_secret'):
            if key in data:
                if not isinstance(data[key], str) or len(data[key]) > 8192:
                    raise DomainError('Invalid setting value.')
                values[key] = data[key].strip()
        url = values.get('slack_webhook_url', '')
        if url:
            try:
                parsed = urlsplit(url)
                parsed.port
            except ValueError:
                raise DomainError('Invalid Slack webhook URL.')
            if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
                raise DomainError('Slack webhook URL must be an HTTP or HTTPS URL without credentials.')
        db.save_settings(values)
    settings = db.settings()
    return jsonify(webhook_path=settings['webhook_path'],
                   slack_webhook_url_configured=bool(settings['slack_webhook_url']),
                   pagerduty_webhook_secret_configured=bool(settings['pagerduty_webhook_secret']))


@app.route('/api/support', methods=['GET', 'POST'])
@admin_required
def support_api():
    if request.method == 'GET':
        return jsonify([serialize_row(row) for row in db.list_rows('support_list')])
    data = body()
    db.add_support(dict(id=text_field(data, 'id', 20), name=text_field(data, 'name', 30),
                        status=status_field(data), priority=priority_field(data)))
    return jsonify(status='success'), 201


@app.patch('/api/support/<path:support_id>')
@admin_required
def edit_support_api(support_id):
    data = body()
    values = {}
    if 'status' in data:
        values['status'] = status_field(data)
    if 'priority' in data:
        values['priority'] = priority_field(data)
    if 'updated_at' in data:
        values['updated_at'] = utc_datetime(data['updated_at'])
    if not values:
        raise DomainError('No support changes provided.')
    db.edit_support(support_id, values)
    return jsonify(status='success')


@app.delete('/api/support/<path:support_id>')
@admin_required
def delete_support_api(support_id):
    db.delete_support(support_id)
    return jsonify(status='success')


class SlackNotifier:
    def __init__(self, webhook_url):
        self.webhook_url = webhook_url

    def send_message(self, message, max_retries=15):
        if not self.webhook_url:
            return False
        for attempt in range(max_retries):
            try:
                response = requests.post(self.webhook_url, json={'message': message}, timeout=10)
                if response.status_code == 200:
                    return True
            except requests.RequestException:
                logger.warning('Slack request failed (attempt %s).', attempt + 1)
            if attempt < max_retries - 1:
                time.sleep(min(2 ** attempt, 30))
        return False


def notification_result(case_id, message=None, settings=None, status_code=200):
    if message is not None:
        settings = settings if settings is not None else db.settings()
        if not SlackNotifier(settings['slack_webhook_url']).send_message(message):
            return jsonify(status='partial_success', case_id=case_id,
                           message='Case saved, but the Slack notification failed. Do not retry the case operation; check Settings and Slack.'), 207
    return jsonify(status='success', case_id=case_id), status_code


def automatic_case(case_id, title, settings=None, status_code=200, duplicate_status=200):
    support = db.create_case(case_id)
    if support is None:
        return jsonify(status='skipped', reason='Case already exists.'), duplicate_status
    return notification_result(case_id, f"{title}\nOwner: {support['name']} <@{support['id']}>", settings, status_code)


@app.route('/api/cases', methods=['GET', 'POST'])
@admin_required
def cases_api():
    if request.method == 'GET':
        return jsonify([serialize_row(row) for row in db.list_rows('cases')])
    data = body()
    case_id = data.get('id')
    if not isinstance(case_id, str) or not re.fullmatch(r'[0-9]{1,30}', case_id):
        raise DomainError('Case ID must contain 1–30 digits (0–9) only.')
    if boolean_field(data, 'notify'):
        priority = data.get('priority', 'Medium')
        if priority not in ('Critical', 'High', 'Medium', 'Low'):
            raise DomainError('Select a valid case priority.')
        account = text_field(data, 'account_name', 255)
        return automatic_case(case_id, f'Case {case_id} - {priority} - {account}', status_code=201, duplicate_status=409)
    support_id = text_field(data, 'support_id', 20)
    if db.create_case(case_id, automatic=False, support_id=support_id) is None:
        raise DomainError('Case already exists.', 409)
    return notification_result(case_id, status_code=201)


@app.post('/api/cases/<path:case_id>/reassign')
@admin_required
def reassign_case_api(case_id):
    data = body()
    support_id = text_field(data, 'support_id', 20)
    notify = boolean_field(data, 'notify')
    adjust = boolean_field(data, 'adjust_priority')
    case, support = db.reassign_case(case_id, support_id, adjust)
    message = (f"Supplementary Case {case_id}\nOwner Changed From {case['support_name']} to {support['name']} <@{support['id']}>\n" if notify else None)
    return notification_result(case_id, message)


@app.delete('/api/cases/<path:case_id>')
@admin_required
def delete_case_api(case_id):
    db.delete_case(case_id)
    return jsonify(status='success')


@app.post('/api/cases/bulk-delete')
@admin_required
def delete_cases_api():
    case_ids = body().get('ids')
    if not isinstance(case_ids, list) or not case_ids:
        raise DomainError('Select at least one case.')
    if any(not isinstance(case_id, str) or not case_id or len(case_id) > 30 for case_id in case_ids):
        raise DomainError('Each case ID must be a non-empty string of at most 30 characters.')
    deleted_count = db.delete_cases(case_ids)
    return jsonify(status='success', deleted_count=deleted_count,
                   message=f'{deleted_count} selected case(s) deleted.')


def verify_pagerduty_signature(payload, signature_header, secret):
    if not payload or not signature_header or not secret:
        return False
    expected = 'v1=' + hmac.new(secret.encode('utf-8'), payload, hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected.encode(), signature.strip().encode())
               for signature in signature_header.split(',') if signature.strip().startswith('v1='))


@app.post(WEBHOOK_PATH_PREFIX + '<suffix>')
def webhook_receiver(suffix):
    if not WEBHOOK_SUFFIX_PATTERN.fullmatch(suffix):
        abort(404)
    settings = db.settings()
    if not settings['webhook_path'] or request.path != settings['webhook_path']:
        abort(404)
    raw = request.get_data(cache=True)
    logger.info('Webhook request: %s', json.dumps(dict(remote_ip=request.remote_addr, method=request.method,
                path=request.path, headers={key: '[REDACTED]' if key.lower() in SENSITIVE_HEADERS else value
                                           for key, value in request.headers.items()},
                body=raw.decode('utf-8', errors='replace')), ensure_ascii=False, separators=(',', ':')))
    data = request.get_json(silent=True)
    event = data.get('event') if isinstance(data, dict) else None
    details = event.get('data') if isinstance(event, dict) else None
    if not isinstance(details, dict):
        raise DomainError('Invalid PagerDuty JSON.')
    # The former external supplement API is closed, even for valid signatures.
    if 'supplement' in details:
        raise DomainError('External supplement requests are disabled. Use the authenticated UI.', 403)
    secret = settings['pagerduty_webhook_secret']
    signature = request.headers.get('X-PagerDuty-Signature', '')
    if not secret:
        raise DomainError('Webhook signature verification is not configured.', 503)
    if not signature:
        raise DomainError('Missing webhook signature.', 400)
    if not verify_pagerduty_signature(raw, signature, secret):
        raise DomainError('Invalid webhook signature.', 403)
    title = details.get('title')
    match = re.search(r'Case\s+(\d+)', title) if isinstance(title, str) else None
    if not match or len(match.group(1)) > 30:
        raise DomainError('Cannot extract case ID from title.')
    return automatic_case(match.group(1), title, settings)


@app.get('/health')
def health_check():
    return jsonify(status='healthy', version='0.6.0')


if __name__ == '__main__':
    if not app.config['ADMIN_PASSWORD_HASH']:
        raise RuntimeError('ADMIN_PASSWORD must be set before starting the server.')
    app.run(host='0.0.0.0', port=int(os.getenv('PORT', '5000')), debug=False)
