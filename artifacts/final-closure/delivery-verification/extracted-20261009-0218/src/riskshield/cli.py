import argparse
import json
import os
import sys
from pathlib import Path

from riskshield.api import PROJECT_ROOT
from riskshield.model_gateway import ModelUnavailable, model_status, probe_model
from riskshield.schemas import CaseImport
from riskshield.store import Store


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="风控盾 Day 1 工具")
    subs = parser.add_subparsers(dest="command", required=True)
    importer = subs.add_parser("import-case", help="校验并幂等导入历史案例包")
    importer.add_argument("path", type=Path)
    subs.add_parser("model-status", help="只检查配置项是否存在，不输出凭据")
    subs.add_parser("probe-model", help="仅在显式启用后发送一次合成内容模型请求")
    args = parser.parse_args()
    try:
        if args.command == "import-case":
            case = CaseImport.model_validate_json(args.path.read_text(encoding="utf-8"))
            result = Store(os.getenv("RISKSHIELD_DB", str(PROJECT_ROOT / "runtime/riskshield.db"))).import_case(case)
        elif args.command == "model-status":
            result = model_status()
        else:
            result = probe_model()
    except ModelUnavailable as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, ensure_ascii=False))
        raise SystemExit(2) from None
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
