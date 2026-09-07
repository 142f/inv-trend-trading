"""时间边界、不可变记录、无外层结果的参数选择。"""
from __future__ import annotations
from dataclasses import dataclass, asdict
import hashlib
import json
from pathlib import Path
import numpy as np
import pandas as pd


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False, default=str).encode()).hexdigest()


def immutable_json(path, value):
    path = Path(path)
    payload = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False, default=str).encode()
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_EXCL prevents overwrite/races; no mutable "latest" can rewrite a frozen choice.
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    return hashlib.sha256(payload).hexdigest()

@dataclass(frozen=True)
class WalkForwardWindow:
    window_id: str
    train_start: str
    train_end: str
    test_start: str
    test_end_exclusive: str

    def __post_init__(self):
        a, b, c, d = map(pd.Timestamp, (self.train_start, self.train_end,
            self.test_start, self.test_end_exclusive))
        if not a < b < c < d:
            raise ValueError("滚动时间顺序必须为训练起点<训练终点<样本外起点<样本外终点")

    def as_dict(self):
        return asdict(self)


def make_windows(config):
    windows = []
    start = pd.Timestamp(config["first_test_start"], tz="UTC")
    limit = pd.Timestamp(config["last_test_end_exclusive"], tz="UTC")
    for key in ("train_years", "test_months", "gap_calendar_days", "inner_slices"):
        if type(config[key]) is not int or config[key] <= 0:
            raise ValueError(f"{key}必须为正整数")
    while start < limit:
        end = min(start + pd.DateOffset(months=config["test_months"]), limit)
        train_end = start - pd.Timedelta(days=config["gap_calendar_days"])
        train_start = train_end - pd.DateOffset(years=config["train_years"])
        windows.append(WalkForwardWindow(f"窗口{len(windows)+1:02d}",train_start.isoformat(),
            train_end.isoformat(),start.isoformat(),end.isoformat()))
        start = end
    return windows


def choose_from_training(records, allowed_ids, policy):
    """只接受训练摘要，不接受外层收益对象；未达门槛时现金，不强选赢家。"""
    ranked = []
    for rank, cid in enumerate(allowed_ids):
        row = records[cid]
        if row.get("scope") != "TRAIN_ONLY":
            raise ValueError("只允许训练窗口记录参与参数选择")
        scores = row["slices"]
        sr = np.array([x["sharpe_ratio"] if x["sharpe_ratio"] is not None else -10 for x in scores])
        ca = np.array([x["calmar_ratio"] if x["calmar_ratio"] is not None else -10 for x in scores])
        positive = np.mean([x["total_return"] > 0 for x in scores])
        dd = max(x["max_drawdown"] for x in scores)
        count = sum(x["trade_count"] for x in scores)
        value = float(np.median(sr) + .5 * np.median(ca) - np.std(sr) - dd)
        eligible = (positive >= policy["min_positive_inner_ratio"] and dd <= policy["max_inner_drawdown"]
            and count >= policy["min_trades"] and np.median(sr) > 0)
        ranked.append(dict(candidate_id=cid, score=value, eligible=bool(eligible),
            positive_inner_ratio=float(positive), inner_drawdown=float(dd), trade_count=count,
            complexity_rank=rank))
    good = [x for x in ranked if x["eligible"]]
    best = min(good, key=lambda x: (-x["score"], x["complexity_rank"],x["candidate_id"])) if good else None
    return (best["candidate_id"] if best else "现金"), ranked
