#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from xlsx_model_catalogue import read_attribute_models


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("inputs", nargs="+")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows = read_attribute_models(args.inputs)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"catalogue": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"models": len(rows), "brands": len({row['brand'] for row in rows}),
                      "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
