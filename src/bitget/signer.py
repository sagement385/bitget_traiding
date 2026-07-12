import base64, hashlib, hmac, time

def timestamp_ms() -> str: return str(int(time.time()*1000))

def sign(secret_key: str, timestamp: str, method: str, request_path: str, body: str = '') -> str:
    msg=f'{timestamp}{method.upper()}{request_path}{body}'
    digest=hmac.new(secret_key.encode(), msg.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()
