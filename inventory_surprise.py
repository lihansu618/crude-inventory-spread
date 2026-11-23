"""
Crude inventory surprise -> WTI calendar spread
================================================
Question: when US crude stocks build/draw more than the seasonal norm, does the
WTI front spread (C1 - C2) keep moving in the following days?

Data (all free, from EIA; downloaded automatically, or put the .xls in ./data):
  WCESTUS1                  weekly US commercial crude stocks ex-SPR (kbbl)
  W_EPC0_SAX_YCUOK_MBBL     weekly Cushing, OK crude stocks (kbbl)
  RCLC1, RCLC2              daily NYMEX WTI futures, contract 1 and 2 ($/bbl)

Usage:
  python crude_inventory_spread.py             # real data
  python crude_inventory_spread.py --selftest  # synthetic data with a planted effect

Outputs: results.txt, fig_storage_curve.png, fig_backtest.png
"""
import os
import sys
import math
import numpy as np
import pandas as pd
import statsmodels.api as sm
from pandas.tseries.holiday import USFederalHolidayCalendar, GoodFriday
from pandas.tseries.offsets import CustomBusinessDay

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
DATA_DIR = "data"
EIA_URL = "https://www.eia.gov/dnav/pet/hist_xls/{key}{freq}.xls"
STOCK_SERIES = {"US": "WCESTUS1", "Cushing": "W_EPC0_SAX_YCUOK_MBBL"}
SEASONAL_YEARS = 5        # seasonal norm = same week, prior 5 years
Z_WINDOW = 104            # weeks used to scale the surprise
HORIZONS = [1, 3, 5, 10]  # trading days after the release (20 would always span a roll)
SPLIT_DATE = "2016-01-01" # in-sample / out-of-sample split
COST = 0.01               # $/bbl per unit position, per side (1 tick; 2 ticks round trip)
Z_CAP = 2.0               # cap on position size
EXCLUDE = [("2020-04-01", "2020-05-01")]  # negative-price episode

HOLIDAYS = USFederalHolidayCalendar().holidays("1985-01-01", "2035-12-31")
GOOD_FRIDAYS = pd.DatetimeIndex(
    [GoodFriday.dates(pd.Timestamp(y, 1, 1), pd.Timestamp(y, 12, 31))[0] for y in range(1985, 2036)]
)
BDAY = CustomBusinessDay(holidays=HOLIDAYS.union(GOOD_FRIDAYS))


# ----------------------------------------------------------------------------
# Data loading
# ----------------------------------------------------------------------------
def load_eia(key, freq):
    """Read one EIA 'hist_xls' file -> Series indexed by date."""
    path = os.path.join(DATA_DIR, f"{key}{freq}.xls")
    if not os.path.exists(path):
        import urllib.request
        os.makedirs(DATA_DIR, exist_ok=True)
        print(f"downloading {key}{freq}.xls ...")
        urllib.request.urlretrieve(EIA_URL.format(key=key, freq=freq), path)
    raw = pd.read_excel(path, sheet_name="Data 1", header=None)
    dates = pd.to_datetime(raw[0], errors="coerce")
    vals = pd.to_numeric(raw[1], errors="coerce")
    s = pd.Series(vals.values, index=dates).dropna()
    s = s[s.index.notna()].sort_index()
    s.name = key
    return s


def load_real():
    stocks = {name: load_eia(key, "w") for name, key in STOCK_SERIES.items()}
    c1, c2 = load_eia("RCLC1", "d"), load_eia("RCLC2", "d")
    px = pd.concat([c1, c2], axis=1, keys=["c1", "c2"]).dropna()
    return stocks, px


# ----------------------------------------------------------------------------
# Calendar helpers
# ----------------------------------------------------------------------------
def release_dates(week_ending):
    """EIA publishes Wednesday 10:30 ET for the week ending the prior Friday,
    one day later when a federal holiday falls Monday-Wednesday of that week."""
    out = []
    for f in week_ending:
        wed = f + pd.Timedelta(days=5)
        hol = ((HOLIDAYS >= f + pd.Timedelta(days=3)) & (HOLIDAYS <= wed)).any()
        out.append(wed + pd.Timedelta(days=1) if hol else wed)
    return pd.DatetimeIndex(out)


def cl_expiries(start, end):
    """CL last trade date: 3 business days before the 25th of the month before
    delivery (or before the last business day preceding the 25th)."""
    out = []
    for m in pd.date_range(start, end, freq="MS"):
        d = pd.Timestamp(m.year, m.month, 25)
        if len(pd.bdate_range(d, d, freq=BDAY)) == 0:
            d = d - BDAY
        out.append(d - 3 * BDAY)
    return pd.DatetimeIndex(out)


# ----------------------------------------------------------------------------
# Signal
# ----------------------------------------------------------------------------
def build_signal(stock):
    """All quantities use only data available at the release of week t."""
    df = pd.DataFrame({"stock": stock})
    df["chg"] = df["stock"].diff()
    # seasonal norm: same week (+/- 1) in each of the prior N years
    lags = [52 * k + j for k in range(1, SEASONAL_YEARS + 1) for j in (-1, 0, 1)]
    df["chg_norm"] = pd.concat([df["chg"].shift(l) for l in lags], axis=1).mean(axis=1, skipna=False)
    df["lvl_norm"] = pd.concat(
        [df["stock"].shift(52 * k) for k in range(1, SEASONAL_YEARS + 1)], axis=1
    ).mean(axis=1, skipna=False)
    df["surprise"] = df["chg"] - df["chg_norm"]
    df["z"] = df["surprise"] / df["surprise"].rolling(Z_WINDOW).std().shift(1)
    df["lvl_dev"] = df["stock"] / df["lvl_norm"] - 1.0   # % vs 5-year average
    df["release"] = release_dates(df.index)
    return df.dropna()


def attach_prices(sig, px):
    """For each release: spread at prior close, at entry close (release day) and
    h days later. Windows that span a contract roll are flagged."""
    spread = px["c1"] - px["c2"]              # > 0 = backwardation
    days = px.index
    pos = days.searchsorted(sig["release"].values)   # first trading day >= release
    ok = (pos > 0) & (pos < len(days) - max(HORIZONS))
    sig, pos = sig[ok].copy(), pos[ok]
    sig["entry"] = days[pos]
    sig["spread_entry"] = spread.values[pos]
    sig["d_announce"] = spread.values[pos] - spread.values[pos - 1]
    exp = cl_expiries(days[0] - pd.Timedelta(days=40), days[-1] + pd.Timedelta(days=40))
    for h in HORIZONS:
        sig[f"fwd{h}"] = spread.values[pos + h] - spread.values[pos]
        lo, hi = days[pos - 1], days[pos + h]
        # roll if an expiry falls in [entry - 1d, exit]; rule may be off by a day
        sig[f"roll{h}"] = [((exp >= a) & (exp <= b)).any() for a, b in zip(lo, hi)]
    sig["roll_announce"] = [((exp >= a) & (exp <= b)).any() for a, b in zip(days[pos - 2], days[pos])]
    for a, b in EXCLUDE:
        sig = sig[~((sig["entry"] >= a) & (sig["entry"] < b))]
    return sig


# ----------------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------------
def hac_ols(y, x, lags):
    m = sm.OLS(y, sm.add_constant(x), missing="drop").fit(cov_type="HAC", cov_kwds={"maxlags": lags})
    return m.params.iloc[1], m.tvalues.iloc[1], m.rsquared, int(m.nobs)


def run_tests(sig, log):
    # 1. storage curve: spread level vs inventory level deviation
    b, t, r2, n = hac_ols(sig["spread_entry"], sig["lvl_dev"] * 100, 12)
    log(f"  [1] storage curve   spread = a + b*lvl_dev(%)   b={b:+.4f}  t={t:+.2f}  R2={r2:.3f}  n={n}")
    # 2. announcement-day reaction
    d = sig[~sig["roll_announce"]]
    b, t, r2, n = hac_ols(d["d_announce"], d["z"], 4)
    log(f"  [2] release day     d_spread = a + b*z          b={b:+.4f}  t={t:+.2f}  R2={r2:.3f}  n={n}")
    # 3. post-release drift
    for h in HORIZONS:
        d = sig[~sig[f"roll{h}"]]
        for label, sub in [("full", d), ("IS", d[d["entry"] < SPLIT_DATE]), ("OOS", d[d["entry"] >= SPLIT_DATE])]:
            if len(sub) < 60:
                continue
            b, t, r2, n = hac_ols(sub[f"fwd{h}"], sub["z"], max(4, 2 * math.ceil(h / 5)))
            log(f"  [3] drift h={h:<2d} {label:<4s} fwd = a + b*z              b={b:+.4f}  t={t:+.2f}  R2={r2:.4f}  n={n}")


def backtest(sig, log):
    """Weekly: at release-day close take position -z (capped) in the C1-C2
    spread, hold 5 trading days. Flat in weeks that span a roll."""
    bt = sig[["entry", "z", "fwd5", "roll5"]].copy()
    bt["pos"] = np.where(bt["roll5"], 0.0, -bt["z"].clip(-Z_CAP, Z_CAP))
    bt["gross"] = bt["pos"] * bt["fwd5"]
    bt["cost"] = 2 * COST * bt["pos"].abs()   # open and close every week
    bt["net"] = bt["gross"] - bt["cost"]
    bt = bt.set_index("entry")

    def stats(x):
        if len(x) < 20 or x.std() == 0:
            return "n/a"
        return f"Sharpe={x.mean() / x.std() * np.sqrt(52):+.2f}  mean={x.mean():+.4f}$/bbl/wk  hit={(x[x != 0] > 0).mean():.0%}  n={len(x)}"

    for label, sub in [("full", bt), ("IS ", bt[:SPLIT_DATE]), ("OOS", bt[SPLIT_DATE:])]:
        log(f"  [4] backtest {label} gross: {stats(sub['gross'])}")
        log(f"      backtest {label} net  : {stats(sub['net'])}")
    return bt


def make_figures(results):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, len(results), figsize=(6 * len(results), 4.5), squeeze=False)
    for ax, (name, (sig, bt)) in zip(axes[0], results.items()):
        ax.scatter(sig["lvl_dev"] * 100, sig["spread_entry"], s=6, alpha=0.4)
        ax.set(title=f"Storage curve: {name} stocks", xlabel="Stocks vs 5-yr seasonal average (%)",
               ylabel="C1 - C2 ($/bbl)")
        ax.axhline(0, lw=0.8, color="grey")
    fig.tight_layout(); fig.savefig("fig_storage_curve.png", dpi=150); plt.close(fig)

    fig, ax = plt.subplots(figsize=(9, 4.5))
    for name, (sig, bt) in results.items():
        ax.plot(bt["gross"].cumsum(), label=f"{name} gross")
        ax.plot(bt["net"].cumsum(), ls="--", label=f"{name} net of {2 * COST:.2f}$/bbl round trip")
    ax.axvline(pd.Timestamp(SPLIT_DATE), color="grey", lw=0.8)
    ax.set(title="Cumulative P&L, weekly fade of inventory surprise in C1-C2", ylabel="$/bbl per unit z")
    ax.legend(); fig.tight_layout(); fig.savefig("fig_backtest.png", dpi=150); plt.close(fig)


# ----------------------------------------------------------------------------
# Synthetic data with a planted effect, to check the pipeline end to end
# ----------------------------------------------------------------------------
def load_synthetic(beta=0.03, seed=0):
    rng = np.random.default_rng(seed)
    weeks = pd.date_range("2000-01-07", "2026-11-13", freq="W-FRI")
    woy = weeks.isocalendar().week.values.astype(float)
    shock = rng.normal(0, 4000, len(weeks))
    chg = 3000 * np.cos(2 * np.pi * woy / 52) + shock
    stock = pd.Series(400000 + np.cumsum(chg - chg.mean()), index=weeks)
    days = pd.bdate_range("2000-01-03", "2026-11-20", freq=BDAY)
    rel = release_dates(weeks)
    drift = pd.Series(0.0, index=days)
    for r, s in zip(rel, shock / 4000):
        i = days.searchsorted(r)
        if i + 5 < len(days):
            drift.iloc[i] += -3 * beta * s            # release-day jump
            drift.iloc[i + 1:i + 6] += -beta * s / 5   # planted 5-day drift
    spread = (drift + rng.normal(0, 0.05, len(days))).cumsum()
    spread = spread - spread.ewm(span=250).mean()       # mean-reverting level
    jump = pd.Series(0.0, index=days)
    exp = cl_expiries(days[0], days[-1])
    after = jump.index.isin([e + BDAY for e in exp])
    jump.loc[after] = rng.normal(0, 0.5, after.sum())
    spread = spread + jump                               # roll noise that must be filtered out
    c2 = 60 + rng.normal(0, 1, len(days)).cumsum() * 0.3
    px = pd.DataFrame({"c1": c2 + spread.values, "c2": c2}, index=days)
    return {"US": stock}, px


# ----------------------------------------------------------------------------
def main():
    selftest = "--selftest" in sys.argv
    stocks, px = load_synthetic() if selftest else load_real()
    lines = []

    def log(s=""):
        print(s); lines.append(s)

    log(f"prices {px.index[0].date()} -> {px.index[-1].date()}  ({len(px)} days)" + ("  [SYNTHETIC]" if selftest else ""))
    results = {}
    for name, stock in stocks.items():
        if (stock.index.weekday != 4).mean() > 0.02:
            log(f"WARNING: {name} weekly dates are not Fridays - check the release-date mapping")
        sig = attach_prices(build_signal(stock), px)
        log(f"\n{name} stocks: {len(sig)} weekly observations, {sig['entry'].iloc[0].date()} -> {sig['entry'].iloc[-1].date()}")
        run_tests(sig, log)
        results[name] = (sig, backtest(sig, log))
    if not selftest:
        with open("results.txt", "w") as f:
            f.write("\n".join(lines) + "\n")
    make_figures(results)


if __name__ == "__main__":
    main()