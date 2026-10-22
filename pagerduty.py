"""Retrieve PagerDuty email details before mutating the assignment queue."""
import re
from html.parser import HTMLParser
from urllib.parse import urlsplit

import requests

from database import DomainError

CASE_FIELDS = ('Case', 'Priority', 'Subject', 'Account', 'Product')


def fallback_case_message(title, case_id):
    # Split only the two field separators; account names may contain hyphens.
    match = re.search(r'Case\s+' + re.escape(case_id) + r'\s+-\s+(.+?)\s+-\s+(.+)', title)
    if not match:
        raise DomainError('Cannot extract priority and account from PagerDuty title.')
    return f'Case: {case_id}\nPriority: {match.group(1).strip()}\nAccount: {match.group(2).strip()}'


class CaseEmailParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.links = []
        self.anchor = None
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style'}:
            self.ignored += 1
        if self.ignored:
            return
        if tag in {'br', 'p', 'div', 'tr'}:
            self.parts.append('\n')
        if tag == 'a':
            self.anchor = [dict(attrs).get('href', ''), []]

    def handle_endtag(self, tag):
        if tag in {'script', 'style'}:
            self.ignored = max(0, self.ignored - 1)
        if self.ignored:
            return
        if tag in {'p', 'div', 'tr'}:
            self.parts.append('\n')
        if tag == 'a' and self.anchor is not None:
            self.links.append((self.anchor[0], ''.join(self.anchor[1]).strip()))
            self.anchor = None

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)
            if self.anchor is not None:
                self.anchor[1].append(data)


def parse_case_email(body, case_id):
    if not isinstance(body, str) or not body.strip():
        raise DomainError('PagerDuty log entry is missing the email body.', 502)
    parser = CaseEmailParser()
    parser.feed(body)
    parser.close()
    fields = {}
    for line in ''.join(parser.parts).splitlines():
        key, separator, value = line.strip().partition(':')
        if separator and key in CASE_FIELDS:
            if key in fields:
                raise DomainError('PagerDuty email contains duplicate case fields.', 502)
            fields[key] = value.strip()
    if any(not fields.get(key) for key in CASE_FIELDS):
        raise DomainError('PagerDuty email is missing required case fields.', 502)
    if fields['Case'] != case_id:
        raise DomainError('PagerDuty email case ID does not match the webhook title.', 502)
    links = {url for url, label in parser.links if label == case_id}
    if len(links) != 1:
        raise DomainError('PagerDuty email must contain one case URL.', 502)
    url = links.pop()
    try:
        parsed = urlsplit(url)
        parsed.port
        valid = (parsed.scheme in {'http', 'https'} and parsed.hostname and not parsed.username
                 and not parsed.password and len(url) <= 2048
                 and not any(character.isspace() or ord(character) < 32 for character in url))
    except ValueError:
        valid = False
    if not valid:
        raise DomainError('PagerDuty email contains an invalid case URL.', 502)
    return '\n'.join(f'{key}: {fields[key]}' for key in CASE_FIELDS), url


class PagerDutyClient:
    def __init__(self, token):
        self.token = token

    def get_resource(self, url, resource, include_channels=False):
        # Only send the API credential to the expected PagerDuty API resource.
        if not isinstance(url, str) or not re.fullmatch(
                r'https://api\.pagerduty\.com/' + resource + r'/[A-Za-z0-9]+', url):
            raise DomainError(f'Invalid PagerDuty {resource} self URL.', 502)
        if not self.token:
            raise DomainError('PagerDuty API token is not configured.', 503)
        try:
            response = requests.get(
                url, headers={'Accept': 'application/vnd.pagerduty+json;version=2',
                              'Authorization': f'Token token={self.token}'},
                params={'include[]': 'channels'} if include_channels else None,
                timeout=10, allow_redirects=False)
            if response.status_code != 200:
                raise DomainError('PagerDuty API request failed.', 502)
            data = response.json()
        except (requests.RequestException, ValueError):
            raise DomainError('PagerDuty API request failed or returned invalid JSON.', 502) from None
        if not isinstance(data, dict):
            raise DomainError('PagerDuty API returned an invalid response.', 502)
        return data

    def case_details(self, incident_url, case_id):
        incident = self.get_resource(incident_url, 'incidents').get('incident')
        trigger = incident.get('first_trigger_log_entry') if isinstance(incident, dict) else None
        if not isinstance(trigger, dict):
            raise DomainError('PagerDuty incident is missing its first trigger log entry.', 502)
        entry = self.get_resource(trigger.get('self'), 'log_entries', include_channels=True).get('log_entry')
        channel = entry.get('channel') if isinstance(entry, dict) else None
        if not isinstance(channel, dict):
            raise DomainError('PagerDuty log entry is missing its email channel.', 502)
        return parse_case_email(channel.get('body'), case_id)
