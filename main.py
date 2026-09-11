import os

import requests

url = "https://api.anthropic.com/v1/messages"

headers = {
    "x-api-key": os.environ["ANTHROPIC_API_KEY"],
    "anthropic-version": "2023-06-01",
    "content-type": "application/json"
}

body = {
    "model": "claude-sonnet-4-5",
    "max_tokens": 1024,
    "system": "Your name is zanbot, a personal assistant for Zhanbo Chen",
    "messages": [
        {
            "role": "user",
            "content": "Hello, Zanbot!"
        }
    ]
}

response = requests.post(url, headers=headers, json=body)
response.raise_for_status()

data = response.json()
print(data)

print("----------------------------------------------------------------------")

print(data["content"][0]["text"])