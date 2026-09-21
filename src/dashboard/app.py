"""NEMCast dashboard.

    streamlit run src/dashboard/app.py
"""

import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).parent))

st.set_page_config(page_title="NEMCast", page_icon="⚡", layout="wide")

st.title("NEMCast")
st.caption("Wholesale electricity price forecasting for the Australian NEM")

tab_market, tab_prices, tab_drivers, tab_forecast, tab_model = st.tabs(
    ["The market", "Prices", "Drivers", "The forecast", "The model"])

with tab_market:
    from tabs import market
    market.render()

with tab_prices:
    from tabs import prices
    prices.render()

with tab_drivers:
    st.info("Price against margin, wind, demand. Curtailment. Not built yet.")

with tab_forecast:
    st.info("AEMO's benchmark and the false-positive browser. Not built yet.")

with tab_model:
    st.info("Precision-recall against the benchmark. Not built yet.")
