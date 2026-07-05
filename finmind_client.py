"""Thin wrapper around the FinMind REST API (https://api.finmindtrade.com).

Used by the 達人選股 (expert stock-picking) feature to fetch institutional
trading, shareholding concentration, balance sheet / cash flow / financial
statement items, dividend policy, and PER/PBR data that isn't available from
the TWSE/TPEX/MOPS crawlers in crawler.py.

Unlike crawler.py's _get()/_post(), this is an official documented API (no
anti-bot evasion needed), but still retries on rate-limit (402) and 5xx.
"""
import logging
import os
import time

import requests

logger = logging.getLogger(__name__)

_BASE_URL = 'https://api.finmindtrade.com/api/v4/data'
_session = requests.Session()


def _token():
    token = os.environ.get('FINMIND_TOKEN')
    if not token:
        raise RuntimeError(
            'FINMIND_TOKEN environment variable not set. '
            'Get a token from https://finmindtrade.com/ and set it in the environment '
            '(do not hardcode it in source).'
        )
    return token


def fetch(dataset, start_date=None, end_date=None, data_id=None, *, retries=4, timeout=60):
    """Fetch one dataset from FinMind. Omitting data_id returns ALL stocks for
    start_date in a single call (bulk mode) — confirmed against the live API
    for every dataset this project uses. end_date is NOT a true range filter:
    it's only honored when it equals start_date (or is simply ignored) —
    passing a wider range silently returns just start_date's rows instead of
    the whole span. Every caller in crawler.py therefore always sets
    start_date == end_date and loops one call per day/period itself. Returns
    the list under the 'data' key."""
    params = {'dataset': dataset, 'token': _token()}
    if data_id:
        params['data_id'] = data_id
    if start_date:
        params['start_date'] = start_date
    if end_date:
        params['end_date'] = end_date

    # Force gzip/deflate only — if brotli ever ends up importable in the
    # deployment container, requests/urllib3 would otherwise auto-advertise
    # 'br' and a brotli-encoded response would fail to decode (the same
    # class of bug crawler.py's _get()/_post() already guard against for
    # TWSE/TPEX/MOPS).
    headers = {'Accept-Encoding': 'gzip, deflate'}

    for attempt in range(retries):
        try:
            resp = _session.get(_BASE_URL, params=params, headers=headers, timeout=timeout)
        except requests.exceptions.RequestException as e:
            if attempt == retries - 1:
                raise
            wait = (attempt + 1) * 10
            logger.warning('FinMind connection error (%s): %s — retry in %ds', dataset, e, wait)
            time.sleep(wait)
            continue

        if resp.status_code == 402:
            # Rate limit reached ("Requests reach the upper limit")
            if attempt == retries - 1:
                raise RuntimeError(f'FinMind rate limit exceeded fetching {dataset}: {resp.text}')
            wait = 60 * (attempt + 1)
            logger.warning('FinMind rate limit hit (%s) — waiting %ds', dataset, wait)
            time.sleep(wait)
            continue

        if resp.status_code >= 500:
            if attempt == retries - 1:
                resp.raise_for_status()
            wait = (attempt + 1) * 10
            logger.warning('FinMind HTTP %d (%s) — retry in %ds', resp.status_code, dataset, wait)
            time.sleep(wait)
            continue

        resp.raise_for_status()
        payload = resp.json()
        if payload.get('status') != 200:
            raise RuntimeError(f'FinMind error for {dataset}: {payload.get("msg")}')
        return payload.get('data', [])

    return []
