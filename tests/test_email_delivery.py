import os
import unittest
from unittest.mock import MagicMock, patch

import job_monitor


class EmailDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "SMTP_SERVER": "smtp.example.test",
            "SMTP_PORT": "587",
            "SENDER_EMAIL": "sender@example.test",
            "SENDER_PASSWORD": "app-password",
            "RECIPIENT_EMAIL": "recipient@example.test",
        }

    def test_missing_credentials_fail_preflight(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(ValueError, "SENDER_EMAIL"):
                job_monitor.validate_email_config({})

    @patch("job_monitor.smtplib.SMTP")
    def test_success_requires_smtp_acceptance(self, smtp_cls):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.sendmail.return_value = {}
        smtp_cls.return_value = smtp

        with patch.dict(os.environ, self.env, clear=True):
            job_monitor.send_email({}, "Subject", "<p>Body</p>")

        smtp_cls.assert_called_once_with("smtp.example.test", 587, timeout=30)
        smtp.ehlo.assert_called()
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("sender@example.test", "app-password")
        smtp.sendmail.assert_called_once()

    @patch("job_monitor.smtplib.SMTP")
    def test_refused_recipient_fails_delivery(self, smtp_cls):
        smtp = MagicMock()
        smtp.__enter__.return_value = smtp
        smtp.sendmail.return_value = {"recipient@example.test": (550, b"rejected")}
        smtp_cls.return_value = smtp

        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(RuntimeError, "refused 1 recipient"):
                job_monitor.send_email({}, "Subject", "<p>Body</p>")


if __name__ == "__main__":
    unittest.main()
