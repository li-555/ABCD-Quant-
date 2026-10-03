"""Download checksum-verified Binance monthly spot archives, without credentials.

python -m research.download_factor_data --output cache/factor_history
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import io
import json
from pathlib import Path
import time
import zipfile

import pandas as pd
import requests
import yaml

from bot.data.market_bars import parse_klines


def get(url):
    for attempt in range(3):
        try:
            response = requests.get(url, timeout=30)
            response.raise_for_status()
            return response.content
        except requests.RequestException:
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def download_one(task):
    symbol, month, root = task
    name = f"{symbol}-30m-{month}.zip"
    url = f"https://data.binance.vision/data/spot/monthly/klines/{symbol}/30m/{name}"
    path = root / "archives" / name
    checksum_path = path.with_suffix(".zip.CHECKSUM")
    expected = (checksum_path.read_text() if checksum_path.exists()
                else get(url + ".CHECKSUM").decode()).split()[0]
    content = path.read_bytes() if path.exists() else get(url)
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise ValueError(f"Checksum mismatch: {url}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    checksum_path.write_text(expected + "  " + name)
    with zipfile.ZipFile(io.BytesIO(content)) as z:
        raw = pd.read_csv(z.open(z.namelist()[0]), header=None)
    frame = parse_klines(raw, 30)
    return symbol, frame, {"url": url, "sha256": actual, "rows": len(frame)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="cache/factor_history")
    parser.add_argument("--protocol", default="research/factor_protocol.json")
    args = parser.parse_args()
    protocol = json.loads(Path(args.protocol).read_text())
    root = Path(args.output)
    root.mkdir(parents=True, exist_ok=True)
    universe = yaml.safe_load(Path("bot/config/universe.yaml").read_text())["coins"]
    symbols = [u["binance_symbol"] for u in universe]
    months = pd.period_range(protocol["data_start"],
                             pd.Timestamp(protocol["holdout"][1]) - pd.Timedelta(days=1), freq="M")
    tasks = [(s, str(m), root) for s in symbols for m in months]
    frames = {s: [] for s in symbols}
    manifest = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        for i, (symbol, frame, item) in enumerate(pool.map(download_one, tasks), 1):
            frames[symbol].append(frame)
            manifest.append(item)
            if i % 25 == 0:
                print(f"Verified {i}/{len(tasks)} archives", flush=True)
    audit = []
    for u in universe:
        f = pd.concat(frames[u["binance_symbol"]]).sort_index()
        if f.index.has_duplicates:
            raise ValueError("duplicate bars across archives")
        idx = pd.date_range(pd.Timestamp(protocol["data_start"], tz="UTC") + pd.Timedelta(minutes=30),
                            pd.Timestamp(protocol["holdout"][1], tz="UTC"), freq="30min")
        f = f.reindex(idx)
        missing = int(f.close.isna().sum())
        audit.append({"coin": u["coin"], "rows": len(f), "missing": missing,
                      "coverage": 1 - missing / len(f)})
        if missing:
            raise ValueError(f"Incomplete data for {u['coin']}: {missing} bars; do not silently fill")
        f.to_csv(root / f"{u['coin']}.csv", index_label="close_time")
    (root / "manifest.json").write_text(json.dumps({"archives": manifest, "coverage": audit}, indent=2))
    print(json.dumps(audit, indent=2), flush=True)


if __name__ == "__main__":
    main()
