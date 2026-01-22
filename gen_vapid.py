from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
import base64

private_key = ec.generate_private_key(ec.SECP256R1())
priv_pem = private_key.private_bytes(
    encoding=serialization.Encoding.PEM,
    format=serialization.PrivateFormat.PKCS8,
    encryption_algorithm=serialization.NoEncryption(),
)
pub = private_key.public_key()
pub_bytes = pub.public_bytes(
    encoding=serialization.Encoding.X962,
    format=serialization.PublicFormat.UncompressedPoint
)

def b64u(b): return base64.urlsafe_b64encode(b).decode().rstrip('=')

print("VAPID_PRIVATE_KEY_PEM:\n", priv_pem.decode())
print("\nVAPID_PUBLIC_KEY:", b64u(pub_bytes))
