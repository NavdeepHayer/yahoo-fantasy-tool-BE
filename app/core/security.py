import secrets

def gen_state() -> str:
    return secrets.token_urlsafe(32)
