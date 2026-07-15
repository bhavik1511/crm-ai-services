import requests
import json

url = "http://localhost:8000/ask-ai"
payload = {
    "messages": [{"role": "user", "content": "AAJ Holding"}]
}
headers = {'Content-Type': 'application/json'}

try:
    print("Testing name search 'AAJ Holding' with /ask-ai...")
    response = requests.post(url, json=payload, headers=headers)
    data = response.json()
    
    with open("response_aaj.txt", "w", encoding="utf-8") as f:
        f.write(data.get('answer', 'No answer'))
    print("Successfully wrote response to response_aaj.txt")
except Exception as e:
    print(f"Error: {e}")
