from semantic_layer import _run_query

def test_invoices():
    # Find invoices with negative outstanding
    q = """
    SELECT i.id, i.invoice_no, i.total_net_amount, i.paid_amount,
           (SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id) as receipt_sum,
           (SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id AND cn.is_active = 1) as cn_sum,
           ROUND(i.total_net_amount - COALESCE((
               SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id
           ), 0) - COALESCE((
               SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id AND cn.is_active = 1
           ), 0), 3) AS outstanding_amount
    FROM invoice i
    WHERE  ROUND(i.total_net_amount - COALESCE((
               SELECT SUM(rd.applied_amount) FROM receipt_details rd WHERE rd.invoice_id = i.id
           ), 0) - COALESCE((
               SELECT SUM(cn.total_amount) FROM credit_note cn WHERE cn.invoice_id = i.id AND cn.is_active = 1
           ), 0), 3) < 0
    LIMIT 5
    """
    res = _run_query(q)
    print("Negative Invoices:")
    for r in res:
        print(f"ID: {r['id']}, Invoice No: {r['invoice_no']}, Net: {r['total_net_amount']}, "
              f"Paid: {r['paid_amount']}, Receipt Sum: {r['receipt_sum']}, "
              f"CN Sum: {r['cn_sum']}, Outstanding: {r['outstanding_amount']}")

if __name__ == "__main__":
    test_invoices()
