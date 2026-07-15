from semantic_layer import get_pipeline_and_proposals

def test():
    print("Testing without employee_id...")
    res1 = get_pipeline_and_proposals.invoke({"start_date": "2025-10-01", "end_date": "2026-03-12 23:59:59"})
    print(res1)
    
    print("\nTesting WITH employee_id=31...")
    res2 = get_pipeline_and_proposals.invoke({"start_date": "2025-10-01", "end_date": "2026-03-12 23:59:59", "employee_id": 31})
    print(res2)

if __name__ == "__main__":
    test()
