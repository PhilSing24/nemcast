"""Tab 2 — prices. Actual 5-minute RRP for one trading day, one or more regions."""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import data as D

COLOUR = {
    "NSW1": "#378ADD",
    "QLD1": "#C2650F",
    "VIC1": "#7F77DD",
    "SA1": "#1D9E75",
    "TAS1": "#D4537E",
}
CAP = "#A32D2D"
ZERO = "#8A897F"

TRADING_OFFSET = pd.Timedelta(hours=4, minutes=5)


@st.cache_data
def _actual(region):
    df = D.prices(region).copy()
    df["trading_day"] = (df.SETTLEMENTDATE - TRADING_OFFSET).dt.normalize()
    return df


def _hours_from_start(ts, day):
    return (ts - (pd.Timestamp(day) + TRADING_OFFSET)).dt.total_seconds() / 3600


def render():
    regions = D.regions_df()
    names = {r.region_id: f"{r.name} ({r.region_id})" for r in regions.itertuples()}

    c1, c2, c3 = st.columns([1.6, 1, 0.9])

    picked = c1.multiselect(
        "Regions", D.REGION_ORDER, default=["SA1"],
        format_func=lambda r: names[r], key="px_regions")

    ref = _actual("SA1")
    days = ref.trading_day.dt.date
    lo, hi = days.min(), days.max()
    default_day = pd.Timestamp("2024-02-15").date()
    if not (lo <= default_day <= hi):
        default_day = hi

    day = c2.date_input("Trading day", value=default_day,
                        min_value=lo, max_value=hi, key="px_day")
    log_scale = c3.checkbox("Log scale", value=False,
                            help="Only available when every price is positive.")

    if not picked:
        st.info("Pick at least one region.")
        return

    day_ts = pd.Timestamp(day)
    start = day_ts + TRADING_OFFSET
    end = day_ts + pd.Timedelta(days=1, hours=4)
    st.markdown(f"Trading day **{start:%a %d %b %Y, %H:%M}** → "
                f"**{end:%a %d %b, %H:%M}**")

    frames = {}
    for r in picked:
        d = _actual(r)
        d = d[d.trading_day == day_ts].sort_values("SETTLEMENTDATE")
        if not d.empty:
            frames[r] = d

    if not frames:
        st.warning("No dispatch data for that trading day.")
        return

    fig = go.Figure()
    for r, d in frames.items():
        fig.add_trace(go.Scatter(
            x=_hours_from_start(d.SETTLEMENTDATE, day_ts), y=d.RRP,
            mode="lines", name=r,
            line=dict(color=COLOUR[r], width=1.5, shape="hv"),
            hovertemplate="%{y:$,.2f}<extra>" + r + "</extra>"))

    all_min = min(d.RRP.min() for d in frames.values())
    use_log = log_scale and all_min > 0

    fig.add_hline(y=0, line=dict(color=ZERO, width=0.8, dash="dot"))
    fig.add_hline(y=300, line=dict(color=CAP, width=0.8, dash="dash"),
                  annotation_text="$300 cap strike",
                  annotation_position="top left",
                  annotation_font=dict(size=11, color=CAP))

    ticks = list(range(0, 25, 3))
    fig.update_layout(
        height=430, margin=dict(l=0, r=0, t=10, b=0),
        yaxis=dict(title="$/MWh", type="log" if use_log else "linear",
                   zeroline=False),
        xaxis=dict(tickmode="array", tickvals=ticks,
                   ticktext=[f"{(4 + t) % 24:02d}:00" for t in ticks],
                   range=[0, 24]),
        legend=dict(orientation="h", y=1.12, x=0),
        hovermode="x unified",
        plot_bgcolor="rgba(0,0,0,0)", paper_bgcolor="rgba(0,0,0,0)")
    fig.update_xaxes(gridcolor="#EEEDE8")
    fig.update_yaxes(gridcolor="#EEEDE8")

    st.plotly_chart(fig, use_container_width=True,
                    config={"displayModeBar": False})

    if log_scale and not use_log:
        st.caption(f"Log scale off — the day contains negative prices "
                   f"(minimum ${all_min:,.0f}).")

    stats = pd.DataFrame([{
        "Region": r,
        "Min": d.RRP.min(),
        "Mean": d.RRP.mean(),
        "Median": d.RRP.median(),
        "Max": d.RRP.max(),
        "Over $300": int((d.RRP > 300).sum()),
        "Negative": int((d.RRP < 0).sum()),
        "Intervals": len(d),
    } for r, d in frames.items()])

    st.dataframe(
        stats, hide_index=True, use_container_width=True,
        column_config={c: st.column_config.NumberColumn(c, format="$%.2f")
                       for c in ("Min", "Mean", "Median", "Max")})
