"""Read-only CLI: inspect an operator-supplied Gaia result workbook."""

import argparse
import json
from pathlib import Path

from app.gaia_listing.application.result_diagnostics import diagnose_gaia_result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("batch_id", help="本地盖亚批次 ID")
    parser.add_argument("result_xlsx", type=Path, help="运营下载的盖亚结果表")
    parser.add_argument("--data-root", type=Path, default=Path("panel_data/gaia_listing"))
    args = parser.parse_args()
    if not args.batch_id or any(c not in "0123456789abcdef" for c in args.batch_id.lower()):
        parser.error("批次 ID 格式无效")
    batch_path = args.data_root / "batches" / (args.batch_id + ".json")
    batch = json.loads(batch_path.read_text(encoding="utf-8"))
    if batch.get("id") != args.batch_id:
        parser.error("批次记录 ID 不一致")
    mapping_id = str(batch.get("mapping_version_id") or "")
    sidecar = args.data_root / "imports" / "model_mappings" / (mapping_id + ".goods.json")
    metadata = args.data_root / "imports" / "model_mappings" / (mapping_id + ".json")
    goods_by_sku = None
    if sidecar.is_file() and metadata.is_file():
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        mapping = json.loads(metadata.read_text(encoding="utf-8"))
        if (payload.get("quality_coverage_version") == 2
                and payload.get("source_sha256") == mapping.get("sha256")
                and mapping.get("id") == mapping_id):
            goods_by_sku = payload.get("goods_by_sku")
    report = diagnose_gaia_result(
        batch=batch, result_path=args.result_xlsx, goods_by_sku=goods_by_sku)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
