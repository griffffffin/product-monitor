import asyncio
import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict


class EmailNotifier:
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self._connection_pool = None

    async def send_notification(self, subject: str, body: str) -> bool:
        """Async email send in a thread pool"""
        loop = asyncio.get_event_loop()
        try:
            result = await loop.run_in_executor(None, self._send_email_sync, subject, body)
            return result
        except Exception as e:
            logging.error(f"Email küldési hiba: {e}")
            return False

    def _send_email_sync(self, subject: str, body: str) -> bool:
        """Sync email send"""
        try:
            message = MIMEMultipart()
            message["From"] = self.config["sender_email"]
            message["To"] = self.config["recipient_email"]
            message["Subject"] = subject
            message.attach(MIMEText(body, "plain", "utf-8"))

            with smtplib.SMTP(self.config["smtp_server"], self.config["smtp_port"]) as server:
                server.starttls()
                server.login(self.config["sender_email"], self.config["sender_password"])
                server.send_message(message)
            return True
        except Exception as e:
            logging.error(f"Email küldési hiba: {e}")
            return False
