"""离线验证日频更新。不连接 baostock，不改 cost/zq 的计算公式。"""
import datetime as dt
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DAILY = REPO / "scripts" / "fetch-daily.py"
TODAY = dt.date(2026, 11, 6)


def weekdays(start, end):
    d = start
    out = []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


DATES = weekdays(dt.date(2026, 6, 1), TODAY)


def bar(day, close, turnover=1.0):
    return {
        "date": day.isoformat(),
        "open": close - 0.05,
        "high": close + 0.1,
        "low": close - 0.1,
        "close": close,
        "vol": 1000.0,
        "amount": close * 1000 * 100,
        "turnover": turnover,
    }


def close_of(day):
    return round(10 + (day.toordinal() % 7) * 0.13, 2)


def series(shock=False):
    return [bar(d, close_of(d) + (2 if shock else 0)) for d in DATES]


class Result:
    def __init__(self, rows, error="0", msg=""):
        self._rows = list(rows)
        self._i = 0
        self.error_code = error
        self.error_msg = msg

    def next(self):
        if self._i >= len(self._rows):
            return False
        self._row = self._rows[self._i]
        self._i += 1
        return True

    def get_row_data(self):
        return self._row


class Login:
    def __init__(self, error="0", msg="ok"):
        self.error_code = error
        self.error_msg = msg


class FakeBao:
    def __init__(self):
        self.k_calls = []
        self.mode = {}
        self.fail_left = {}

    def login(self):
        return Login()

    def logout(self):
        return Login()

    def query_all_stock(self, day=None):
        # 生产代码要求列表至少 100 只，否则视为异常并放弃
        rows = [[f"sh.{600000 + i:06d}", "1", f"股票{i}"] for i in range(100)]
        rows += [["sz.000001", "1", "平安银行"], ["sz.000300", "1", "沪深300"]]
        return Result(rows)

    def query_history_k_data_plus(self, code, fields, start_date=None, end_date=None, frequency="d", adjustflag="2"):
        self.k_calls.append({"code": code, "start": start_date, "end": end_date, "adjustflag": adjustflag, "fields": fields})
        if code in ("sh.000001", "sz.399001", "sz.399006"):
            rows = [[d.isoformat(), "3000", "3010", "2990", "3005", "100", "1000"] for d in DATES]
            return Result(rows)
        spec = self.mode.get(code, "bars")
        left = self.fail_left.get(code, 0)
        if left:
            self.fail_left[code] = left - 1
            if spec == "timeout":
                from concurrent.futures import TimeoutError as FutureTimeoutError
                raise FutureTimeoutError()
            raise RuntimeError("transient")
        if spec == "empty":
            return Result([])
        if spec == "error":
            return Result([], error="100", msg="bad")
        # 短窗口和随后的整段重抓都使用新的复权价，旧文件仍是旧价
        shock = spec in ("shock", "shock-then-full")
        bars = series(shock=shock)
        rows = []
        for b in bars:
            if start_date and b["date"] < start_date:
                continue
            if end_date and b["date"] > end_date:
                continue
            rows.append([b["date"], b["open"], b["high"], b["low"], b["close"], b["vol"] * 100, b["amount"], b["turnover"]])
        return Result(rows)


def load_fd(tmp):
    os.chdir(tmp)
    sys.modules["baostock"] = FakeBao()
    name = "fd_under_test_" + str(abs(hash(tmp)))
    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, DAILY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.get_bj_today_orig = mod.get_bj_today
    mod.beijing_now_iso_orig = mod.beijing_now_iso
    mod.get_bj_today = lambda: TODAY
    mod.beijing_now_iso = lambda: TODAY.isoformat() + "T16:00:00"
    return mod, sys.modules["baostock"]


def write_stock(fd, code, bars):
    fd.atomic_write(fd.DATA_DIR / f"{code}.json", fd.dumps({"code": code, "bars": bars}))


class DailyUpdateTests(unittest.TestCase):
    def setUp(self):
        self._old = os.getcwd()
        self.tmp = tempfile.TemporaryDirectory()
        self.fd, self.bs = load_fd(self.tmp.name)
        sys.argv = ["fetch-daily.py"]

    def tearDown(self):
        os.chdir(self._old)
        self.tmp.cleanup()

    def kline_calls(self, code):
        return [c for c in self.bs.k_calls if c["code"] == code]

    def test_schedule_was_not_changed(self):
        text = (REPO / ".github/workflows/update-data.yml").read_text()
        self.assertIn("cron: '30 10 * * 1-5'", text)
        self.assertIn("cron: '30 13 * * 1-5'", text)

    def test_formula_is_stable_for_a_fixed_series(self):
        bars = [bar(d, close_of(d), turnover=0 if d == DATES[3] else 2.5) for d in DATES[:8]]
        ind, _ = self.fd.compute_indicator_series(bars)
        self.assertIsNotNone(ind["cost50"][0])
        self.assertEqual(ind["cost50"][2], ind["cost50"][3])
        again, _ = self.fd.compute_indicator_series(bars)
        self.assertEqual(ind["cost50"], again["cost50"])
        self.assertEqual(ind["zq"], again["zq"])
        self.assertEqual(ind["zq1"], again["zq1"])

    def test_cold_start_writes_stocks_and_keeps_adjustflag(self):
        self.bs.mode["sz.000300"] = "empty"
        code = self.fd.main()
        self.assertEqual(code, 0)
        self.assertTrue((self.fd.DATA_DIR / "600000.json").exists())
        self.assertTrue((self.fd.DATA_DIR / "000001.json").exists())
        self.assertFalse((self.fd.DATA_DIR / "000300.json").exists())
        saved = json.loads((self.fd.DATA_DIR / "600000.json").read_text())
        self.assertEqual(saved["bars"][-1]["date"], TODAY.isoformat())
        self.assertIsNotNone(saved["bars"][-1]["cost50"])
        self.assertIsNotNone(saved["bars"][-1]["zq"])
        self.assertTrue(all(c["adjustflag"] == "2" for c in self.kline_calls("sh.600000")))
        self.assertEqual(len(self.kline_calls("sz.000300")), 1)

    def test_clean_empty_is_not_retried_and_second_run_skips_it(self):
        self.bs.mode["sz.000300"] = "empty"
        self.assertEqual(self.fd.main(), 0)
        self.assertEqual(len(self.kline_calls("sz.000300")), 1)
        ck = json.loads(self.fd.CHECKPOINT_FILE.read_text())
        self.assertEqual(ck["updated"][:10], TODAY.isoformat())
        self.assertIn("000300", ck["empty"])
        before = len(self.bs.k_calls)
        self.assertEqual(self.fd.main(), 0)
        self.assertEqual(len(self.kline_calls("sz.000300")), 1)
        self.assertGreater(len(self.bs.k_calls), before)

    def test_already_latest_does_not_fetch(self):
        write_stock(self.fd, "600000", series())
        write_stock(self.fd, "000001", series())
        self.bs.mode["sz.000300"] = "empty"
        self.assertEqual(self.fd.main(), 0)
        self.assertEqual(self.kline_calls("sh.600000"), [])
        self.assertEqual(self.kline_calls("sz.000001"), [])
        self.assertEqual(len(self.kline_calls("sz.000300")), 1)

    def test_recent_gap_is_refilled(self):
        gap = DATES[-5]
        bars = [b for b in series() if b["date"] != gap.isoformat()]
        write_stock(self.fd, "600000", bars)
        write_stock(self.fd, "000001", series())
        self.bs.mode["sz.000300"] = "empty"
        self.assertEqual(self.fd.main(), 0)
        saved = json.loads((self.fd.DATA_DIR / "600000.json").read_text())
        self.assertIn(gap.isoformat(), [b["date"] for b in saved["bars"]])
        self.assertGreaterEqual(len(self.kline_calls("sh.600000")), 1)

    def test_plus_one_day_appends_without_reseeding(self):
        old = [b for b in series() if b["date"] < TODAY.isoformat()]
        write_stock(self.fd, "600000", old)
        write_stock(self.fd, "000001", series())
        self.bs.mode["sz.000300"] = "empty"
        self.assertEqual(self.fd.main(), 0)
        saved = json.loads((self.fd.DATA_DIR / "600000.json").read_text())
        self.assertEqual(saved["bars"][-1]["date"], TODAY.isoformat())
        self.assertEqual(saved["bars"][-1]["close"], close_of(TODAY))
        self.assertEqual(len(self.kline_calls("sh.600000")), 1)
        meta = json.loads(self.fd.META_FILE.read_text())
        self.assertEqual(meta["reseeded"], 0)

    def test_ex_rights_replaces_whole_series(self):
        # 最后一天还没补上，才会去请求；重叠日价格对不上就整段替换
        old = [b for b in series() if b["date"] < TODAY.isoformat()]
        write_stock(self.fd, "600000", old)
        write_stock(self.fd, "000001", series())
        self.bs.mode["sh.600000"] = "shock-then-full"
        self.bs.mode["sz.000300"] = "empty"
        self.assertEqual(self.fd.main(), 0)
        saved = json.loads((self.fd.DATA_DIR / "600000.json").read_text())
        closes = [b["close"] for b in saved["bars"]]
        self.assertTrue(all(abs(c - (close_of(dt.date.fromisoformat(b["date"])) + 2)) < 1e-9 for b, c in zip(saved["bars"], closes)))
        self.assertGreaterEqual(len(self.kline_calls("sh.600000")), 2)
        self.assertIn("600000", json.loads(self.fd.META_FILE.read_text())["reseededCodes"])

    def test_timeout_retries_then_succeeds(self):
        self.bs.mode["sh.600000"] = "timeout"
        self.bs.fail_left["sh.600000"] = 2
        self.bs.mode["sz.000300"] = "empty"
        write_stock(self.fd, "000001", series())
        self.assertEqual(self.fd.main(), 0)
        self.assertTrue((self.fd.DATA_DIR / "600000.json").exists())
        self.assertEqual(len(self.kline_calls("sh.600000")), 3)

    def test_login_failure_writes_nothing(self):
        self.bs.login = lambda: Login("1", "down")
        self.assertEqual(self.fd.main(), 1)
        self.assertEqual(list(self.fd.DATA_DIR.glob("*.json")), [])

    def test_empty_code_list_writes_nothing(self):
        self.bs.query_all_stock = lambda day=None: Result([["sz.999999", "0", "无"]])
        # 最新交易日也靠这个列表；给满 10 行但没有可交易代码
        rows = [["sz.99999%d" % i, "0", "无"] for i in range(12)]
        self.bs.query_all_stock = lambda day=None: Result(rows)
        self.assertEqual(self.fd.main(), 1)
        self.assertEqual(list(self.fd.DATA_DIR.glob("*.json")), [])

    def test_checkpoint_date_follows_beijing_not_runner_clock(self):
        self.assertEqual(self.fd.beijing_now_iso_orig()[:10], self.fd.get_bj_today_orig().isoformat())
        self.fd.get_bj_today = lambda: dt.date(2026, 11, 7)
        self.fd.beijing_now_iso = lambda: "2026-11-07T22:00:00"
        self.bs.mode["sz.000300"] = "empty"
        self.assertEqual(self.fd.main(), 0)
        ck = json.loads(self.fd.CHECKPOINT_FILE.read_text())
        self.assertEqual(ck["updated"][:10], "2026-11-07")
        out = []
        real_print = print
        def capture(*a, **k):
            out.append(" ".join(str(x) for x in a))
        import builtins
        builtins.print = capture
        try:
            self.assertEqual(self.fd.main(), 0)
        finally:
            builtins.print = real_print
        text = "\n".join(out)
        self.assertIn("checkpoint loaded", text)
        self.assertNotIn("清空", text)


if __name__ == "__main__":
    unittest.main()
