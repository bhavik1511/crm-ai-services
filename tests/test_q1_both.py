import traceback
from agent import ask_question

try:
    history1 = [{"role": "user", "content": "What is the total revenue for Quarter 1?"}]
    print("--- Q1 REVENUE ---")
    res1, _ = ask_question(history1)
    print("Result:", res1)

    history2 = [{"role": "user", "content": "How many open leads do we have in Quarter 1?"}]
    print("\n--- Q1 LEADS ---")
    res2, _ = ask_question(history2)
    print("Result:", res2)
except Exception as e:
    print(traceback.format_exc())
