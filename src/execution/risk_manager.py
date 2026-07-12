from dataclasses import dataclass

@dataclass
class RiskConfig:
    max_order_pct: float = 0.1
    max_position_pct: float = 0.3
    max_daily_loss_pct: float = 0.03
    max_leverage: float = 3
    max_consecutive_losses: int = 3
    min_balance: float = 10
    kill_switch: bool = False

class RiskManager:
    def __init__(self, cfg: RiskConfig):
        self.cfg=cfg; self.api_error=False; self.ws_connected=True; self.daily_pnl=0.0; self.consecutive_losses=0
    def validate(self, order_notional: float, equity: float, current_exposure: float=0, leverage: float=1) -> tuple[bool,str]:
        if self.cfg.kill_switch: return False,'kill_switch_on'
        if self.api_error: return False,'api_error_state'
        if not self.ws_connected: return False,'websocket_disconnected'
        if equity < self.cfg.min_balance: return False,'below_min_balance'
        if leverage > self.cfg.max_leverage: return False,'leverage_exceeds_limit'
        if order_notional > equity*self.cfg.max_order_pct: return False,'order_notional_exceeds_limit'
        if current_exposure+order_notional > equity*self.cfg.max_position_pct: return False,'position_exposure_exceeds_limit'
        if self.daily_pnl <= -equity*self.cfg.max_daily_loss_pct: return False,'daily_loss_limit'
        if self.consecutive_losses >= self.cfg.max_consecutive_losses: return False,'consecutive_loss_limit'
        return True,'ok'
