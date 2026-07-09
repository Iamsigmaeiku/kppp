import socket
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(5)
s.connect(('192.168.0.111',8899))
print("connected")
while True:
    try:
        data = s.recv(1024)
        if not data: 
            print("connected closed")
            break
        print(data.hex())
    except socket.timeout:
        continue