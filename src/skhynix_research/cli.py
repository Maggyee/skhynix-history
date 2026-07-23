from __future__ import annotations
import argparse, logging, sys
from pathlib import Path
import pandas as pd
from .config import ROOT, load_config
from .calendar import parse_utc
from .download import discover_all, download_all
from .reporting import analyze_all, generate_reports
from .history_audit import audit_history_coverage

def setup():
    for p in ["data/raw","data/normalized","reports/charts","logs"]:(ROOT/p).mkdir(parents=True,exist_ok=True)
    fmt=logging.Formatter("%(asctime)sZ %(levelname)s %(message)s")
    root=logging.getLogger();root.setLevel(logging.INFO)
    if not root.handlers:
        sh=logging.StreamHandler();sh.setFormatter(fmt);root.addHandler(sh)
        for name,file in [("download","download.log"),("analysis","analysis.log")]:
            fh=logging.FileHandler(ROOT/"logs"/file);fh.setFormatter(fmt);logging.getLogger(name).addHandler(fh)

def end_value(s): return pd.Timestamp.now(tz="UTC").floor("s") if s=="now" else parse_utc(s)

def main(argv=None):
    setup(); ap=argparse.ArgumentParser(); sub=ap.add_subparsers(dest="cmd",required=True)
    sub.add_parser("discover")
    d=sub.add_parser("download");d.add_argument("--start",default=load_config()["start"]);d.add_argument("--end",default="now")
    a=sub.add_parser("analyze");a.add_argument("--start",default=load_config()["start"]);a.add_argument("--end",default="now")
    r=sub.add_parser("report");r.add_argument("--start",default=load_config()["start"]);r.add_argument("--end",default="now")
    q=sub.add_parser("quick");q.add_argument("--start",default=load_config()["start"]);q.add_argument("--end",default="now")
    ah=sub.add_parser("audit-history");ah.add_argument("--start",default=load_config()["start"]);ah.add_argument("--end",default="now")
    args=ap.parse_args(argv)
    if args.cmd=="discover":
        m,e=discover_all();print(m[["exchange","resolved_symbol","status","listing_time","contract_type"]].to_string(index=False));return 0 if (m.status!="failed").any() else 1
    start=parse_utc(args.start); end=end_value(args.end)
    if args.cmd=="audit-history":
        print(audit_history_coverage(start,end));return 0
    if args.cmd=="download":
        p,f,e=download_all(start,end);print(f"prices={len(p):,} funding={len(f):,} errors={e}");return 0 if len(p) else 1
    if args.cmd=="analyze": analyze_all(start,end);print("analysis outputs: reports/");return 0
    if args.cmd=="report": print(generate_reports(start,end));return 0
    # Audit/backfill probes are cached and keep lower-frequency evidence separate
    # from the primary 1m dataset.
    audit_history_coverage(start,end)
    p,f,e=download_all(start,end)
    if not len(p): print("没有任何可用价格数据",file=sys.stderr);return 1
    coverage,ps,agg,fs,ev,cv,op,ctx=analyze_all(start,end)
    summary=generate_reports(start,end,e)
    print("\n报告文件：",ROOT/"reports"/"EXECUTIVE_SUMMARY.md",ROOT/"reports"/"quick_report.html",sep="\n")
    # Terminal's five most useful conclusions are the first numbered summary items.
    for line in [x for x in summary.splitlines() if x[:2] in {"1.","3.","4.","8.","11"}][:5]:print(line)
    return 0

if __name__=="__main__": raise SystemExit(main())
