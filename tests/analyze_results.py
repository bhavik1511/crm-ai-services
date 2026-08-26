import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

r = json.load(open('tests/validation_results.json', encoding='utf-8'))
passed = [x for x in r if x['passed']]
failed  = [x for x in r if not x['passed']]
rate_limited = [x for x in r if 'rate_limit' in str(x.get('answer_excerpt',''))]
planner_err  = [x for x in r if 'error trying to plan' in str(x.get('answer_excerpt',''))]
sql_used_list = [x for x in r if x['sql_used']]

print(f"Total={len(r)}  Passed={len(passed)}  Failed={len(failed)}")
print(f"Rate-limit hits={len(rate_limited)}  Planner-error={len(planner_err)}  SQL-used={len(sql_used_list)}")
print()
print("=== PASSED TESTS ===")
for x in passed:
    ans = x.get('answer_excerpt','')[:100].replace('\n',' ')
    print(f"[{x['test_id']:>3}] {x['category']:<14} sql={str(x['sql_used']):<5} {x['elapsed_ms']:>6}ms | {x['question'][:55]}")
    print(f"       ans: {ans}")

print()
print("=== FAILED (non-rate-limit) ===")
genuinely_failed = [x for x in failed if 'rate_limit' not in str(x.get('answer_excerpt','')) and 'error trying to plan' not in str(x.get('answer_excerpt',''))]
for x in genuinely_failed:
    print(f"[{x['test_id']:>3}] {x['category']:<14} | {x['question'][:55]}")
    print(f"       root_cause: {x['root_cause']}")
    print(f"       answer: {str(x.get('answer_excerpt',''))[:120]}")
