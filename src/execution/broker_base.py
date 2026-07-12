from abc import ABC, abstractmethod
from src.models import OrderRequest

class Broker(ABC):
    @abstractmethod
    def place_order(self, order: OrderRequest): ...
    @abstractmethod
    def cancel_order(self, client_oid: str): ...
