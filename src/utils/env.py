import os
from pathlib import Path

def load_dotenv(path: str = '.env') -> None:
    p=Path(path)
    if not p.exists(): return
    for line in p.read_text().splitlines():
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line: continue
        k,v=line.split('=',1)
        os.environ.setdefault(k.strip(), v.strip())
