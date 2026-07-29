import os
from dotenv import load_dotenv
from google import genai
from google.genai import types
# =================================

load_dotenv()

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

response = client.models.generate_content(
    model="gemini-3.6-flash",
    contents="A new member just joined. Welcome them in two sentences.",
    config=types.GenerateContentConfig(
        system_instruction=(
          "You are Alpha, the guide for Alpha Training Lab, a crypto "
          "trading community. You are calm, steady and concise. "
          "You never give trading advice."
        ),
        max_output_tokens=1000,
        thinking_config=types.ThinkingConfig(thinking_level="low")
    ),
)

# print(response.text)
print(response.text)
print("---")
print(response.usage_metadata)
print(response.candidates[0].finish_reason)
client.close()