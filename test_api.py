import os
from openai import AsyncOpenAI
import asyncio

# Set up your OpenAI client using environment variables
client = AsyncOpenAI(api_key=os.getenv("API_KEY"))

async def test_openai_connection():
    try:
        response = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Hello, are you working?"}],
            max_tokens=10,
            temperature=0.7
        )
        print("OpenAI API test successful:", response.choices[0].message.content.strip())
    except Exception as e:
        print(f"OpenAI API test failed: {type(e).__name__}: {str(e)}")

async def main():
    await test_openai_connection()

if __name__ == '__main__':
    asyncio.run(main())