"""独立境外语料模块的路径入口。只依赖本包根目录，不依赖原服务器。"""
from pathlib import Path
import os

ROOT = Path(os.environ.get("ARIS_ROOT", str(Path.cwd()))).resolve()
CONFIG = ROOT / "config"
DATA = ROOT / "data"
RAW = Path(os.environ.get("ARIS_RAW_ROOT", str(DATA / "raw"))).resolve()
RAW_WEB = RAW / "web"
LOGS = ROOT / "logs"
STATE = ROOT / "state"
SOURCE_STATE = Path(os.environ.get("ARIS_SOURCE_STATE_ROOT", str(STATE))).resolve()
OUTPUTS = ROOT / "outputs"


def ensure(*paths: Path) -> None:
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


if __name__ == "__main__":
    print(f"ARIS_ROOT={ROOT}")
    for path in (CONFIG, RAW_WEB, STATE, LOGS):
        print(f"{path}: {'OK' if path.exists() else 'MISSING'}")

CONTRACTS = ROOT.parent / "contracts"
