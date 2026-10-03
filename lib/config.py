import pathlib
import sys
import yaml
from typing import Dict, Any

DEFAULT_MAX_AGE_DAYS = 30

def load_config(config_path: str = "feeds.yaml") -> Dict[str, Any]:
    """Load and validate the YAML configuration file."""
    path = pathlib.Path(config_path)
    if not path.is_file():
        print(f"Error: Configuration file '{config_path}' not found.", file=sys.stderr)
        sys.exit(1)

    try:
        with open(path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"Error: Failed to parse '{config_path}': {e}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(config, dict):
        print(f"Error: '{config_path}' must be a YAML mapping/dictionary.", file=sys.stderr)
        sys.exit(1)

    if "ntfy_topic" not in config or not config["ntfy_topic"]:
        print("Error: Missing required 'ntfy_topic' in configuration.", file=sys.stderr)
        sys.exit(1)

    if "feeds" not in config or not isinstance(config["feeds"], list):
        print("Error: Missing or invalid 'feeds' list in configuration.", file=sys.stderr)
        sys.exit(1)

    return config