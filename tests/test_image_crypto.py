import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from noteford.image_crypto import (
    V2_MAGIC,
    decrypt_v2_bytes,
    image_extension,
)


def test_decrypt_synthetic_v2_image():
    aes_key = b'0123456789abcdef'
    xor_key = 0x5A
    first = b'\xff\xd8\xffJPEGDATA!'
    aes_size = len(first)
    pad = 16 - (len(first) % 16)
    padded = first + bytes([pad]) * pad
    enc = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
    encrypted = enc.update(padded) + enc.finalize()
    raw = b'RAW'
    tail_plain = b'\xff\xd9'
    tail = bytes(b ^ xor_key for b in tail_plain)
    header = V2_MAGIC + struct.pack('<LL', aes_size, len(tail)) + b'\x00'
    container = header + encrypted + raw + tail

    plain = decrypt_v2_bytes(container, aes_key, xor_key)
    assert plain.startswith(b'\xff\xd8\xff')
    assert plain.endswith(b'\xff\xd9')
    assert image_extension(plain) == 'jpg'
