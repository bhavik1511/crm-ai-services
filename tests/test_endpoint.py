import requests
import json

url = "http://localhost:8000/ask-ai"
payload = {
    "messages": [{"role": "user", "content": "A00147-108-24952"}]
}
headers = {'Content-Type': 'application/json'}

print("Sending request to backend...")
response = requests.post(url, json=payload, headers=headers)
print(f"Status: {response.status_code}")
print(f"Response: {response.json()}")
