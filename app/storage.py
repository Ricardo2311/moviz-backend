"""
Storage backend abstraction for uploaded files.

STORAGE_BACKEND=local (default) — saves files to ./uploads/ on disk (dev only)
STORAGE_BACKEND=s3              — uploads to any S3-compatible bucket (AWS S3, Cloudflare R2, etc.)

Required env vars for s3 backend:
  S3_BUCKET_NAME        bucket to upload into
  S3_ACCESS_KEY_ID      access key ID
  S3_SECRET_ACCESS_KEY  secret access key
  S3_PUBLIC_URL_BASE    public base URL used to construct object URLs returned to clients

Optional env vars for s3 backend:
  S3_ENDPOINT_URL       custom endpoint (required for Cloudflare R2 and other non-AWS providers)
  S3_REGION             defaults to "us-east-1"
"""

import os
from pathlib import Path
from uuid import uuid4

STORAGE_BACKEND: str = os.getenv("STORAGE_BACKEND", "local")

ALLOWED_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_UPLOAD_BYTES = 5 * 1024 * 1024  # 5 MB

_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

# Magic-byte signatures for each accepted image type.
# Checking file content (not just the extension) prevents renaming malicious
# files to .jpg and uploading them.
_MAGIC_BYTES: dict[str, list[bytes]] = {
    ".jpg":  [b"\xff\xd8\xff"],
    ".jpeg": [b"\xff\xd8\xff"],
    ".png":  [b"\x89PNG\r\n\x1a\n"],
    ".webp": [b"RIFF"],          # verified together with offset-8 "WEBP" marker below
    ".gif":  [b"GIF87a", b"GIF89a"],
}


def _check_magic_bytes(suffix: str, content: bytes) -> bool:
    """Return True when the file content matches a known signature for *suffix*."""
    signatures = _MAGIC_BYTES.get(suffix, [])
    for sig in signatures:
        if content[: len(sig)] == sig:
            if suffix == ".webp":
                # WEBP files are RIFF containers: bytes 8-11 must be "WEBP"
                return len(content) >= 12 and content[8:12] == b"WEBP"
            return True
    return False

# --- local backend config ---
_BACKEND_DIR = Path(__file__).resolve().parent.parent
PROFILE_UPLOAD_DIR = _BACKEND_DIR / "uploads" / "profile-images"
PUBLIC_ASSET_BASE_URL = os.getenv("PUBLIC_ASSET_BASE_URL", "http://localhost:8000").rstrip("/")

# --- S3 backend config ---
S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME", "")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL") or None  # None → AWS default
S3_ACCESS_KEY_ID = os.getenv("S3_ACCESS_KEY_ID", "")
S3_SECRET_ACCESS_KEY = os.getenv("S3_SECRET_ACCESS_KEY", "")
S3_REGION = os.getenv("S3_REGION", "us-east-1")
S3_PUBLIC_URL_BASE = os.getenv("S3_PUBLIC_URL_BASE", "").rstrip("/")

_s3_client = None


def _get_s3_client():
    global _s3_client
    if _s3_client is not None:
        return _s3_client

    try:
        import boto3
    except ImportError:
        raise RuntimeError(
            "boto3 is required for STORAGE_BACKEND=s3. "
            "Install it with: pip install boto3"
        )

    missing = [
        name
        for name, val in [
            ("S3_BUCKET_NAME", S3_BUCKET_NAME),
            ("S3_ACCESS_KEY_ID", S3_ACCESS_KEY_ID),
            ("S3_SECRET_ACCESS_KEY", S3_SECRET_ACCESS_KEY),
            ("S3_PUBLIC_URL_BASE", S3_PUBLIC_URL_BASE),
        ]
        if not val
    ]
    if missing:
        raise RuntimeError(
            f"Missing required env var(s) for STORAGE_BACKEND=s3: {', '.join(missing)}"
        )

    _s3_client = boto3.client(
        "s3",
        region_name=S3_REGION,
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY_ID,
        aws_secret_access_key=S3_SECRET_ACCESS_KEY,
    )
    return _s3_client


def _validate_and_read(file) -> tuple[str, bytes]:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError(
            "Unsupported image format. Allowed: jpg, jpeg, png, webp, gif"
        )
    content = file.file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise ValueError(
            f"Image too large. Maximum size is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB"
        )
    # Validate file content against known magic bytes so a renamed script
    # cannot be uploaded by simply giving it an image extension.
    if not _check_magic_bytes(suffix, content):
        raise ValueError(
            "File content does not match the declared image format. "
            "Please upload a real image file."
        )
    return suffix, content


def upload_profile_image(file) -> str:
    """Validate, store a profile image, and return its public URL."""
    suffix, content = _validate_and_read(file)
    object_key = f"profile-images/{uuid4().hex}{suffix}"

    if STORAGE_BACKEND == "s3":
        _get_s3_client().put_object(
            Bucket=S3_BUCKET_NAME,
            Key=object_key,
            Body=content,
            ContentType=_CONTENT_TYPES.get(suffix, "application/octet-stream"),
        )
        return f"{S3_PUBLIC_URL_BASE}/{object_key}"

    # local backend
    PROFILE_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    destination = PROFILE_UPLOAD_DIR / Path(object_key).name
    destination.write_bytes(content)
    return f"{PUBLIC_ASSET_BASE_URL}/uploads/{object_key}"


def delete_profile_image(image_url: str | None) -> None:
    """Delete a previously uploaded profile image. No-op for None or unrecognized URLs."""
    if not image_url:
        return

    if STORAGE_BACKEND == "s3":
        if not S3_PUBLIC_URL_BASE or not image_url.startswith(S3_PUBLIC_URL_BASE):
            return
        object_key = image_url[len(S3_PUBLIC_URL_BASE):].lstrip("/")
        try:
            _get_s3_client().delete_object(Bucket=S3_BUCKET_NAME, Key=object_key)
        except Exception:
            pass  # best-effort deletion; log in production
        return

    # local backend
    if "/uploads/profile-images/" not in image_url:
        return
    filename = image_url.rsplit("/", 1)[-1]
    target = PROFILE_UPLOAD_DIR / filename
    if target.exists():
        target.unlink()
