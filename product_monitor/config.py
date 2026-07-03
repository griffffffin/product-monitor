import os

if os.getenv("INVOCATION_ID"):  # systemd service
    LOG_FILE = "/var/log/product-monitor/service.log"
else:
    LOG_FILE = "product-monitor.log"

# SMTP credentials come from a shared environment file (several projects on
# the same host use the same Gmail account) - see EnvironmentFile=/opt/secrets.env
# in the systemd unit. The .get() fallback ensures a missing env var (e.g.
# during test runs) doesn't raise at import/collection time.
EMAIL_CONFIG = {
    "smtp_server": os.environ.get("PRODUCT_MONITOR_SMTP_SERVER", "smtp.gmail.com"),
    "smtp_port": int(os.environ.get("PRODUCT_MONITOR_SMTP_PORT", "587")),
    "sender_email": os.environ.get("PRODUCT_MONITOR_SENDER_EMAIL", ""),
    "sender_password": os.environ.get("PRODUCT_MONITOR_SENDER_PASSWORD", ""),
    "recipient_email": os.environ.get("PRODUCT_MONITOR_RECIPIENT_EMAIL", ""),
}
