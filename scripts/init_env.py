"""Create a fresh .env interactively. Existing files are never overwritten."""
import getpass, hashlib, os, secrets
from pathlib import Path
p=Path(__file__).resolve().parent.parent/'.env'
if p.exists():
    raise SystemExit('.env already exists. Edit it directly to rotate exchange credentials.')
pw=getpass.getpass('Administrator password: ')
if len(pw)<8:
    raise SystemExit('Use at least 8 characters.')
salt=secrets.token_bytes(16)
hashed=hashlib.scrypt(pw.encode(),salt=salt,n=16384,r=8,p=1).hex()
key=getpass.getpass('Delta API key (hidden): ')
secret=getpass.getpass('Delta API secret (hidden): ')
if any(c in key+secret for c in '\n\r'):
    raise SystemExit('Invalid credentials.')
p.write_text(f'ADMIN_PASSWORD_HASH={salt.hex()}:{hashed}\nSESSION_SECRET={secrets.token_hex(48)}\nDELTA_ENV=testnet\nDELTA_API_KEY={key}\nDELTA_API_SECRET={secret}\nCOOKIE_SECURE=true\nPUBLIC_ORIGIN=https://tokeniq.rookcapital.in\nBACKEND_URL=http://127.0.0.1:8100\n')
os.chmod(p,0o600)
print('Created protected .env. For localhost HTTP, set COOKIE_SECURE=false.')
