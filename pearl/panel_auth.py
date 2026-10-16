"""The private web panel uses its own password, separate from staff Telegram login."""

import hashlib
import hmac


def panel_password(config, webhook_secret):
    configured = getattr(config, "web_panel_password", "")
    if configured:
        if len(configured) < 16:
            raise ValueError("WEB_PANEL_PASSWORD: минимум 16 символов.")
        return configured
    return hmac.new(
        webhook_secret.encode(),
        ("pearl-owner-panel:" + config.admin_password).encode(),
        hashlib.sha256,
    ).hexdigest()
