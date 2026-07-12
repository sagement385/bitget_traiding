from src.execution.broker_base import Broker

class PaperBroker(Broker):
    def __init__(self): self.orders=[]
    def place_order(self, order):
        row=order.__dict__.copy(); row['status']='filled'; self.orders.append(row); return row
    def cancel_order(self, client_oid):
        for o in self.orders:
            if o['client_oid']==client_oid: o['status']='canceled'; return o
        return {'client_oid':client_oid,'status':'unknown'}
