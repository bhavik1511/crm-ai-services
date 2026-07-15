import asyncio
from agent import ask_question_async

async def main():
    history = [
        {"role": "user", "content": "A00147-108-24952"}
    ]
    ans, chart, nav = await ask_question_async(history)
    print("\n--- AGENT RESPONSE ---")
    print(ans)

if __name__ == "__main__":
    asyncio.run(main())
