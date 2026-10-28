"""Verificação server-side (SSV) das recompensas de anúncio do AdMob.

O Google chama nosso callback (GET) quando o usuário termina um anúncio
recompensado. A query vem assinada (ECDSA P-256 / SHA-256); só creditamos
mensagens se a assinatura bater com as chaves públicas do Google.
Doc: https://developers.google.com/admob/android/ssv
"""

import base64
import time
from urllib.parse import parse_qsl

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

VERIFIER_KEYS_URL = "https://www.gstatic.com/admob/reward/verifier-keys.json"
_KEYS_TTL_SECONDS = 24 * 3600

_keys_cache: dict[str, ec.EllipticCurvePublicKey] = {}
_keys_fetched_at = 0.0


async def _get_verifier_keys(force_refresh: bool = False) -> dict[str, ec.EllipticCurvePublicKey]:
    global _keys_cache, _keys_fetched_at
    if not force_refresh and _keys_cache and time.time() - _keys_fetched_at < _KEYS_TTL_SECONDS:
        return _keys_cache

    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(VERIFIER_KEYS_URL)
        response.raise_for_status()

    keys: dict[str, ec.EllipticCurvePublicKey] = {}
    for entry in response.json().get("keys", []):
        keys[str(entry["keyId"])] = serialization.load_pem_public_key(entry["pem"].encode())
    _keys_cache, _keys_fetched_at = keys, time.time()
    return keys


async def verify_ssv_query(raw_query: str) -> dict[str, str] | None:
    """Retorna os parâmetros do callback se a assinatura for válida; senão None.

    A mensagem assinada é a query crua até (sem incluir) "&signature=";
    `signature` e `key_id` são sempre os dois últimos parâmetros.
    """
    marker = raw_query.find("&signature=")
    if marker == -1:
        return None
    message = raw_query[:marker].encode()
    params = dict(parse_qsl(raw_query, keep_blank_values=True))
    signature = params.get("signature")
    key_id = params.get("key_id")
    if not signature or not key_id:
        return None

    try:
        signature_bytes = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
    except (ValueError, TypeError):
        return None

    keys = await _get_verifier_keys()
    if key_id not in keys:
        # Google faz rotação de chaves; tenta uma vez com a lista atualizada.
        keys = await _get_verifier_keys(force_refresh=True)
    public_key = keys.get(key_id)
    if public_key is None:
        return None

    try:
        public_key.verify(signature_bytes, message, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature:
        return None
    return params
