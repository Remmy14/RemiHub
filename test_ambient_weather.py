import os
import requests


API_KEY = os.environ["AMBIENT_API_KEY"]
APPLICATION_KEY = os.environ["AMBIENT_APPLICATION_KEY"]

url = "https://api.ambientweather.net/v1/devices"

params = {
    "apiKey": API_KEY,
    "applicationKey": APPLICATION_KEY,
}

response = requests.get(url, params=params, timeout=20)
response.raise_for_status()

devices = response.json()

for device in devices:
    print("Device:", device.get("info", {}).get("name"))
    print("MAC:", device.get("macAddress"))
    print("Last data:")
    print(device.get("lastData"))
    print()
