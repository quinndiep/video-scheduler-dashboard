"""
Security module for encrypted credential storage and sensitive data redaction.
"""

from cryptography.fernet import Fernet
import keyring
import logging
import re
import json
import uuid
from typing import Dict, Any


class CredentialManager:
    """Manages encrypted credential storage using Fernet encryption."""
    
    SERVICE_NAME = "video-scheduler"
    KEY_NAME = "encryption_key"
    
    def __init__(self):
        self.key = self._get_or_create_key()
        self.fernet = Fernet(self.key)
    
    def _get_or_create_key(self) -> bytes:
        """
        Retrieve encryption key from macOS Keychain, or create if missing.
        
        Returns:
            bytes: Fernet encryption key
        """
        try:
            key = keyring.get_password(self.SERVICE_NAME, self.KEY_NAME)
            if not key:
                # First run - generate and store permanently
                key = Fernet.generate_key().decode()
                keyring.set_password(self.SERVICE_NAME, self.KEY_NAME, key)
                logging.info("Generated new encryption key and stored in Keychain")
            return key.encode()
        except Exception as e:
            logging.error(f"Failed to access Keychain: {e}")
            raise
    
    def encrypt(self, data: str) -> str:
        """
        Encrypt sensitive data.
        
        Args:
            data: Plain text string to encrypt
            
        Returns:
            str: Encrypted string (base64 encoded)
        """
        if not data:
            return data
        return self.fernet.encrypt(data.encode()).decode()
    
    def decrypt(self, encrypted: str) -> str:
        """
        Decrypt sensitive data.
        
        Args:
            encrypted: Encrypted string to decrypt
            
        Returns:
            str: Decrypted plain text string
        """
        if not encrypted:
            return encrypted
        return self.fernet.decrypt(encrypted.encode()).decode()
    
    def rotate_key(self, users_data: Dict[str, Any]) -> None:
        """
        Rotate encryption key (decrypt with old, re-encrypt with new).
        
        Args:
            users_data: Dictionary of user data with encrypted tokens
        """
        # TODO: Implement in future when key rotation is needed
        pass


class SensitiveDataFilter(logging.Filter):
    """Redact sensitive data (tokens, passwords) from logs.

    Every pattern keeps the *label* in group 1 and puts the secret in group 2, so
    the replacement ``\\1[REDACTED]`` keeps the log line readable while removing
    the credential.  Patterns without a label redact the whole match.
    """

    PATTERNS = [
        # Google OAuth access tokens (label is the "ya29." prefix itself)
        r'(ya29\.)([A-Za-z0-9\-._~+/]+=*)',
        # Generic access / refresh tokens, JSON or key=value style
        r'((?:access_token|ACCESS_TOKEN)["\'\s:=]+)([A-Za-z0-9\-._~+/]+=*)',
        r'((?:refresh_token|REFRESH_TOKEN)["\'\s:=]+)([A-Za-z0-9\-._~+/]+=*)',
        # Bearer tokens (covers "Authorization: Bearer <token>" as well)
        r'((?:bearer|Bearer)\s+)([A-Za-z0-9\-._~+/]+=*)',
        r'(Authorization:\s*)(?!\[REDACTED\])([A-Za-z0-9\-._~+/]{8,})',
        # Platform‑specific tokens
        r'((?:instagram_token|INSTAGRAM_ACCESS_TOKEN)["\'\s:=]+)([A-Za-z0-9\-._~+/]+=*)',
        r'((?:facebook_token|FACEBOOK_ACCESS_TOKEN)["\'\s:=]+)([A-Za-z0-9\-._~+/]+=*)',
        r'((?:tiktok_token|TIKTOK_ACCESS_TOKEN)["\'\s:=]+)([A-Za-z0-9\-._~+/]+=*)',
        # Google client secrets and OAuth client ids
        r'((?:client_secret|CLIENT_SECRET)["\'\s:=]+)([A-Za-z0-9\-._~+/]{10,})',
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact secrets in ``record.msg`` and any pre‑formatted ``args``."""
        message = str(record.msg)
        for pattern in self.PATTERNS:
            message = re.sub(pattern, r'\1[REDACTED]', message, flags=re.IGNORECASE)
        record.msg = message
        # A logger called with %-style args would otherwise keep the raw secret
        # in record.args and print it when the handler formats the record.
        if record.args:
            record.args = self._redact_args(record.args)
        return True

    def _redact_args(self, args):
        """Apply every pattern to each string inside a logging ``args`` tuple."""
        if isinstance(args, dict):
            return {
                key: self._redact_text(val) if isinstance(val, str) else val
                for key, val in args.items()
            }
        if isinstance(args, tuple):
            return tuple(
                self._redact_text(arg) if isinstance(arg, str) else arg
                for arg in args
            )
        return self._redact_text(args) if isinstance(args, str) else args

    def _redact_text(self, text: str) -> str:
        """Return ``text`` with every known token pattern redacted."""
        for pattern in self.PATTERNS:
            text = re.sub(pattern, r'\1[REDACTED]', text, flags=re.IGNORECASE)
        return text


def setup_secure_logging():
    """Install the sensitive‑data filter so no token ever reaches a log handler.

    The filter is attached to the root logger *and* to every handler that logging
    has configured, because filters on a logger are not applied to records that
    propagate up from child loggers.
    """
    root = logging.getLogger()
    log_filter = SensitiveDataFilter()
    root.addFilter(log_filter)
    for handler in root.handlers:
        handler.addFilter(log_filter)
    logging.info("Secure logging filter installed")


def generate_account_id() -> str:
    """Return a stable UUID string for a YouTube account entry.

    This tiny helper lives in ``security.py`` so that ``server_secure.py`` can
    import it without a circular dependency.
    """
    return str(uuid.uuid4())


def migrate_users_to_encrypted(users_file_path: str, backup: bool = True):
    """
    One-time migration from plain-text to encrypted users.json.
    
    Args:
        users_file_path: Path to users.json file
        backup: Whether to create timestamped backup (default: True)
    """
    import shutil
    from datetime import datetime
    
    # Backup
    if backup:
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{users_file_path}.bak.{timestamp}"
        shutil.copy(users_file_path, backup_path)
        logging.info(f"Backed up users.json to {backup_path}")
    
    # Load plain‑text data (flat ``{email: record}`` map, legacy wrapper tolerated)
    with open(users_file_path, "r") as f:
        users = json.load(f)
    if set(users.keys()) == {"users"} and isinstance(users["users"], dict):
        users = users["users"]
    
    # Encrypt all tokens
    cm = CredentialManager()
    for user_id, user_data in users.items():
        # Encrypt Google token – may be a dict or a plain string
        if "google_token" in user_data and not str(user_data["google_token"]).startswith("gAAAAA"):
            token_val = user_data["google_token"]
            if isinstance(token_val, dict):
                token_val = json.dumps(token_val)
            user_data["google_token"] = cm.encrypt(token_val)
        
        # Add account_id to YouTube accounts if missing and encrypt token (handle dicts)
        for yt_account in user_data.get("youtube_accounts", []):
            if "account_id" not in yt_account:
                yt_account["account_id"] = str(uuid.uuid4())
                logging.info(f"Added account_id to YouTube account: {yt_account.get('channel_name', 'Unknown')}")
            if "token" in yt_account and not str(yt_account["token"]).startswith("gAAAAA"):
                token_val = yt_account["token"]
                if isinstance(token_val, dict):
                    token_val = json.dumps(token_val)
                yt_account["token"] = cm.encrypt(token_val)
        
        # Encrypt platform tokens – handle possible dict values
        for platform in ["instagram_token", "facebook_token", "tiktok_token"]:
            if platform in user_data and not str(user_data[platform]).startswith("gAAAAA"):
                token_val = user_data[platform]
                if isinstance(token_val, dict):
                    token_val = json.dumps(token_val)
                user_data[platform] = cm.encrypt(token_val)
    
    # Save encrypted
    with open(users_file_path, "w") as f:
        json.dump(users, f, indent=2)
    
    logging.info("Migrated users.json to encrypted format")


if __name__ == "__main__":
    # Test encryption
    cm = CredentialManager()
    test_token = "ya29.test_token_12345"
    encrypted = cm.encrypt(test_token)
    decrypted = cm.decrypt(encrypted)
    
    print(f"Original: {test_token}")
    print(f"Encrypted: {encrypted}")
    print(f"Decrypted: {decrypted}")
    print(f"Match: {test_token == decrypted}")
    
    # Test logging filter
    setup_secure_logging()
    logging.basicConfig(level=logging.INFO)
    logging.info(f"This should be redacted: access_token: {test_token}")
