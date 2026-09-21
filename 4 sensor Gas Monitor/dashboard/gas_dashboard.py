"""
Multi-Sensor Gas Dashboard — Streamlit
=======================================
Supports two CO₂ sensors simultaneously on independent COM ports.

┌─────────────────────────────────────────────────────────┐
│  Tab 1 │ STC3x   │ CO₂ concentration 0–25 % + Temp °C  │
│  Tab 2 │ MG-811  │ Sensor voltage → estimated CO₂ ppm  │
│  Tab 3 │ Export  │ Excel / CSV download                 │
└─────────────────────────────────────────────────────────┘

Expected serial formats
-----------------------
STC3x  (115200 baud, ~1 reading/s):
    CO2 Concentration: 0.040 %
    Temperature: 25.00 C
    ---

MG-811 (115200 baud, ~1 reading/8 s):
    Average Sensor Voltage (ZERO POINT): 2.345 V

Run:
    pip install streamlit plotly pandas numpy pyserial openpyxl
    streamlit run gas_dashboard.py
"""

import streamlit as st
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd
import numpy as np
import random, time, re, io, math
from datetime import datetime

try:
    import serial
    import serial.tools.list_ports
    SERIAL_AVAILABLE = True
except ImportError:
    SERIAL_AVAILABLE = False

# ─────────────────────────────────────────────────────────────────────────────
# Page config
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Gas Sensor Dashboard",
    page_icon="🌫️",
    layout="wide",
)

# ─────────────────────────────────────────────────────────────────────────────
# Safety thresholds
# ─────────────────────────────────────────────────────────────────────────────

# STC3x — CO₂ in % (OSHA / NIOSH reference levels)
CO2_PCT_LEVELS = [
    (1,  "Extremely Low",        "#2a78d6", "Fresh outdoor air (~400 ppm)"),
    (5,  "Low",           "#0ca30c", "Well-ventilated indoor air"),
    (10,  "Slightly Low",             "#8cc63f", "Slightly elevated — monitor"),
    (25,  "Moderate",              "#fab219", "Poor ventilation — increase airflow"),
    (50,  "Slightly High",              "#e87b1a", "Headache / drowsiness risk"),
    (75,  "High",               "#d03b3b", "OSHA 8-hour max exposure"),
    (90,  "Extremely High",       "#7b0000", "IDLH — evacuate immediately"),
]

# MG-811 — estimated CO₂ in ppm
CO2_PPM_LEVELS = [
    (400,   "Ambient",  "#2a78d6", "Fresh outdoor air"),
    (800,   "Normal",   "#0ca30c", "Typical well-ventilated indoor air"),
    (1500,  "Elevated", "#fab219", "Some ventilation recommended"),
    (2500,  "Caution",  "#e87b1a", "Reduce occupancy / increase airflow"),
    (5000,  "Warning",  "#d03b3b", "OSHA permissible exposure limit (PEL)"),
    (40000, "Danger",   "#7b0000", "IDLH — evacuate immediately"),
]

TEMP_NORMAL    = (15.0, 35.0)
MAX_HISTORY    = 2000
REFRESH_MS_DEF = 2000


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def hex_to_rgba(hex_color: str, alpha: float = 0.13) -> str:
    """Convert '#RRGGBB' → 'rgba(r,g,b,alpha)'. Plotly rejects 8-digit hex."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def get_co2_pct_status(value: float) -> tuple:
    for upper, label, color, desc in CO2_PCT_LEVELS:
        if value <= upper:
            return label, color, desc
    return "Immediately Dangerous", "#7b0000", "IDLH — evacuate immediately"


def get_co2_ppm_status(value: float) -> tuple:
    for upper, label, color, desc in CO2_PPM_LEVELS:
        if value <= upper:
            return label, color, desc
    return "Danger", "#7b0000", "IDLH — evacuate immediately"


def get_temp_status(value: float) -> tuple:
    lo, hi = TEMP_NORMAL
    if lo <= value <= hi:
        return "Normal", "#0ca30c"
    return ("Cold", "#2a78d6") if value < lo else ("Hot", "#d03b3b")


def voltage_to_ppm(voltage: float, v_ref: float, c_ref: float, sensitivity: float) -> float:
    """
    MG-811 logarithmic conversion.
    As voltage ↓, CO₂ ↑ (inverse relationship).
      ppm = c_ref × 10^((v_ref - voltage) / sensitivity)
    sensitivity ≈ 0.076 V per decade (from MG-811 datasheet).
    """
    if sensitivity <= 0:
        return c_ref
    decades = (v_ref - voltage) / sensitivity
    return float(np.clip(c_ref * (10 ** decades), 0, 100_000))


def stat_card(col, label: str, value: str, unit: str = "", color: str = "#555"):
    col.markdown(
        f"<div style='border:0.5px solid #ddd;border-radius:8px;padding:10px;"
        f"text-align:center;min-height:70px'>"
        f"<div style='font-size:11px;color:#aaa;margin-bottom:2px'>{label}</div>"
        f"<div style='font-size:20px;font-weight:600;color:{color}'>"
        f"{value}<span style='font-size:11px;font-weight:400'> {unit}</span></div>"
        f"</div>",
        unsafe_allow_html=True,
    )


def status_banner(label: str, color: str, desc: str):
    st.markdown(
        f"<div style='background:{color}18;border-left:4px solid {color};"
        f"border-radius:6px;padding:10px 16px;margin-bottom:14px'>"
        f"<span style='font-weight:600;color:{color};font-size:15px'>● {label}</span>"
        f"<span style='color:#666;font-size:13px;margin-left:12px'>{desc}</span>"
        f"</div>",
        unsafe_allow_html=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Serial parsers
# ─────────────────────────────────────────────────────────────────────────────
RE_CO2   = re.compile(r"CO2 Concentration:\s*([\d.]+)\s*%",  re.IGNORECASE)
RE_TEMP  = re.compile(r"Temperature:\s*([\d.]+)\s*C",         re.IGNORECASE)
RE_VOLT  = re.compile(r"Average Sensor Voltage.*?:\s*([\d.]+)\s*V", re.IGNORECASE)


class STC3xReader:
    """Buffers the 3-line STC3x block and returns a dict on the '---' separator."""
    def __init__(self):
        self._buf: dict = {}

    def feed(self, line: str) -> dict | None:
        line = line.strip()
        m = RE_CO2.search(line)
        if m:
            self._buf["co2"] = float(m.group(1))
            return None
        m = RE_TEMP.search(line)
        if m:
            self._buf["temp"] = float(m.group(1))
            return None
        if line == "---" and "co2" in self._buf and "temp" in self._buf:
            result = dict(self._buf)
            self._buf.clear()
            return result
        return None


def parse_mg811_line(line: str) -> float | None:
    """Parse 'Average Sensor Voltage (ZERO POINT): 2.345 V' → 2.345."""
    m = RE_VOLT.search(line)
    return float(m.group(1)) if m else None


# ─────────────────────────────────────────────────────────────────────────────
# Simulators
# ─────────────────────────────────────────────────────────────────────────────

def simulate_stc3x() -> dict:
    sv = st.session_state.stc3x_sim
    sv["co2"]  = float(np.clip(sv["co2"]  + random.gauss(0, 0.008), 0.03, 2.0))
    sv["temp"] = float(np.clip(sv["temp"] + random.gauss(0, 0.05),  18.0, 32.0))
    return dict(sv)


def simulate_mg811() -> dict:
    sv = st.session_state.mg811_sim
    # Voltage drifts 2.3–2.8 V (higher V = lower CO₂)
    sv["voltage"] = float(np.clip(sv["voltage"] + random.gauss(0, 0.006), 2.2, 2.85))
    return dict(sv)


# ─────────────────────────────────────────────────────────────────────────────
# Plotly charts — STC3x
# ─────────────────────────────────────────────────────────────────────────────

def make_co2_pct_gauge(value: float) -> go.Figure:
    label, color, _ = get_co2_pct_status(value)
    steps, prev = [], 0.0
    for upper, _, c, _ in CO2_PCT_LEVELS:
        steps.append({"range": [prev, upper], "color": hex_to_rgba(c, 0.14)})
        prev = upper

    fig = go.Figure(go.Indicator(
        mode="gauge+number+delta",
        value=value,
        delta={"reference": 0.04, "valueformat": ".3f", "suffix": " %"},
        number={"suffix": " %", "font": {"size": 40, "color": color},
                "valueformat": ".3f"},
        title={"text": f"CO₂ Concentration<br>"
                       f"<span style='font-size:14px;color:{color}'>● {label}</span>",
               "font": {"size": 17}},
        gauge={
            "axis": {"range": [0, 5], "tickformat": ".1f",
                     "ticksuffix": "%", "nticks": 6, "tickcolor": "#aaa"},
            "bar": {"color": color, "thickness": 0.25},
            "bgcolor": "rgba(0,0,0,0)", "borderwidth": 0,
            "steps": steps,
            "threshold": {"line": {"color": color, "width": 3},
                          "thickness": 0.8, "value": value},
        },
    ))
    fig.update_layout(height=290, margin=dict(t=52, b=12, l=28, r=28),
                      paper_bgcolor="rgba(0,0,0,0)", font=dict(family="monospace"))
    return fig


def make_temp_gauge(value: float) -> go.Figure:
    status, color = get_temp_status(value)
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=value,
        number={"suffix": " °C", "font": {"size": 30, "color": color},
                "valueformat": ".2f"},
        title={"text": f"Temperature<br>"
                       f"<span style='font-size:13px;color:{color}'>● {status}</span>",
               "font": {"size": 15}},
        gauge={
            "axis": {"range": [0, 50], "nticks": 6, "tickcolor": "#aaa"},
            "bar": {"color": color, "thickness": 0.25},
            "bgcolor": "rgba(0,0,0,0)", "borderwidth": 0,
            "steps": [
                {"range": [0,  15], "color": hex_to_rgba("#2a78d6", 0.12)},
                {"range": [15, 35], "color": hex_to_rgba("#0ca30c", 0.12)},
                {"range": [35, 50], "color": hex_to_rgba("#d03b3b", 0.12)},
            ],
        },
    ))
    fig.update_layout(height=240, margin=dict(t=52, b=12, l=20, r=20),
                      paper_bgcolor="rgba(0,0,0,0)", font=dict(family="monospace"))
    return fig


def make_stc3x_history(df: pd.DataFrame) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.65, 0.35], vertical_spacing=0.08)
    for lo, hi, c in [(0, 0.04, "#2a78d6"), (0.04, 0.5, "#0ca30c"),
                      (0.5, 2.0, "#fab219"), (2.0, 5.0, "#d03b3b")]:
        fig.add_hrect(y0=lo, y1=hi, fillcolor=hex_to_rgba(c, 0.06),
                      line_width=0, row=1, col=1)
    fig.add_trace(go.Scatter(x=df["time"], y=df["co2"], mode="lines",
                             line=dict(color="#2a78d6", width=2.5),
                             fill="tozeroy", fillcolor=hex_to_rgba("#2a78d6", 0.06),
                             name="CO₂ %",
                             hovertemplate="%{y:.3f} %<extra>CO₂</extra>"),
                  row=1, col=1)
    fig.add_hline(y=0.04, line_dash="dot", line_color="#aaa", line_width=1,
                  annotation_text="ambient 0.04 %", annotation_font_size=10,
                  annotation_font_color="#aaa", row=1, col=1)
    fig.add_trace(go.Scatter(x=df["time"], y=df["temp"], mode="lines",
                             line=dict(color="#e87ba4", width=2), name="Temp °C",
                             hovertemplate="%{y:.2f} °C<extra>Temp</extra>"),
                  row=2, col=1)
    fig.update_layout(height=360, margin=dict(t=12, b=32, l=56, r=12),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", x=0, y=1.04),
                      font=dict(family="monospace", size=11))
    fig.update_yaxes(title_text="CO₂ (%)", range=[0, 5], tickformat=".2f",
                     showgrid=True, gridcolor="rgba(0,0,0,0.05)", row=1, col=1)
    fig.update_yaxes(title_text="°C", range=[0, 50], tickformat=".1f",
                     showgrid=True, gridcolor="rgba(0,0,0,0.05)", row=2, col=1)
    fig.update_xaxes(showgrid=False, tickfont=dict(size=10, color="#aaa"))
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Plotly charts — MG-811
# ─────────────────────────────────────────────────────────────────────────────

def make_voltage_gauge(voltage: float) -> go.Figure:
    # Voltage alone has no safety zone — colour by estimated ppm
    ppm   = st.session_state.mg811_ppm_last
    _, color, _ = get_co2_ppm_status(ppm)
    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=voltage,
        number={"suffix": " V", "font": {"size": 34, "color": color},
                "valueformat": ".3f"},
        title={"text": "Sensor Output Voltage<br>"
                       "<span style='font-size:12px;color:#999'>"
                       "Higher V = Lower CO₂ (inverse)</span>",
               "font": {"size": 15}},
        gauge={
            "axis": {"range": [0, 5], "nticks": 6, "tickcolor": "#aaa",
                     "ticksuffix": " V"},
            "bar": {"color": color, "thickness": 0.25},
            "bgcolor": "rgba(0,0,0,0)", "borderwidth": 0,
            "steps": [
                {"range": [0.0, 1.5], "color": hex_to_rgba("#d03b3b", 0.12)},
                {"range": [1.5, 2.2], "color": hex_to_rgba("#fab219", 0.12)},
                {"range": [2.2, 3.0], "color": hex_to_rgba("#0ca30c", 0.12)},
                {"range": [3.0, 5.0], "color": hex_to_rgba("#2a78d6", 0.10)},
            ],
        },
    ))
    fig.update_layout(height=250, margin=dict(t=52, b=12, l=20, r=20),
                      paper_bgcolor="rgba(0,0,0,0)", font=dict(family="monospace"))
    return fig


def make_ppm_gauge(ppm: float) -> go.Figure:
    label, color, _ = get_co2_ppm_status(ppm)
    steps, prev = [], 0.0
    for upper, _, c, _ in CO2_PPM_LEVELS:
        steps.append({"range": [prev, upper], "color": hex_to_rgba(c, 0.14)})
        prev = upper

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=ppm,
        number={"suffix": " ppm", "font": {"size": 34, "color": color},
                "valueformat": ".0f"},
        title={"text": f"Estimated CO₂<br>"
                       f"<span style='font-size:14px;color:{color}'>● {label}</span>",
               "font": {"size": 17}},
        gauge={
            "axis": {"range": [0, 5000], "nticks": 6, "tickcolor": "#aaa"},
            "bar": {"color": color, "thickness": 0.25},
            "bgcolor": "rgba(0,0,0,0)", "borderwidth": 0,
            "steps": steps,
            "threshold": {"line": {"color": color, "width": 3},
                          "thickness": 0.8, "value": ppm},
        },
    ))
    fig.update_layout(height=290, margin=dict(t=52, b=12, l=28, r=28),
                      paper_bgcolor="rgba(0,0,0,0)", font=dict(family="monospace"))
    return fig


def make_mg811_history(df: pd.DataFrame) -> go.Figure:
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True,
                        row_heights=[0.5, 0.5], vertical_spacing=0.08)
    fig.add_trace(go.Scatter(x=df["time"], y=df["voltage"], mode="lines+markers",
                             marker=dict(size=4), line=dict(color="#1baf7a", width=2.5),
                             name="Voltage (V)",
                             hovertemplate="%{y:.3f} V<extra>Voltage</extra>"),
                  row=1, col=1)
    fig.add_trace(go.Scatter(x=df["time"], y=df["ppm_est"], mode="lines",
                             line=dict(color="#e87b1a", width=2.5),
                             fill="tozeroy", fillcolor=hex_to_rgba("#e87b1a", 0.06),
                             name="Est. ppm",
                             hovertemplate="%{y:.0f} ppm<extra>CO₂ est.</extra>"),
                  row=2, col=1)
    fig.add_hline(y=400, line_dash="dot", line_color="#aaa", line_width=1,
                  annotation_text="ambient 400 ppm", annotation_font_size=10,
                  annotation_font_color="#aaa", row=2, col=1)
    fig.update_layout(height=360, margin=dict(t=12, b=32, l=60, r=12),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      legend=dict(orientation="h", x=0, y=1.04),
                      font=dict(family="monospace", size=11))
    fig.update_yaxes(title_text="Voltage (V)", tickformat=".3f",
                     showgrid=True, gridcolor="rgba(0,0,0,0.05)", row=1, col=1)
    fig.update_yaxes(title_text="CO₂ (ppm)", showgrid=True,
                     gridcolor="rgba(0,0,0,0.05)", row=2, col=1)
    fig.update_xaxes(showgrid=False, tickfont=dict(size=10, color="#aaa"))
    return fig


# ─────────────────────────────────────────────────────────────────────────────
# Excel export builder
# ─────────────────────────────────────────────────────────────────────────────

def build_excel(stc3x_rows: list, mg811_rows: list) -> bytes:
    """Build a formatted .xlsx with two data sheets + a summary sheet."""
    output = io.BytesIO()
    df_stc  = pd.DataFrame(stc3x_rows)  if stc3x_rows  else pd.DataFrame()
    df_mg   = pd.DataFrame(mg811_rows)  if mg811_rows   else pd.DataFrame()

    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        # ── Sheet 1: STC3x ──
        if not df_stc.empty:
            df_stc_out = df_stc.rename(columns={
                "time": "Timestamp",
                "co2":  "CO2_percent",
                "temp": "Temperature_C",
            })
            df_stc_out.to_excel(writer, sheet_name="STC3x_CO2", index=False)
            _format_sheet(writer, "STC3x_CO2", df_stc_out)

        # ── Sheet 2: MG-811 ──
        if not df_mg.empty:
            df_mg_out = df_mg.rename(columns={
                "time":    "Timestamp",
                "voltage": "Sensor_Voltage_V",
                "ppm_est": "Estimated_CO2_ppm",
            })
            df_mg_out.to_excel(writer, sheet_name="MG811_Voltage", index=False)
            _format_sheet(writer, "MG811_Voltage", df_mg_out)

        # ── Sheet 3: Summary ──
        summary_data = {
            "Metric": ["STC3x Readings", "MG-811 Readings",
                       "STC3x CO2 Min (%)", "STC3x CO2 Max (%)",
                       "STC3x CO2 Avg (%)", "STC3x Temp Min (°C)",
                       "STC3x Temp Max (°C)", "MG-811 Voltage Min (V)",
                       "MG-811 Voltage Max (V)", "MG-811 Est. CO2 Min (ppm)",
                       "MG-811 Est. CO2 Max (ppm)", "Export Time"],
            "Value": [
                len(df_stc), len(df_mg),
                f"{df_stc['co2'].min():.3f}"  if not df_stc.empty else "—",
                f"{df_stc['co2'].max():.3f}"  if not df_stc.empty else "—",
                f"{df_stc['co2'].mean():.3f}" if not df_stc.empty else "—",
                f"{df_stc['temp'].min():.2f}" if not df_stc.empty else "—",
                f"{df_stc['temp'].max():.2f}" if not df_stc.empty else "—",
                f"{df_mg['voltage'].min():.3f}"  if not df_mg.empty else "—",
                f"{df_mg['voltage'].max():.3f}"  if not df_mg.empty else "—",
                f"{df_mg['ppm_est'].min():.0f}"  if not df_mg.empty else "—",
                f"{df_mg['ppm_est'].max():.0f}"  if not df_mg.empty else "—",
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            ],
        }
        pd.DataFrame(summary_data).to_excel(writer, sheet_name="Summary", index=False)
        _format_sheet(writer, "Summary", pd.DataFrame(summary_data))

    return output.getvalue()


def _format_sheet(writer, sheet_name: str, df: pd.DataFrame):
    """Apply header styling and auto column widths via openpyxl."""
    from openpyxl.styles import PatternFill, Font, Alignment
    ws = writer.sheets[sheet_name]
    header_fill = PatternFill("solid", fgColor="2A78D6")
    for cell in ws[1]:
        cell.fill = header_fill
        cell.font = Font(bold=True, color="FFFFFF")
        cell.alignment = Alignment(horizontal="center")
    for col in ws.columns:
        max_len = max(len(str(c.value or "")) for c in col) + 4
        ws.column_dimensions[col[0].column_letter].width = min(max_len, 30)


def build_csv(stc3x_rows: list, mg811_rows: list) -> str:
    """Build a flat CSV merging both datasets side by side by row index."""
    df_s = pd.DataFrame(stc3x_rows).add_prefix("stc3x_") if stc3x_rows else pd.DataFrame()
    df_m = pd.DataFrame(mg811_rows).add_prefix("mg811_") if mg811_rows else pd.DataFrame()
    combined = pd.concat([df_s, df_m], axis=1)
    return combined.to_csv(index=False)


# ─────────────────────────────────────────────────────────────────────────────
# Session state
# ─────────────────────────────────────────────────────────────────────────────

def init_state():
    defaults = {
        "demo_mode":       True,
        # STC3x
        "stc3x_history":   [],
        "stc3x_alerts":    [],
        "stc3x_conn":      None,
        "stc3x_reader":    STC3xReader(),
        "stc3x_last":      {"co2": 0.04, "temp": 25.0},
        "stc3x_sim":       {"co2": 0.04, "temp": 25.0},
        # MG-811
        "mg811_history":   [],
        "mg811_alerts":    [],
        "mg811_conn":      None,
        "mg811_last":      {"voltage": 2.60},
        "mg811_ppm_last":  400.0,
        "mg811_sim":       {"voltage": 2.60},
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v


# ─────────────────────────────────────────────────────────────────────────────
# Sidebar
# ─────────────────────────────────────────────────────────────────────────────

def render_sidebar() -> tuple[int, float, float, float]:
    st.sidebar.title("⚙️ Settings")

    # ── Demo mode ──
    st.session_state.demo_mode = st.sidebar.toggle(
        "Demo mode (simulated data)", value=st.session_state.demo_mode
    )
    st.sidebar.markdown("---")

    def port_block(label: str, key_conn: str, key_reader: str = None):
        """Reusable connect/disconnect block for a sensor."""
        if not SERIAL_AVAILABLE:
            st.sidebar.error("pyserial not installed.\n`pip install pyserial`")
            return
        ports = [p.device for p in serial.tools.list_ports.comports()]
        if not ports:
            st.sidebar.warning("No COM ports found.")
            return
        port = st.sidebar.selectbox(f"{label} port", ports, key=f"sel_{key_conn}")
        baud = st.sidebar.selectbox("Baud rate", [9600, 38400, 57600, 115200],
                                    index=3, key=f"baud_{key_conn}")
        c1, c2 = st.sidebar.columns(2)
        if c1.button("Connect", key=f"conn_{key_conn}", use_container_width=True):
            try:
                if st.session_state[key_conn]:
                    st.session_state[key_conn].close()
                st.session_state[key_conn] = serial.Serial(port, baud, timeout=0.3)
                if key_reader:
                    st.session_state[key_reader] = STC3xReader()
                st.sidebar.success(f"Connected: {port}")
            except Exception as e:
                st.sidebar.error(str(e))
        if c2.button("Disconnect", key=f"disc_{key_conn}", use_container_width=True):
            if st.session_state[key_conn]:
                st.session_state[key_conn].close()
                st.session_state[key_conn] = None

    if not st.session_state.demo_mode:
        with st.sidebar.expander("🔌 STC3x Sensor (Port 1)", expanded=True):
            port_block("STC3x", "stc3x_conn", "stc3x_reader")
        with st.sidebar.expander("🔌 MG-811 Sensor (Port 2)", expanded=True):
            port_block("MG-811", "mg811_conn")

    st.sidebar.markdown("---")

    # ── MG-811 calibration ──
    with st.sidebar.expander("🔧 MG-811 Calibration"):
        st.caption("Adjust to match your sensor's burn-in voltage.")
        v_ref = st.slider("V_ref at fresh air (V)", 1.5, 4.0, 2.60, 0.01)
        c_ref = st.number_input("Reference CO₂ (ppm)", 200, 1000, 427, 1)
        sensitivity = st.slider("Sensitivity (V/decade)", 0.03, 0.20, 0.076, 0.001,
                                 format="%.3f")
    st.sidebar.markdown("---")

    refresh_ms = st.sidebar.slider(
        "Refresh interval (ms)", 500, 8000, REFRESH_MS_DEF, 250
    )
    if st.sidebar.button("🗑 Clear all history", use_container_width=True):
        for k in ["stc3x_history", "stc3x_alerts", "mg811_history", "mg811_alerts"]:
            st.session_state[k] = []

    st.sidebar.markdown("---")
    st.sidebar.markdown("**STC3x serial format:**")
    st.sidebar.code("CO2 Concentration: 0.040 %\nTemperature: 25.00 C\n---",
                    language="text")
    st.sidebar.markdown("**MG-811 serial format:**")
    st.sidebar.code("Average Sensor Voltage (ZERO POINT): 2.345 V",
                    language="text")

    return refresh_ms, v_ref, float(c_ref), sensitivity


# ─────────────────────────────────────────────────────────────────────────────
# Tab renderers
# ─────────────────────────────────────────────────────────────────────────────

def render_stc3x_tab():
    r       = st.session_state.stc3x_last
    co2     = r["co2"]
    temp    = r["temp"]
    label, color, desc = get_co2_pct_status(co2)
    mode    = "🟢 Demo" if st.session_state.demo_mode else "🔵 Live"

    st.markdown(
        f"<div style='display:flex;align-items:baseline;gap:12px;margin-bottom:6px'>"
        f"<h3 style='margin:0'>SparkFun STC3x — CO₂ Sensor</h3>"
        f"<span style='font-size:12px;color:#aaa'>{mode} · "
        f"Range 0–25 % · Calibrated at 0.04 % (FRC)</span></div>",
        unsafe_allow_html=True,
    )
    status_banner(label, color, desc)

    # Gauges
    g1, g2 = st.columns([2, 1])
    with g1:
        st.plotly_chart(make_co2_pct_gauge(co2), use_container_width=True,
                        config={"displayModeBar": False})
    with g2:
        st.plotly_chart(make_temp_gauge(temp), use_container_width=True,
                        config={"displayModeBar": False})

    # Stats
    h = st.session_state.stc3x_history
    if len(h) >= 2:
        df = pd.DataFrame(h)
        st.markdown("#### Session Statistics")
        c1, c2, c3, c4, c5 = st.columns(5)
        _, mc, _ = get_co2_pct_status(df["co2"].min())
        _, xc, _ = get_co2_pct_status(df["co2"].max())
        stat_card(c1, "Current CO₂",  f"{co2:.3f}",             "%",  color)
        stat_card(c2, "Min CO₂",      f"{df['co2'].min():.3f}", "%",  mc)
        stat_card(c3, "Max CO₂",      f"{df['co2'].max():.3f}", "%",  xc)
        stat_card(c4, "Avg CO₂",      f"{df['co2'].mean():.3f}","%",  "#555")
        stat_card(c5, "Readings",      str(len(df)),             "",   "#888")

        st.markdown("#### History — CO₂ & Temperature")
        st.plotly_chart(make_stc3x_history(df), use_container_width=True,
                        config={"displayModeBar": False})
    else:
        st.info("Waiting for data — history chart appears after 2+ readings.")

    if st.session_state.stc3x_alerts:
        with st.expander(f"⚠️ Alert log ({len(st.session_state.stc3x_alerts)} events)"):
            for a in reversed(st.session_state.stc3x_alerts[-30:]):
                st.markdown(
                    f"<span style='color:{a['color']};font-size:13px'>"
                    f"[{a['time']}]  {a['msg']}</span>",
                    unsafe_allow_html=True,
                )


def render_mg811_tab(v_ref: float, c_ref: float, sensitivity: float):
    voltage  = st.session_state.mg811_last["voltage"]
    ppm      = voltage_to_ppm(voltage, v_ref, c_ref, sensitivity)
    st.session_state.mg811_ppm_last = ppm
    label, color, desc = get_co2_ppm_status(ppm)
    mode = "🟢 Demo" if st.session_state.demo_mode else "🔵 Live"

    st.markdown(
        f"<div style='display:flex;align-items:baseline;gap:12px;margin-bottom:6px'>"
        f"<h3 style='margin:0'>MG-811 — CO₂ Voltage Sensor</h3>"
        f"<span style='font-size:12px;color:#aaa'>{mode} · "
        f"ADC pin 34 · 100-sample average · ~8 s/reading</span></div>",
        unsafe_allow_html=True,
    )
    status_banner(label, color, desc)

    # Calibration note
    st.markdown(
        f"<div style='background:#f8f8f8;border-radius:6px;padding:8px 14px;"
        f"font-size:12px;color:#666;margin-bottom:12px'>"
        f"🔧 Calibration: V_ref = {v_ref:.2f} V at {c_ref:.0f} ppm · "
        f"Sensitivity = {sensitivity:.3f} V/decade &nbsp;|&nbsp; "
        f"Adjust in sidebar ▸ MG-811 Calibration</div>",
        unsafe_allow_html=True,
    )

    # Gauges
    g1, g2 = st.columns([1, 1])
    with g1:
        st.plotly_chart(make_voltage_gauge(voltage), use_container_width=True,
                        config={"displayModeBar": False})
    with g2:
        st.plotly_chart(make_ppm_gauge(ppm), use_container_width=True,
                        config={"displayModeBar": False})

    # Stats
    h = st.session_state.mg811_history
    if len(h) >= 2:
        df = pd.DataFrame(h)
        st.markdown("#### Session Statistics")
        c1, c2, c3, c4, c5 = st.columns(5)
        _, vc, _  = get_co2_ppm_status(df["ppm_est"].min())
        _, xc, _  = get_co2_ppm_status(df["ppm_est"].max())
        stat_card(c1, "Current Voltage", f"{voltage:.3f}",               "V",   color)
        stat_card(c2, "Est. CO₂",        f"{ppm:.0f}",                   "ppm", color)
        stat_card(c3, "Min Voltage",      f"{df['voltage'].min():.3f}",   "V",   "#555")
        stat_card(c4, "Max Voltage",      f"{df['voltage'].max():.3f}",   "V",   "#555")
        stat_card(c5, "Readings",          str(len(df)),                  "",    "#888")

        st.markdown("#### History — Voltage & Estimated CO₂")
        st.plotly_chart(make_mg811_history(df), use_container_width=True,
                        config={"displayModeBar": False})
    else:
        st.info(
            "Waiting for data. MG-811 produces one reading every ~8 seconds "
            "(100 samples × 50 ms + 3 s delay)."
        )

    if st.session_state.mg811_alerts:
        with st.expander(f"⚠️ Alert log ({len(st.session_state.mg811_alerts)} events)"):
            for a in reversed(st.session_state.mg811_alerts[-30:]):
                st.markdown(
                    f"<span style='color:{a['color']};font-size:13px'>"
                    f"[{a['time']}]  {a['msg']}</span>",
                    unsafe_allow_html=True,
                )


def render_export_tab():
    st.markdown("### 📥 Export Sensor Data")
    stc3x_rows = st.session_state.stc3x_history
    mg811_rows = st.session_state.mg811_history
    total      = len(stc3x_rows) + len(mg811_rows)

    # Summary counts
    m1, m2, m3 = st.columns(3)
    stat_card(m1, "STC3x Readings",  str(len(stc3x_rows)), "", "#2a78d6")
    stat_card(m2, "MG-811 Readings", str(len(mg811_rows)),  "", "#1baf7a")
    stat_card(m3, "Total Rows",       str(total),            "", "#555")

    st.markdown("---")

    if total == 0:
        st.info("No data yet — switch to Tab 1 or Tab 2 to collect readings first.")
        return

    # ── Download buttons ──
    now_str = datetime.now().strftime("%Y%m%d_%H%M%S")

    col_xl, col_csv = st.columns(2)

    with col_xl:
        st.markdown("#### 📊 Excel (.xlsx)")
        st.caption("Three sheets: STC3x data · MG-811 data · Summary statistics")
        xl_bytes = build_excel(stc3x_rows, mg811_rows)
        st.download_button(
            label="⬇️ Download Excel",
            data=xl_bytes,
            file_name=f"gas_readings_{now_str}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    with col_csv:
        st.markdown("#### 📄 CSV (.csv)")
        st.caption("Flat file with both sensors merged side by side")
        csv_str = build_csv(stc3x_rows, mg811_rows)
        st.download_button(
            label="⬇️ Download CSV",
            data=csv_str,
            file_name=f"gas_readings_{now_str}.csv",
            mime="text/csv",
            use_container_width=True,
        )

    st.markdown("---")

    # ── Data preview ──
    if stc3x_rows:
        with st.expander(f"👁 Preview STC3x data ({len(stc3x_rows)} rows)"):
            df = pd.DataFrame(stc3x_rows)
            df.columns = ["Timestamp", "CO2 (%)", "Temperature (°C)"]
            st.dataframe(df, use_container_width=True, height=240)

    if mg811_rows:
        with st.expander(f"👁 Preview MG-811 data ({len(mg811_rows)} rows)"):
            df = pd.DataFrame(mg811_rows)
            df.columns = ["Timestamp", "Voltage (V)", "Est. CO₂ (ppm)"]
            st.dataframe(df, use_container_width=True, height=240)


# ─────────────────────────────────────────────────────────────────────────────
# Data acquisition helpers
# ─────────────────────────────────────────────────────────────────────────────

def acquire_stc3x():
    """Read one STC3x block from serial (non-blocking) or simulate."""
    if st.session_state.demo_mode:
        reading = simulate_stc3x()
    else:
        conn = st.session_state.stc3x_conn
        reading = None
        if conn and conn.is_open:
            try:
                for _ in range(8):
                    raw = conn.readline().decode("utf-8", errors="ignore")
                    result = st.session_state.stc3x_reader.feed(raw)
                    if result:
                        reading = result
                        break
            except Exception as e:
                st.error(f"STC3x serial error: {e}")
        if reading is None:
            return   # keep last displayed value

    st.session_state.stc3x_last = reading
    st.session_state.stc3x_history.append({
        "time": datetime.now().strftime("%H:%M:%S"),
        "co2":  reading["co2"],
        "temp": reading["temp"],
    })
    if len(st.session_state.stc3x_history) > MAX_HISTORY:
        st.session_state.stc3x_history.pop(0)

    label, color, desc = get_co2_pct_status(reading["co2"])
    if label not in ("Ambient", "Acceptable"):
        st.session_state.stc3x_alerts.append({
            "time":  datetime.now().strftime("%H:%M:%S"),
            "msg":   f"STC3x CO₂ {label}: {reading['co2']:.3f} % — {desc}",
            "color": color,
        })


def acquire_mg811(v_ref: float, c_ref: float, sensitivity: float):
    """Read one MG-811 line from serial (non-blocking) or simulate."""
    if st.session_state.demo_mode:
        reading = simulate_mg811()
    else:
        conn = st.session_state.mg811_conn
        reading = None
        if conn and conn.is_open:
            try:
                # MG-811 cycle ~8 s; check for buffered data without blocking
                if conn.in_waiting > 0:
                    raw  = conn.readline().decode("utf-8", errors="ignore")
                    volt = parse_mg811_line(raw)
                    if volt is not None:
                        reading = {"voltage": volt}
            except Exception as e:
                st.error(f"MG-811 serial error: {e}")
        if reading is None:
            return

    voltage = reading["voltage"]
    ppm     = voltage_to_ppm(voltage, v_ref, c_ref, sensitivity)
    st.session_state.mg811_last     = {"voltage": voltage}
    st.session_state.mg811_ppm_last = ppm
    st.session_state.mg811_history.append({
        "time":    datetime.now().strftime("%H:%M:%S"),
        "voltage": voltage,
        "ppm_est": ppm,
    })
    if len(st.session_state.mg811_history) > MAX_HISTORY:
        st.session_state.mg811_history.pop(0)

    label, color, desc = get_co2_ppm_status(ppm)
    if label not in ("Ambient", "Normal"):
        st.session_state.mg811_alerts.append({
            "time":  datetime.now().strftime("%H:%M:%S"),
            "msg":   f"MG-811 CO₂ {label}: {ppm:.0f} ppm — {desc}",
            "color": color,
        })


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main():
    init_state()
    refresh_ms, v_ref, c_ref, sensitivity = render_sidebar()

    # Acquire data for both sensors every cycle
    acquire_stc3x()
    acquire_mg811(v_ref, c_ref, sensitivity)

    # ── Tabs ──
    tab1, tab2, tab3 = st.tabs([
        "🟢  STC3x — CO₂ (%)",
        "🟠  MG-811 — Voltage / ppm",
        "📥  Export",
    ])
    with tab1:
        render_stc3x_tab()
    with tab2:
        render_mg811_tab(v_ref, c_ref, sensitivity)
    with tab3:
        render_export_tab()

    # Auto-refresh
    time.sleep(refresh_ms / 1000)
    st.rerun()


if __name__ == "__main__":
    main()