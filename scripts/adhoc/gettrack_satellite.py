import requests
import math



API_KEY ="AIzaSyDn-3-lOQIxx9rDNAwVA7UAB9HlsOtY6XY"
LAT, LNG = 22.742304850060208, 120.32173316061305
ZOOM = 19
SIZE = "1280x1280"
SCALE = 2
url = "https://maps.googleapis.com/maps/api/staticmap"
params = {
    "center": f"{LAT},{LNG}",
    "zoom": ZOOM,
    "size": SIZE,
    "scale": SCALE,
    "maptype": "satellite",
    "key": API_KEY,
}

resp = requests.get(url, params=params)
resp.raise_for_status()

with open("tks_qiaotou_track.png1", "wb") as f:
    f.write(resp.content)

print("saved:", resp.url)

def meters_per_pixel(lat_deg, zoom, scale):
    return (156543.03392 * math.cos(math.radians(lat_deg))) / (2 ** zoom) / scale

mpp = meters_per_pixel(LAT, ZOOM, SCALE)
print(f"{mpp:.4f} 公尺/像素")