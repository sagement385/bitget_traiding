class OrderManager:
    def __init__(self): self.orders={}
    def create(self, order): self.orders[order.client_oid]={'order':order,'status':'created'}; return order
    def update(self, client_oid, status): self.orders.setdefault(client_oid,{})['status']=status
