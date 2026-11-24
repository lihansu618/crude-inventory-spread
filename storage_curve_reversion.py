"""
Extension: does the WTI front spread revert to its inventory-implied level?
===========================================================================
inventory_surprise.py found that the *level* of C1 - C2 is well explained by
inventories, but that inventory *surprises* do not predict the spread.
Here the signal is the gap between the actual spread and the level implied by
a storage curve fitted on past data only:

    spread_pct_t = f(inventory deviation_t) + residual_t

Question 1: does the residual predict the next 5-10 days' spread change?
Question 2: does it beat a benchmark that ignores inventories (spread minus
            its own trailing mean)? Any stationary series mean-reverts, so the
            inventory model only earns its keep if it adds to that.
Question 3: the continuous C1 - C2 series does revert. Is that tradeable, or
            does it happen only when the series switches contracts?

All four model variants (linear/quadratic x expanding/rolling window) are
reported; none was selected after seeing results.

Usage:  python storage_curve_reversion.py     (needs ./data from inventory_surprise.py)
Outputs: results_reversion.txt, fig_reversion.png
"""
import numpy as np
import pandas as pd
import statsmodels.api as sm
from inventory_surprise import (load_real, release_dates, cl_expiries, hac_ols,
                                SEASONAL_YEARS, SPLIT_DATE, COST, Z_CAP, Z_WINDOW, EXCLUDE)

HORIZONS = [5, 10]
MIN_OBS = 156          # weeks of history before the first fit
ROLL_WINDOW = 260      # weeks, for the rolling variants
VARIANTS = [("quad", "rolling"), ("quad", "expanding"), ("linear", "rolling"), ("linear", "expanding")]
HEADLINE = ("quad", "rolling")
CRISES = [("2008-07-01", "2009-07-01"), ("2020-01-01", "2021-01-01")]


def build_frame(stock, px):
    """One row per weekly report, measured at the close of the release day."""
    df = pd.DataFrame({"stock": stock})
    norm = pd.concat([df["stock"].shift(52 * k) for k in range(1, SEASONAL_YEARS + 1)], axis=1).mean(axis=1, skipna=False)
    df["dev"] = (df["stock"] / norm - 1.0) * 100            # % vs 5-year seasonal average
    df = df.dropna()
    days, spread = px.index, (px["c1"] - px["c2"]).values
    pos = days.searchsorted(release_dates(df.index).values)
    ok = (pos > 0) & (pos < len(days) - max(HORIZONS))
    df, pos = df[ok].copy(), pos[ok]
    df["entry"] = days[pos]
    df["c2"] = px["c2"].values[pos]
    df["s_pct"] = spread[pos] / df["c2"] * 100               # spread as % of price
    exp = cl_expiries(days[0] - pd.Timedelta(days=40), days[-1] + pd.Timedelta(days=40))
    for h in HORIZONS:
        df[f"fwd{h}"] = (spread[pos + h] - spread[pos]) / df["c2"] * 100   # P&L of +1 spread, % of price
        df[f"roll{h}"] = [((exp >= a) & (exp <= b)).any() for a, b in zip(days[pos - 1], days[pos + h])]
    # change in the *continuous* C1-C2 series to the next report, rolls included (not a P&L)
    df["naive"] = (np.append(spread[pos][1:], np.nan) - spread[pos]) / df["c2"] * 100
    for a, b in EXCLUDE:
        df = df[~((df["entry"] >= a) & (df["entry"] < b))]
    return df.dropna(subset=["naive"]).reset_index(drop=True)


def add_signals(df, model, window):
    """Walk-forward: the curve for week t is fitted on weeks strictly before t."""
    d, y = df["dev"].values, df["s_pct"].values
    X = np.column_stack([np.ones(len(d)), d] + ([d ** 2] if model == "quad" else []))
    resid, own = np.full(len(d), np.nan), np.full(len(d), np.nan)
    for t in range(MIN_OBS, len(d)):
        lo = max(0, t - ROLL_WINDOW) if window == "rolling" else 0
        beta = np.linalg.lstsq(X[lo:t], y[lo:t], rcond=None)[0]
        resid[t] = y[t] - X[t] @ beta          # actual minus inventory-implied spread
        own[t] = y[t] - y[lo:t].mean()         # benchmark: actual minus its own past mean
    out = df.copy()
    for name, v in [("z_inv", resid), ("z_own", own)]:
        s = pd.Series(v)
        out[name] = s / s.rolling(Z_WINDOW).std().shift(1)
    return out.dropna(subset=["z_inv", "z_own"]).reset_index(drop=True)


def sharpe(x):
    return np.nan if len(x) < 20 or x.std() == 0 else x.mean() / x.std() * np.sqrt(52)


def backtest(df, col):
    """Weekly: position -z (capped) held 5 days; flat when the window spans a roll."""
    pos = np.where(df["roll5"], 0.0, -df[col].clip(-Z_CAP, Z_CAP))
    gross = pd.Series(pos * df["fwd5"].values, index=pd.DatetimeIndex(df["entry"]))
    cost = 2 * COST / df["c2"].values * 100 * np.abs(pos)   # open and close each week
    return gross, gross - cost


def report(df, log):
    for h in HORIZONS:
        d = df[~df[f"roll{h}"]]
        lags = 4 if h == 5 else 8
        for label, sub in [("full", d), ("IS", d[d["entry"] < SPLIT_DATE]), ("OOS", d[d["entry"] >= SPLIT_DATE])]:
            if len(sub) < 60:
                continue
            b, t, r2, n = hac_ols(sub[f"fwd{h}"], sub["z_inv"], lags)
            bo, to, r2o, _ = hac_ols(sub[f"fwd{h}"], sub["z_own"], lags)
            m = sm.OLS(sub[f"fwd{h}"], sm.add_constant(sub[["z_inv", "z_own"]])).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
            log(f"    h={h:<2d} {label:<4s} n={n:<4d} inv: b={b:+.3f} t={t:+.2f} R2={r2:.3f} | own: b={bo:+.3f} t={to:+.2f} R2={r2o:.3f}"
                f" | joint t: inv={m.tvalues['z_inv']:+.2f} own={m.tvalues['z_own']:+.2f}")
    # robustness: drop the two storage crises, which dominate the 10-day estimates
    calm = ~pd.concat([(df["entry"] >= a) & (df["entry"] < b) for a, b in CRISES], axis=1).any(axis=1)
    for h in HORIZONS:
        d = df[calm & ~df[f"roll{h}"]]
        b, t, r2, n = hac_ols(d[f"fwd{h}"], d["z_inv"], 4 if h == 5 else 8)
        log(f"    h={h:<2d} ex crises n={n:<4d} inv: b={b:+.3f} t={t:+.2f} R2={r2:.3f}")
    # where does the continuous series revert? weeks with vs without a contract roll
    for label, sub in [("no roll in week", df[calm & ~df["roll5"]]), ("roll in week", df[calm & df["roll5"]])]:
        b, t, r2, n = hac_ols(sub["naive"], sub["z_inv"], 4)
        log(f"    continuous-series change, {label:<15s} n={n:<4d} b={b:+.3f} t={t:+.2f} R2={r2:.3f}")
    naive = pd.Series(-df["z_inv"].clip(-Z_CAP, Z_CAP).values * df["naive"].values, index=pd.DatetimeIndex(df["entry"]))
    log(f"    NAIVE backtest on the continuous series (rolls treated as P&L): gross Sharpe {sharpe(naive):+.2f}"
        f"  IS {sharpe(naive[:SPLIT_DATE]):+.2f}  OOS {sharpe(naive[SPLIT_DATE:]):+.2f}")
    res = {"naive": naive}
    for col in ["z_inv", "z_own"]:
        g, n = backtest(df, col)
        res[col] = (g, n)
        top = g.abs().sort_values(ascending=False).index[:10]
        log(f"    backtest {col[2:]}: Sharpe gross/net  full {sharpe(g):+.2f}/{sharpe(n):+.2f}"
            f"  IS {sharpe(g[:SPLIT_DATE]):+.2f}/{sharpe(n[:SPLIT_DATE]):+.2f}"
            f"  OOS {sharpe(g[SPLIT_DATE:]):+.2f}/{sharpe(n[SPLIT_DATE:]):+.2f}"
            f"  | net ex 10 largest weeks {sharpe(n.drop(top)):+.2f}")
    return res


def main():
    stocks, px = load_real()
    lines, curves = [], {}

    def log(s=""):
        print(s); lines.append(s)

    log(f"prices {px.index[0].date()} -> {px.index[-1].date()};  fwd and P&L in % of C2 price; z_inv/z_own correlation shown per variant")
    for name, stock in stocks.items():
        base = build_frame(stock, px)
        for model, window in VARIANTS:
            df = add_signals(base, model, window)
            log(f"\n{name} stocks | {model} curve, {window} window | {df['entry'].iloc[0].date()} -> {df['entry'].iloc[-1].date()}"
                f" | corr(z_inv, z_own)={df['z_inv'].corr(df['z_own']):.2f}")
            res = report(df, log)
            if (model, window) == HEADLINE:
                curves[name] = res
    with open("results_reversion.txt", "w") as f:
        f.write("\n".join(lines) + "\n")

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(1, len(curves), figsize=(6.5 * len(curves), 4.5), squeeze=False)
    for ax, (name, res) in zip(axes[0], curves.items()):
        ax.plot(res["naive"].cumsum(), ls=":", label="Naive: continuous series, rolls counted as P&L")
        ax.plot(res["z_inv"][1].cumsum(), label="Inventory-implied residual, roll weeks flat (net)")
        ax.plot(res["z_own"][1].cumsum(), label="Own-mean benchmark, roll weeks flat (net)")
        ax.axvline(pd.Timestamp(SPLIT_DATE), color="grey", lw=0.8)
        ax.axhline(0, color="grey", lw=0.8)
        ax.set(title=f"{name} stocks: fade the spread's deviation", ylabel="Cumulative P&L (% of price, per unit z)")
        ax.legend(fontsize=8)
    fig.tight_layout(); fig.savefig("fig_reversion.png", dpi=150)


if __name__ == "__main__":
    main()
