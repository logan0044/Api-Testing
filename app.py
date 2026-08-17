import os
from dotenv import load_dotenv
load_dotenv()

from typing import Optional
from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Query, BackgroundTasks
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import api as shopify_api
import api2 as shopify_api2

import sys
import subprocess

_bot_process = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _bot_process
    bot_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.py")
    _bot_process = subprocess.Popen([sys.executable, bot_path])
    print(f"Started bot.py with PID {_bot_process.pid}")
    yield
    if _bot_process:
        _bot_process.terminate()
        _bot_process.wait()


app = FastAPI(title='Shopify Checker API', lifespan=lifespan)
@app.get('/')
def home():
    return {'message': 'Shopify checker API', 'status': True}

@app.get('/health')
def health():
    return {'status': 'ok'}

from fastapi.responses import PlainTextResponse

@app.get('/hits')
def view_hits(key: str = Query(None)):
    if key != 'shopihits99':
        raise HTTPException(status_code=403, detail="Forbidden")
    
    hits_file = '/sock/hits.txt' if os.path.isdir('/sock') else 'hits.txt'
    
    if not os.path.exists(hits_file):
        return PlainTextResponse("No hits recorded yet.")
    
    with open(hits_file, 'r') as f:
        content = f.read()
    
    return PlainTextResponse(content)

@app.get('/clear')
def clear_hits(key: str = Query(None)):
    if key != 'shopihits99':
        raise HTTPException(status_code=403, detail="Forbidden")
    
    hits_file = '/sock/hits.txt' if os.path.isdir('/sock') else 'hits.txt'
    
    # Clear the variant cache in api.py to force refetching cheapest products
    shopify_api._VARIANT_CACHE.clear()
    
    try:
        with open(hits_file, 'w') as f:
            f.write("")
        return {"message": "Hits and product cache cleared successfully", "status": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to clear: {str(e)}")

@app.get('/shopify')
async def shopify_checker(
    background_tasks: BackgroundTasks,
    site: str = Query(..., description="Shopify site domain or URL"),
    cc: str = Query(..., description="Card string in format CC|MM|YYYY|CVV"),
    proxy: Optional[str] = Query(None, description="Proxy string in format host:port or user:pass@host:port"),
    variant: Optional[str] = Query(None, description="Optional variant ID to use instead of auto-finding")
):
    cc_string = cc.strip()

    try:
        cc_parts = shopify_api.parse_cc_string(cc_string)
        card_number = cc_parts['cc']
        mes = cc_parts['mes']
        ano = cc_parts['ano']
        cvv = cc_parts['cvv']
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        success, message, gateway, price, currency = await shopify_api.process_card_async(
            card_number, mes, ano, cvv, site, variant, proxy
        )
    except Exception as exc:
        print(f"[app] shopify_checker unexpected error: {type(exc).__name__}: {exc}")
        return JSONResponse({
            'Gateway': 'UNKNOWN',
            'Price': 0.0,
            'Response': f'Proxy Error: API internal error',
            'Status': False,
            'cc': cc_string
        })

    clean_response = shopify_api.extract_clean_response(message)
    hit_status = shopify_api._classify_status(success, clean_response)
    if hit_status:
        background_tasks.add_task(shopify_api.save_hit_to_file, cc_string, hit_status, gateway, str(price))

    price_value = 0.0
    try:
        price_value = float(price)
    except (ValueError, TypeError):
        if isinstance(price, str) and price.replace('.', '', 1).isdigit():
            price_value = float(price)

    return JSONResponse({
        'Gateway': gateway,
        'Price': price_value,
        'Response': clean_response,
        'Status': success,
        'cc': cc_string
    })

@app.get('/shopify/cheapest')
async def shopify_cheapest(
    site: str = Query(..., description="Shopify site domain or URL"),
    proxy: Optional[str] = Query(None, description="Proxy string in format host:port or user:pass@host:port")
):
    variant_id, price, product_handle, status = shopify_api2.find_cheapest_product(site, proxy)
    if not variant_id:
        raise HTTPException(status_code=400, detail=status or 'Failed to find product')

    return {
        'site': site,
        'variant_id': variant_id,
        'price': price,
        'product_handle': product_handle,
        'status': status
    }


class ChargeRequest(BaseModel):
    site: str
    cc: Optional[str] = None
    card_number: Optional[str] = None
    month: Optional[str] = None
    year: Optional[str] = None
    cvv: Optional[str] = None
    proxy: Optional[str] = None
    variant: Optional[str] = None


@app.post('/shopify')
async def shopify_post(payload: ChargeRequest, background_tasks: BackgroundTasks):
    cc_string = None
    if payload.cc:
        cc_string = payload.cc.strip()
    else:
        if not (payload.card_number and payload.month and payload.year and payload.cvv):
            raise HTTPException(status_code=400, detail='Provide `cc` or all card fields (`card_number`, `month`, `year`, `cvv`)')
        cc_string = f"{payload.card_number}|{payload.month}|{payload.year}|{payload.cvv}"

    try:
        cc_parts = shopify_api.parse_cc_string(cc_string)
        card_number = cc_parts['cc']
        mes = cc_parts['mes']
        ano = cc_parts['ano']
        cvv = cc_parts['cvv']
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    try:
        success, message, gateway, price, currency = await shopify_api.process_card_async(
            card_number, mes, ano, cvv, payload.site, payload.variant, payload.proxy
        )
    except Exception as exc:
        print(f"[app] shopify_post unexpected error: {type(exc).__name__}: {exc}")
        return JSONResponse({
            'Gateway': 'UNKNOWN',
            'Price': 0.0,
            'Response': f'Proxy Error: API internal error',
            'Status': False,
            'cc': cc_string
        })

    clean_response = shopify_api.extract_clean_response(message)
    hit_status = shopify_api._classify_status(success, clean_response)
    if hit_status:
        background_tasks.add_task(shopify_api.save_hit_to_file, cc_string, hit_status, gateway, str(price))

    price_value = 0.0
    try:
        price_value = float(price)
    except (ValueError, TypeError):
        if isinstance(price, str) and price.replace('.', '', 1).isdigit():
            price_value = float(price)

    return JSONResponse({
        'Gateway': gateway,
        'Price': price_value,
        'Response': clean_response,
        'Status': success,
        'cc': cc_string
    })

if __name__ == '__main__':
    import uvicorn
    port = int(os.environ.get('PORT', 5000))
    uvicorn.run('app:app', host='0.0.0.0', port=port)
