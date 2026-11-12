"""Authenticated encryption for money fields in the vault and ledger."""
import base64
import os
import unicodedata

from argon2.low_level import Type, hash_secret_raw
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = 1
KDF_MEMORY_KIB = 64 * 1024
KDF_TIME_COST = 3
KDF_PARALLELISM = 1
SALT_BYTES = 16
KEY_BYTES = 32
NONCE_BYTES = 12
MIN_PASSPHRASE_LENGTH = 10
_KEY_AAD = b"Lumina:money-key:v1"


class InvalidPassphrase(ValueError):
    """The supplied passphrase cannot unwrap the configured data key."""


def _encode(value):
    return base64.b64encode(value).decode("ascii")


def _decode(value):
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("加密配置损坏") from exc


def _derive_key(passphrase, salt, memory_kib, time_cost, parallelism):
    normalized = unicodedata.normalize("NFKC", passphrase).encode("utf-8")
    return hash_secret_raw(
        secret=normalized,
        salt=salt,
        time_cost=time_cost,
        memory_cost=memory_kib,
        parallelism=parallelism,
        hash_len=KEY_BYTES,
        type=Type.ID,
    )


def create_key_envelope(passphrase):
    """Return serializable KDF/wrapped-key metadata and a fresh random DEK."""
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise ValueError(f"保护口令至少需要 {MIN_PASSPHRASE_LENGTH} 个字符")
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    data_key = os.urandom(KEY_BYTES)
    wrapping_key = _derive_key(
        passphrase, salt, KDF_MEMORY_KIB, KDF_TIME_COST, KDF_PARALLELISM)
    wrapped_key = AESGCM(wrapping_key).encrypt(nonce, data_key, _KEY_AAD)
    metadata = {
        "version": VERSION,
        "kdf": "argon2id",
        "memory_kib": KDF_MEMORY_KIB,
        "time_cost": KDF_TIME_COST,
        "parallelism": KDF_PARALLELISM,
        "salt": _encode(salt),
        "wrap_nonce": _encode(nonce),
        "wrapped_key": _encode(wrapped_key),
    }
    return metadata, data_key


def unlock_key(metadata, passphrase):
    """Unwrap a DEK from config metadata; raises InvalidPassphrase on mismatch."""
    if not isinstance(passphrase, str) or len(passphrase) < MIN_PASSPHRASE_LENGTH:
        raise ValueError(f"保护口令至少需要 {MIN_PASSPHRASE_LENGTH} 个字符")
    if not isinstance(metadata, dict) or type(metadata.get("version")) is not int \
            or metadata.get("version") != VERSION:
        raise ValueError("不支持的加密配置版本")
    if metadata.get("kdf") != "argon2id":
        raise ValueError("不支持的密钥派生算法")
    try:
        salt = _decode(metadata["salt"])
        nonce = _decode(metadata["wrap_nonce"])
        wrapped = _decode(metadata["wrapped_key"])
        memory_kib = int(metadata["memory_kib"])
        time_cost = int(metadata["time_cost"])
        parallelism = int(metadata["parallelism"])
        if (len(salt) != SALT_BYTES or len(nonce) != NONCE_BYTES
                or len(wrapped) != KEY_BYTES + 16
                or not 8 * 1024 <= memory_kib <= 256 * 1024
                or not 1 <= time_cost <= 10 or not 1 <= parallelism <= 16):
            raise ValueError("加密配置参数无效")
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("加密配置损坏") from exc
    wrapping_key = _derive_key(passphrase, salt, memory_kib, time_cost, parallelism)
    try:
        return AESGCM(wrapping_key).decrypt(nonce, wrapped, _KEY_AAD)
    except InvalidTag as exc:
        raise InvalidPassphrase("保护口令错误") from exc


def rewrap_key_envelope(metadata, current_passphrase, new_passphrase):
    """Verify the current passphrase and wrap the same DEK with a new one.

    Only the key envelope changes: all amount ciphertexts remain decryptable with
    the unchanged data-encryption key.
    """
    if (not isinstance(new_passphrase, str)
            or len(new_passphrase) < MIN_PASSPHRASE_LENGTH):
        raise ValueError(f"保护口令至少需要 {MIN_PASSPHRASE_LENGTH} 个字符")
    data_key = unlock_key(metadata, current_passphrase)
    memory_kib = int(metadata["memory_kib"])
    time_cost = int(metadata["time_cost"])
    parallelism = int(metadata["parallelism"])
    salt = os.urandom(SALT_BYTES)
    nonce = os.urandom(NONCE_BYTES)
    wrapping_key = _derive_key(
        new_passphrase, salt, memory_kib, time_cost, parallelism)
    wrapped_key = AESGCM(wrapping_key).encrypt(nonce, data_key, _KEY_AAD)
    updated = dict(metadata)
    updated.update({
        "salt": _encode(salt),
        "wrap_nonce": _encode(nonce),
        "wrapped_key": _encode(wrapped_key),
    })
    return updated


def encrypt_amount(data_key, table, row_id, field, cents):
    """Encrypt a positive integer-cent amount into nonce || ciphertext || tag."""
    if len(data_key) != KEY_BYTES:
        raise ValueError("金额数据密钥无效")
    if type(cents) is not int or cents <= 0:
        raise ValueError("金额必须是正整数分")
    nonce = os.urandom(NONCE_BYTES)
    aad = f"Lumina:{table}:{int(row_id)}:{field}:v{VERSION}".encode("ascii")
    ciphertext = AESGCM(data_key).encrypt(nonce, str(cents).encode("ascii"), aad)
    return nonce + ciphertext


def decrypt_amount(data_key, table, row_id, field, payload):
    """Authenticate and decrypt a stored amount, returning integer cents."""
    if len(data_key) != KEY_BYTES or not isinstance(payload, (bytes, bytearray)):
        raise ValueError("加密金额格式无效")
    payload = bytes(payload)
    if len(payload) < NONCE_BYTES + 1 + 16:
        raise ValueError("加密金额数据不完整")
    nonce, ciphertext = payload[:NONCE_BYTES], payload[NONCE_BYTES:]
    aad = f"Lumina:{table}:{int(row_id)}:{field}:v{VERSION}".encode("ascii")
    try:
        plain = AESGCM(data_key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise ValueError("加密金额校验失败，数据可能已损坏或被篡改") from exc
    try:
        cents = int(plain.decode("ascii"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError("加密金额内容无效") from exc
    if cents <= 0:
        raise ValueError("加密金额超出有效范围")
    return cents
