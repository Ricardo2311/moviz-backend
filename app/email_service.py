"""Email delivery via Resend HTTP API (preferred) with SMTP fallback.

Set RESEND_API_KEY for the Resend HTTP API (recommended).
Alternatively set SMTP_HOST + SMTP_USERNAME + SMTP_PASSWORD for SMTP.
"""

import os
import smtplib
from email.message import EmailMessage
from urllib.parse import quote

import httpx

FRONTEND_APP_URL     = os.getenv("FRONTEND_APP_URL", "http://localhost:3002").rstrip("/")

# Resend HTTP API (preferred)
RESEND_API_KEY       = os.getenv("RESEND_API_KEY", "")

# SMTP fallback
SMTP_HOST            = os.getenv("SMTP_HOST", "")
SMTP_PORT            = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME        = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD        = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL      = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME or "no-reply@moviz.local")
SMTP_USE_TLS         = os.getenv("SMTP_USE_TLS", "true").lower() == "true"

PASSWORD_RESET_DEV_FALLBACK = os.getenv("PASSWORD_RESET_DEV_FALLBACK", "true").lower() == "true"


def build_password_reset_link(token: str) -> str:
    return f"{FRONTEND_APP_URL}/reset-password?token={quote(token)}"


def is_smtp_configured() -> bool:
    return bool(RESEND_API_KEY or (SMTP_HOST and SMTP_FROM_EMAIL))


def _send_via_resend(to: str, subject: str, html: str, text: str) -> None:
    """Send using Resend HTTP API — clear errors, delivery tracking."""
    from_addr = os.getenv("SMTP_FROM_EMAIL", "onboarding@resend.dev")
    resp = httpx.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": from_addr, "to": [to], "subject": subject, "html": html, "text": text},
        timeout=20,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Resend API error {resp.status_code}: {resp.text}")


def _send_via_smtp(to: str, subject: str, text: str) -> None:
    """Send using SMTP (fallback)."""
    message = EmailMessage()
    message["Subject"] = subject
    message["From"]    = SMTP_FROM_EMAIL
    message["To"]      = to
    message.set_content(text)
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        if SMTP_USE_TLS:
            server.starttls()
        if SMTP_USERNAME:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(message)


def send_password_reset_email(recipient_email: str, token: str) -> None:
    reset_link = build_password_reset_link(token)

    subject = "Moviz — redefinição de senha"
    text = (
        "Você solicitou a redefinição da sua senha no Moviz.\n\n"
        f"Acesse este link para criar uma nova senha:\n{reset_link}\n\n"
        "O link expira em 30 minutos.\n\n"
        "Se você não pediu essa alteração, ignore este e-mail."
    )
    html = f"""
    <div style="font-family:system-ui,sans-serif;max-width:480px;margin:0 auto;padding:32px 24px;color:#0e1320;">
      <h2 style="font-size:20px;margin:0 0 8px;">Redefinição de senha</h2>
      <p style="color:#4a5878;margin:0 0 24px;">Você solicitou a redefinição da sua senha no <strong>Moviz</strong>.</p>
      <a href="{reset_link}"
         style="display:inline-block;padding:12px 24px;background:#2f5bef;color:#fff;border-radius:8px;text-decoration:none;font-weight:600;font-size:15px;">
        Redefinir senha
      </a>
      <p style="margin:24px 0 0;font-size:12px;color:#7a8baa;">
        O link expira em 30 minutos. Se você não pediu essa alteração, ignore este e-mail.
      </p>
    </div>
    """

    if RESEND_API_KEY:
        _send_via_resend(recipient_email, subject, html, text)
    else:
        _send_via_smtp(recipient_email, subject, text)
