from support_bot import tools

print(tools.get_customer("CUST-001"))                       # by customer_id
print(tools.get_customer("marcus.webb@example.com"))         # by email
print(tools.get_customer("nobody@example.com"))              # should return the {errorCategory,...} shape

print(tools.lookup_order("ORD-1002", "CUST-001"))            # valid
print(tools.lookup_order("ORD-1002", "CUST-002"))             # wrong customer -> permission error
print(tools.lookup_order("ORD-9999", "CUST-001"))             # missing order -> validation error

print(tools.process_refund("ORD-1001", "CUST-001", 89.99, "wrong item"))
print(tools.escalate_to_human("customer wants a human", "customer_requested", "CUST-002"))