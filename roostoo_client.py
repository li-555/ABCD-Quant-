import os
import time
import hmac
import hashlib
import requests

BASE_URL = "https://mock-api.roostoo.com"
API_KEY = os.getenv("ROOSTOO_API_KEY", "J6FvRkX6T78xxykNDb0y2J8f4WeWFNEeY3rPghEZloCrzK5nUbk9oabolRZXqKBa")
API_SECRET = os.getenv("ROOSTOO_API_SECRET", "IcwshAhfqbdGjDE9Gc1VUUtHj4W43pf5aSuXZHKceowq59Qj3SRFpwMTOXFlpSlh")

def _get_timestamp():
    return str(int(time.time() * 1000))

def _get_signed_headers(payload: dict = None):
    if payload is None:
        payload = {}
    payload['timestamp'] = _get_timestamp()
    sorted_keys = sorted(payload.keys())
    total_params = "&".join(f"{k}={payload[k]}" for k in sorted_keys)
    
    signature = hmac.new(
        API_SECRET.encode('utf-8'),
        total_params.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    
    headers = {
        'RST-API-KEY': API_KEY,
        'MSG-SIGNATURE': signature
    }
    return headers, payload

def check_server_time():
    """Check API server time (public)."""
    res = requests.get(f"{BASE_URL}/v3/serverTime")
    return res.json()

def get_ticker(pair=None):
    """Fetch market prices (e.g. BTC/USD)."""
    params = {'timestamp': _get_timestamp()}
    if pair:
        params['pair'] = pair
    res = requests.get(f"{BASE_URL}/v3/ticker", params=params)
    return res.json()

def get_balance():
    """Fetch account balance (signed endpoint)."""
    headers, payload = _get_signed_headers({})
    res = requests.get(f"{BASE_URL}/v3/balance", headers=headers, params=payload)
    return res.json()

def place_order(pair: str, side: str, quantity: float, order_type="MARKET", price=None):
    """Place a mock trade."""
    payload = {
        'pair': pair,
        'side': side.upper(),
        'quantity': str(quantity),
        'type': order_type.upper()
    }
    if price is not None and order_type.upper() == "LIMIT":
        payload['price'] = str(price)

    headers, payload = _get_signed_headers(payload)
    headers['Content-Type'] = 'application/x-www-form-urlencoded'
    res = requests.post(f"{BASE_URL}/v3/place_order", headers=headers, data=payload)
    return res.json()

if __name__ == "__main__":
    print("Checking server time:", check_server_time())
    print("\nChecking account balance:")
    balance_res = get_balance()
    print(balance_res)
