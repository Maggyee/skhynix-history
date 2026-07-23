from pathlib import Path
import yaml

ROOT = Path(__file__).resolve().parents[2]

def load_config():
    with (ROOT / "config.yaml").open() as f:
        return yaml.safe_load(f)

