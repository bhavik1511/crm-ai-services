import asyncio
from agent import ask_question_async

async def main():
    print("Testing AAJ Holding directly bypasses FastApi...")
    ans, chart, nav = await ask_question_async([{"role": "user", "content": "AAJ Holding"}])
    with open("direct_aaj_out.txt", "w", encoding="utf-8") as f:
        f.write(ans)
    print(f"Output saved. Length: {len(ans)}")

    print("Testing What is total revenue this year directly bypasses FastApi...")
    ans, chart, nav = await ask_question_async([{"role": "user", "content": "What is total revenue this year?"}])
    with open("direct_rev_out.txt", "w", encoding="utf-8") as f:
        f.write(ans)
    print(f"Output saved. Length: {len(ans)}")

if __name__ == "__main__":
    asyncio.run(main())
