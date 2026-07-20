#!/bin/bash
set -e
source ~/smartkart-lstm/.venv/bin/activate
export LD_LIBRARY_PATH=/usr/local/cuda-12.6/targets/aarch64-linux/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH:-}
echo "=== libs ==="
find /usr -name 'libcudnn.so*' 2>/dev/null | head || true
ls /usr/local/cuda-12.6/targets/aarch64-linux/lib/libcudart.so* 2>/dev/null | head || true
echo "=== torch ==="
python - <<'PY'
import torch
print("torch", torch.__version__)
print("cuda", torch.version.cuda)
print("available", torch.cuda.is_available())
if torch.cuda.is_available():
    print("gpu", torch.cuda.get_device_name(0))
    x = torch.zeros(1, 50, 8, device="cuda")
    print("tensor_ok", x.shape)
PY
