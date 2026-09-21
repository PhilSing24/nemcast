"""Tab 1 — the market. Orientation: who the five regions are and how they differ."""

import json
from pathlib import Path

import plotly.graph_objects as go
import streamlit as st

import data as D

GEOJSON = Path(__file__).resolve().parents[3] / "data" / "reference" / "au_states.geojson"

SELECTED = "#C2650F"
IN_NEM = "#DCDAD2"
OUTSIDE = "#EFEEE9"
EDGE = "#FFFFFF"
TRACK = "#E8E7E1"
GREEN = "#1D9E75"

HEADER_CSS = """
<style>
[data-testid="stDataFrame"] div[role="columnheader"] {
    background-color: #4A4A47 !important;
}
[data-testid="stDataFrame"] div[role="columnheader"] * {
    color: #FFFFFF !important;
    font-weight: 500 !important;
}
</style>
"""

LABEL_AT = {
    "NSW1": (-32.2, 146.5), "QLD1": (-22.5, 144.5), "VIC1": (-36.9, 144.3),
    "SA1": (-30.0, 135.5), "TAS1": (-42.0, 146.6),
    "WA": (-25.5, 122.0), "NT": (-19.5, 133.5),
}
SHORT = {"NSW1": "NSW", "QLD1": "QLD", "VIC1": "VIC", "SA1": "SA", "TAS1": "TAS"}

COMPARE_COLS = {
    "region_id": "Region",
    "name": "Name",
    "population_m": "Population (million)",
    "annual_demand_twh": "Annual demand (TWh)",
    "peak_demand_gw": "Peak demand (GW)",
    "load_factor": "Load factor",
    "renewable_pct": "Renewable (%)",
}


@st.cache_data
def _geo():
    if not GEOJSON.exists():
        return None
    with open(GEOJSON) as fh:
        return json.load(fh)


def _map(selected):
    gj = _geo()
    if gj is None:
        st.warning("Run `python src/fetch_boundaries.py` to download state boundaries.")
        return None

    fig = go.Figure()
    for feat in gj["features"]:
        rid = feat["properties"]["region_id"]
        in_nem = feat["properties"]["in_nem"]
        fill = SELECTED if rid == selected else (IN_NEM if in_nem else OUTSIDE)
        fig.add_trace(go.Choropleth(
            geojson={"type": "FeatureCollection", "features": [feat]},
            locations=[feat["properties"]["state"]],
            featureidkey="properties.state",
            z=[1], colorscale=[[0, fill], [1, fill]], showscale=False,
            marker=dict(line=dict(color=EDGE, width=1.5)),
            hovertemplate=(f"{feat['properties']['state']}<extra></extra>"
                           if in_nem else "<extra></extra>"),
            hoverinfo="skip" if not in_nem else None))

    for key, (lat, lon) in LABEL_AT.items():
        is_sel = key == selected
        fig.add_trace(go.Scattergeo(
            lat=[lat], lon=[lon], mode="text", text=[SHORT.get(key, key)],
            textfont=dict(size=13 if is_sel else 11,
                          color="#FFFFFF" if is_sel else "#8A897F"),
            hoverinfo="skip", showlegend=False))

    fig.update_geos(fitbounds="locations", visible=False,
                    projection_type="mercator", bgcolor="rgba(0,0,0,0)")
    fig.update_layout(height=300, margin=dict(l=0, r=0, t=0, b=0),
                      paper_bgcolor="rgba(0,0,0,0)", showlegend=False)
    return fig


def _renewable_gauge(pct):
    """Single-value donut: green arc for renewables, grey track for the rest."""
    fig = go.Figure(go.Pie(
        values=[pct, 100 - pct], hole=0.70,
        marker=dict(colors=[GREEN, TRACK], line=dict(width=0)),
        sort=False, direction="clockwise", rotation=0,
        textinfo="none", hoverinfo="skip"))
    fig.add_annotation(
        text=(f"<span style='font-size:30px'><b>{pct:.0f}%</b></span>"
              "<br><span style='font-size:12px'>renewable</span>"),
        showarrow=False)
    fig.update_layout(height=230, margin=dict(l=0, r=0, t=0, b=0),
                      showlegend=False, paper_bgcolor="rgba(0,0,0,0)")
    return fig


def _pct_table(df, label, height=None):
    """Descending percentage table with an inline bar."""
    d = (df[df.pct > 0][["label", "pct"]]
         .sort_values("pct", ascending=False)
         .rename(columns={"label": label, "pct": "Share"}))
    st.dataframe(
        d, hide_index=True, use_container_width=True, height=height,
        column_config={
            "Share": st.column_config.ProgressColumn(
                "Share", format="%.1f%%", min_value=0, max_value=100)})


def render():
    st.markdown(HEADER_CSS, unsafe_allow_html=True)

    regions = D.regions_df()
    names = {r.region_id: f"{r.name} ({r.region_id})" for r in regions.itertuples()}

    region_id = st.selectbox("Region", D.REGION_ORDER,
                             format_func=lambda r: names[r],
                             index=D.REGION_ORDER.index("SA1"))
    r = regions[regions.region_id == region_id].iloc[0]

    left, right = st.columns([0.85, 1.15])

    with left:
        fig = _map(region_id)
        if fig:
            st.plotly_chart(fig, use_container_width=True,
                            config={"displayModeBar": False})

    with right:
        a, b = st.columns([0.75, 1])
        a.metric("Population", f"{r.population_m} M")
        with b:
            st.caption("Largest cities")
            cities = D.cities_df()
            c = cities[cities.region_id == region_id].copy()
            c["People"] = c.population.map(lambda v: f"{v:,}")
            st.dataframe(
                c[["city", "People"]].rename(columns={"city": "City"}),
                hide_index=True, use_container_width=True)

        c2, d2 = st.columns(2)
        c2.metric("Annual demand", f"{r.annual_demand_twh} TWh")
        d2.metric("Peak demand", f"{r.peak_demand_gw} GW")

    st.divider()

    lo, ro = st.columns(2)

    with lo:
        st.markdown("### Generation mix")
        t, g = st.columns([1.25, 1])
        with t:
            mix = D.mix_df()
            _pct_table(mix[mix.region_id == region_id], "Fuel", height=250)
        with g:
            st.plotly_chart(_renewable_gauge(r.renewable_pct),
                            use_container_width=True,
                            config={"displayModeBar": False})

    with ro:
        st.markdown("### Consumption by sector")
        sec = D.sectors_df()
        _pct_table(sec[sec.region_id == region_id], "Sector", height=250)

    with st.expander("Compare all regions"):
        cmp = (D.regions_df(include_total=True)[list(COMPARE_COLS)]
               .rename(columns=COMPARE_COLS))
        st.dataframe(
            cmp, hide_index=True, use_container_width=True,
            column_config={
                title: st.column_config.NumberColumn(title, format="%.2f")
                for title in list(COMPARE_COLS.values())[2:]})
