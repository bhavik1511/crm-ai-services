import asyncio
import json
from agent import ask_question

history = [{"role": "user", "content": "fetch employees data"}]

try:
    async def main():
        from agent import ask_question_async
        ans, chart, nav = await ask_question_async(history)
        print("ANSWER:", ans)
    asyncio.run(main())
except Exception as e:
    print("FAILED:", str(e))
