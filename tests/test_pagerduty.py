import unittest
from unittest.mock import Mock, patch

import requests

from database import DomainError
from pagerduty import PagerDutyClient, fallback_case_message, parse_case_email

INCIDENT_URL = 'https://api.pagerduty.com/incidents/Q235TB5V667RKR'
LOG_URL = 'https://api.pagerduty.com/log_entries/R46E5R3Z4SSS95MAUT9Q5RVKRD'
CASE_URL = 'https://suse.lightning.force.com/lightning/r/Case/500bG000013TW7F/view'
EMAIL_BODY = (
    f'<html>Case: <a href="{CASE_URL}">02083291</a><br/>\r\n'
    'Account: HEFEI LCFC INFORMATION TECHNOLOGY CO LTD<br>\r\n'
    'Subject: controller plane组件流量分析<br>\r\n'
    'Priority: Medium<br>\r\n'
    'Product Family: SUSE Rancher Prime: RKE2<br>\r\n'
    'Product: SUSE Rancher Prime: RKE2 Other x86_64<br>\r\n'
    'Geo Location: APAC<br>\r\nAccount Country: China<br></html>'
)
EXPECTED_MESSAGE = (
    'Case: 02083291\nPriority: Medium\nSubject: controller plane组件流量分析\n'
    'Account: HEFEI LCFC INFORMATION TECHNOLOGY CO LTD\n'
    'Product: SUSE Rancher Prime: RKE2 Other x86_64'
)


def responses(body=EMAIL_BODY):
    return [Mock(status_code=200, json=Mock(return_value={
                'incident': {'first_trigger_log_entry': {'self': LOG_URL}}})),
            Mock(status_code=200, json=Mock(return_value={'log_entry': {'channel': {'body': body}}}))]


class PagerDutyTests(unittest.TestCase):
    def test_fallback_preserves_case_id_and_hyphenated_account(self):
        self.assertEqual(fallback_case_message('Case 02083291 - Medium - Customer - APAC', '02083291'),
                         'Case: 02083291\nPriority: Medium\nAccount: Customer - APAC')
        with self.assertRaises(DomainError):
            fallback_case_message('Case 02083291', '02083291')

    def test_sample_body_produces_exact_five_lines_and_case_link(self):
        self.assertEqual(parse_case_email(EMAIL_BODY, '02083291'), (EXPECTED_MESSAGE, CASE_URL))

    def test_html_entities_nested_markup_and_colons_in_values(self):
        body = EMAIL_BODY.replace('组件流量分析', '<strong>组件</strong> &amp; 流量分析')
        body = body.replace('<br>', '<BR />').replace('CO LTD', 'CO&nbsp;LTD')
        message, url = parse_case_email(body, '02083291')
        self.assertIn('Subject: controller plane组件 & 流量分析', message)
        self.assertIn('Product: SUSE Rancher Prime: RKE2 Other x86_64', message)
        self.assertEqual(url, CASE_URL)

    def test_invalid_or_ambiguous_email_is_rejected(self):
        for body in (None, '', '<html>no case</html>', EMAIL_BODY.replace('Product:', 'Other:'),
                     EMAIL_BODY.replace('Medium', ''), EMAIL_BODY.replace('02083291', '999'),
                     EMAIL_BODY.replace(CASE_URL, 'javascript:alert(1)'),
                     EMAIL_BODY + '<br>Case: 02083291',
                     EMAIL_BODY + '<a href="https://example.com/other">02083291</a>'):
            with self.subTest(body=body), self.assertRaises(DomainError) as error:
                parse_case_email(body, '02083291')
            self.assertEqual(error.exception.status, 502)

    @patch('pagerduty.requests.get')
    def test_two_api_requests_include_headers_and_channels(self, get):
        get.side_effect = responses()
        self.assertEqual(PagerDutyClient('fake-api-token').case_details(INCIDENT_URL, '02083291'),
                         (EXPECTED_MESSAGE, CASE_URL))
        headers = {'Accept': 'application/vnd.pagerduty+json;version=2',
                   'Authorization': 'Token token=fake-api-token'}
        self.assertEqual(get.call_count, 2)
        self.assertEqual(get.call_args_list[0].args, (INCIDENT_URL,))
        self.assertEqual(get.call_args_list[1].args, (LOG_URL,))
        for call in get.call_args_list:
            self.assertEqual(call.kwargs['headers'], headers)
            self.assertFalse(call.kwargs['allow_redirects'])
            self.assertEqual(call.kwargs['timeout'], 10)
        self.assertEqual(get.call_args_list[1].kwargs['params'], {'include[]': 'channels'})
        prepared = requests.Request('GET', LOG_URL, params=get.call_args_list[1].kwargs['params']).prepare()
        self.assertIn('include%5B%5D=channels', prepared.url)

    @patch('pagerduty.requests.get')
    def test_api_failures_and_malformed_responses(self, get):
        failures = [requests.Timeout(), Mock(status_code=401), Mock(status_code=429),
                    Mock(status_code=302), Mock(status_code=500),
                    Mock(status_code=200, json=Mock(side_effect=ValueError('bad JSON'))),
                    Mock(status_code=200, json=Mock(return_value=[])),
                    Mock(status_code=200, json=Mock(return_value={'incident': None})),
                    Mock(status_code=200, json=Mock(return_value={'incident': {'first_trigger_log_entry': []}}))]
        for failure in failures:
            with self.subTest(failure=failure):
                get.side_effect = [failure]
                with self.assertRaises(DomainError) as error:
                    PagerDutyClient('fake-api-token').case_details(INCIDENT_URL, '02083291')
                self.assertEqual(error.exception.status, 502)
                self.assertNotIn('fake-api-token', str(error.exception))
        for data in ({}, {'log_entry': []}, {'log_entry': {'channel': None}},
                     {'log_entry': {'channel': {}}}):
            get.side_effect = [responses()[0], Mock(status_code=200, json=Mock(return_value=data))]
            with self.assertRaises(DomainError):
                PagerDutyClient('fake-api-token').case_details(INCIDENT_URL, '02083291')

    @patch('pagerduty.requests.get')
    def test_untrusted_urls_never_receive_token(self, get):
        for url in (None, 1, 'http://api.pagerduty.com/incidents/ABC',
                    'https://example.com/incidents/ABC', 'https://api.pagerduty.com.evil.com/incidents/ABC',
                    'https://api.pagerduty.com@evil.com/incidents/ABC',
                    INCIDENT_URL + '?redirect=evil', INCIDENT_URL + '#fragment',
                    'https://api.pagerduty.com/incidents/../users/ABC'):
            with self.subTest(url=url), self.assertRaises(DomainError):
                PagerDutyClient('fake-api-token').case_details(url, '02083291')
        get.assert_not_called()
        get.return_value = Mock(status_code=200, json=Mock(return_value={
            'incident': {'first_trigger_log_entry': {'self': 'https://evil.com/log_entries/ABC'}}}))
        with self.assertRaises(DomainError):
            PagerDutyClient('fake-api-token').case_details(INCIDENT_URL, '02083291')
        self.assertEqual(get.call_count, 1)

    @patch('pagerduty.requests.get')
    def test_missing_token_does_not_make_requests(self, get):
        with self.assertRaises(DomainError) as error:
            PagerDutyClient('').case_details(INCIDENT_URL, '02083291')
        self.assertEqual(error.exception.status, 503)
        get.assert_not_called()
