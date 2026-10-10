import argparse
import csv
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--results", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
report = json.loads(args.results.with_name("report.json").read_text(encoding="utf-8"))
with args.results.open(encoding="utf-8-sig", newline="") as handle:
    rows = [
        [float(r["favorite_probability"]), r["correct"] == "True", int(r["league_id"])]
        for r in csv.DictReader(handle)
    ]
template = Path(__file__).resolve().parents[1] / "docs" / "tier1-visual.html"
fragment = template.read_text(encoding="utf-8")
fragment = fragment.replace(
    '<script type="application/json" id="betbot-tier1-data">[]</script>',
    '<script type="application/json" id="betbot-tier1-data">' + json.dumps(rows) + "</script>",
)
meta = {
    "period": report["requested_start"][:10] + " — " + report["requested_end"][:10],
    "names": {str(row["league_id"]): row["name"] for row in report["by_league"]},
}
fragment = fragment.replace(
    '<script type="application/json" id="betbot-tier1-meta">{}</script>',
    '<script type="application/json" id="betbot-tier1-meta">'
    + json.dumps(meta, ensure_ascii=False)
    + "</script>",
)
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(fragment, encoding="utf-8")
print(args.output.resolve())
