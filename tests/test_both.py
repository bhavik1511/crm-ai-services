import requests
import json

url = "http://localhost:8000/ask-ai"
headers = {'Content-Type': 'application/json'}

def test_query(q, filename):
    print(f"\n--- Testing Query: '{q}' ---")
    payload = {"messages": [{"role": "user", "content": q}]}
    try:
        response = requests.post(url, json=payload, headers=headers)
        data = response.json()
        ans = data.get('answer', 'No answer')
        with open(filename, "w", encoding="utf-8") as f:
            f.write(ans)
        val = ans[:500] + "...\n(truncated)" if len(ans) > 500 else ans
        print(f"Saved to {filename}. Head: {val}")
    except Exception as e:
        print(f"Error: {e}")

test_query("AAJ Holding", "aaj_out.txt")
test_query("What is total revenue this year?", "rev_out.txt")
