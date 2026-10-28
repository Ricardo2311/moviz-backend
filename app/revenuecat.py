"""Integração com RevenueCat — fonte da verdade das assinaturas Premium.

O app compra pela loja (Google Play Billing / StoreKit) através do SDK do
RevenueCat, usando o id do usuário Moviz como `appUserID`. O backend nunca
confia no cliente: sempre consulta a API do RevenueCat para decidir `is_premium`.
"""

import os
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

API_BASE = "https://api.revenuecat.com/v1"


def entitlement_id() -> str:
    return os.getenv("REVENUECAT_ENTITLEMENT_ID", "premium")


def is_configured() -> bool:
    return bool(os.getenv("REVENUECAT_SECRET_API_KEY"))


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def fetch_premium_active(app_user_id: str) -> bool:
    """True se o entitlement Premium do usuário estiver ativo agora."""
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.get(
            f"{API_BASE}/subscribers/{quote(app_user_id, safe='')}",
            headers={"Authorization": f"Bearer {os.getenv('REVENUECAT_SECRET_API_KEY', '')}"},
        )
        response.raise_for_status()

    entitlement = (
        response.json().get("subscriber", {}).get("entitlements", {}).get(entitlement_id())
    )
    if not entitlement:
        return False

    now = datetime.now(timezone.utc)
    expires = entitlement.get("expires_date")
    if expires is None:  # compra vitalícia
        return True
    if _parse_iso(expires) > now:
        return True
    grace = entitlement.get("grace_period_expires_date")
    return bool(grace) and _parse_iso(grace) > now
