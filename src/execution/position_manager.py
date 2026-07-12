from src.models import Position
class PositionManager:
    def __init__(self): self.positions={}
    def get(self,symbol): return self.positions.get(symbol, Position(symbol=symbol))
    def set(self,pos): self.positions[pos.symbol]=pos
    def sync_or_halt(self, internal, external):
        if internal.side != external.side or abs(internal.qty-external.qty)>1e-9:
            raise RuntimeError('position_mismatch')
