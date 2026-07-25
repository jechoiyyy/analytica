import sys
from pathlib import Path

# scripts/ 는 패키지가 아니라 평면 모듈 묶음이므로(load_data를 직접 import) 경로를 추가한다.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
