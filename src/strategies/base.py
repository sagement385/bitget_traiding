from abc import ABC, abstractmethod
from src.models import Signal

class Strategy(ABC):
    name='base'
    @abstractmethod
    def generate(self, candles, portfolio=None, position=None) -> Signal: ...
