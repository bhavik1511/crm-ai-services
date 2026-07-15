from semantic_layer import get_pipeline_and_proposals

def test():
    print("Testing Q1 Pipeline Leads...")
    res = get_pipeline_and_proposals.invoke({
        "start_date": "2025-10-01", 
        "end_date": "2025-12-31 23:59:59"
    })
    print(res)

if __name__ == "__main__":
    test()
