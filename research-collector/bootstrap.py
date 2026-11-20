"""Initialize paths for the monorepo and matching deployment archive layout."""
import os
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parent
os.environ.setdefault('ARIS_ROOT', str(ROOT))
sys.path[:0] = [str(ROOT / 'lib'), str(ROOT.parent / 'shared/lib'), str(ROOT.parent / 'contracts')]
