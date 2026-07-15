import traceback
from agent import ask_question

try:
    history = [{"role": "user", "content": "What are the high value proposals?"}]
    print("--- HIGH VALUE PROPOSALS ---")
    res, chart = ask_question(history)
    print("Result:")
    print(res)
    if chart:
        print("\nChart Data:")
        print(chart)
except Exception as e:
    print(traceback.format_exc())
