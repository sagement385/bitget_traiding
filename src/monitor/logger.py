import logging
from pathlib import Path

def setup_logger(name='trading_bot', log_dir='logs'):
    Path(log_dir).mkdir(exist_ok=True)
    logger=logging.getLogger(name); logger.setLevel(logging.INFO)
    if not logger.handlers:
        h=logging.FileHandler(Path(log_dir)/'app.log'); h.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(h); logger.addHandler(logging.StreamHandler())
    return logger
