import os
import socket
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env", override=False)

host = os.getenv("DECODER_HOST", "192.168.0.111")
port = int(os.getenv("DECODER_PORT", "8899"))

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
s.connect((host, port))
print(f"connected to {host}:{port}")
while True:
    try:
        data = s.recv(1024)
        if not data:
            print("connected closed")
            break
        print(data.hex())
    except socket.timeout:
        continue
