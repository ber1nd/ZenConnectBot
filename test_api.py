import openai
import os

# Set up your OpenAI client using environment variables
openai.api_key = os.getenv("API_KEY")

def test_openai_connection():
    try:
        response = openai.ChatCompletion.create(
            model="gpt-4",
            messages=[{"role": "user", "content": "Hello, are you working?"}],
            max_tokens=10,
            temperature=0.7
        )
        print("OpenAI API test successful:", response['choices'][0]['message']['content'].strip())
    except Exception as e:
        print(f"OpenAI API test failed: {type(e).__name__}: {str(e)}")

if __name__ == "__main__":
    test_openai_connection()
