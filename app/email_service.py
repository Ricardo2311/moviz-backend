import os
import smtplib
from email.message import EmailMessage
from urllib.parse import quote


FRONTEND_APP_URL = os.getenv("FRONTEND_APP_URL", "http://localhost:3002").rstrip("/")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
SMTP_FROM_EMAIL = os.getenv("SMTP_FROM_EMAIL", SMTP_USERNAME or "no-reply@moviz.local")
SMTP_USE_TLS = os.getenv("SMTP_USE_TLS", "true").lower() == "true"
PASSWORD_RESET_DEV_FALLBACK = os.getenv("PASSWORD_RESET_DEV_FALLBACK", "true").lower() == "true"


def build_password_reset_link(token: str):
    return f"{FRONTEND_APP_URL}/reset-password?token={quote(token)}"


def is_smtp_configured():
    return bool(SMTP_HOST and SMTP_FROM_EMAIL)


def send_password_reset_email(recipient_email: str, token: str):
    if not SMTP_HOST or not SMTP_FROM_EMAIL:
        raise RuntimeError("SMTP is not configured")

    reset_link = build_password_reset_link(token)
    message = EmailMessage()
    message["Subject"] = "Moviz - redefinicao de senha"
    message["From"] = SMTP_FROM_EMAIL
    message["To"] = recipient_email
    message.set_content(
        "Voce solicitou a redefinicao da sua senha no Moviz.\n\n"
        f"Acesse este link para criar uma nova senha:\n{reset_link}\n\n"
        "Se voce nao pediu essa alteracao, ignore este email."
    )

    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as server:
        if SMTP_USE_TLS:
            server.starttls()
        if SMTP_USERNAME:
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
        server.send_message(message)
