import base64
import html
import re
import time
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import gspread
from google.oauth2.service_account import Credentials

# -----------------------------------------------------------------------------
# 1. PAGE SETUP & CONFIG
# -----------------------------------------------------------------------------
st.set_page_config(
    page_title="Executive Operations & Production Hub 2026-27",
    page_icon="📊",
    layout="wide",
)

SHEET_REGISTRY = {
    "Master Sheet": {
        "id": "1wBo0zF2wlqBzY5ZQaX5CPjCOedORwg_1Eh01vje5KFU",
        "type": "tracker",
        # Data tab the sheet dashboards are built from (first match wins).
        "source_tabs": ["CONFIRMATION SHEET", "ORDER SHEET"],
    },
    "Sample Summary": {
        "id": "1EfQ08lXGL8YjtoaZ2ygJtNVaED7doDijo2jFvCuS7Kg",
        "type": "sample_summary",
        "gid": 78218658,   # fallback only (used if none of the tab names below exist)
        # Source (data) tab, first match wins. Same order as the Apps Script.
        "source_tabs": ["2026-27", "SAMPLE SHEET 2026-27", "Sheet1"],
    },
    "Fabricator Report": {
        "id": "1hP9Bkvu2F-qj4tXYhOCb4alfJOZZtAsMQNOjVQyWoo4",
        "type": "overview_fabricators",
        "gid": 164418035,  # fallback only
        "source_tabs": ["MONTHWISE SUMMARY", "MONTHWISE SUMMARY 2026-27"],
    },
    "Fabric PO": {
        "id": "1CvNPrdV4rQBR3_r8oIoPsW1ak123BGj88-qYbUU1y4I",
        "type": "fabric_po",
        "gid": 418084418,  # fallback only (INDEX tab)
        "source_tabs": ["INDEX"],
    },
    "Designer Target": {
        "id": "14USlaIteR3xlHjwzJOhZjLoyAZi-xAiF4LMXUE_UR2Y",
        "type": "target_revenue",
        "source_tabs": ["NEW"],        # NEW orders  -> yearly target applies here
        "repeat_tabs": ["REPEAT"],     # REPEAT orders -> revenue only
        "annual_target_cr": 50.2,      # yearly NEW target in crore (also editable on the dashboard)
    },
    "FLO Future": {
        "id": "1GFJRv_9gk2I1ARPxSEoEDsyzzJ1flYKnUtIuGV_DpZw",   # FLO_Future Nov 2026
        "type": "flo_future",
        "source_tabs": ["Samples"],    # one row per promised sample (Designer, Qty Promised, Completed, Status, Due Date)
        "visits_tabs": ["Visits & Meetings"],   # 'Causative Milestone' blocks (visits + meetings)
        "period_label": "Nov 2026",
    },
}

# Google Sheet data is re-read at most once per this many seconds (live updates)
LIVE_TTL = 60          # sheet DATA is re-read from Google at most this often (seconds)
TAB_TTL = 600          # list of tab names changes rarely -> re-read every 10 min (or "Refresh data now")
BOOK_TTL = 3600        # an opened sheet handle is reused for 1 hour (saves one Google request per fetch)

# Financial year shown on the dashboards (April 2026 – March 2027)
FY_START_YEAR = 2026
MONTHS = ["April", "May", "June", "July", "August", "September",
          "October", "November", "December", "January", "February", "March"]
MONTH_NUM = {m[:3].lower(): i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}
MONTH_NAME_BY_NUM = {v: k for k, v in {
    "January": 1, "February": 2, "March": 3, "April": 4, "May": 5, "June": 6, "July": 7,
    "August": 8, "September": 9, "October": 10, "November": 11, "December": 12}.items()}
FY_MONTH_LABELS = [
    f"{m}-{(FY_START_YEAR if i < 9 else FY_START_YEAR + 1) % 100:02d}" for i, m in enumerate(MONTHS)
]

CATEGORIES_LIST = ["CO-ORD SET", "KURTA PANT", "TOP", "KURTA PANT DUPATTA", "DRESS", "KAFTAN", "KURTA"]
# Designers always listed on the Designer Dashboard (even with 0 volume), like the sheet.
KNOWN_DESIGNERS = ["ANAMIKA", "AYUSHI", "CHARU", "DEEPIKA", "JYOTI", "KEERTI",
                   "KRISHNA", "MANSI", "MUSKAN", "REKHA"]

# Source-tab columns used by the Master Sheet dashboards
MASTER_COLS = {
    "date_client": "B",   # order date -> month
    "designer": "C",
    "type": "D",          # NEW / REPEAT
    "category": "F",
    "qty": "J",           # order qty
    "client": "N",
    "fabricator": "AA",
    "date_prod": "AC",
    "delay": "AH",        # delay days (>0 overdue)
    "production": "AS",   # in production pcs
    "received": "AT",     # received pcs
    "pending": "AU",      # pending pcs
    "status": "AX",
}

def _stretch_kwargs():
    """Full-width kwarg that works on both old and new Streamlit versions."""
    try:
        major, minor = (int(p) for p in st.__version__.split(".")[:2])
    except Exception:
        return {"use_container_width": True}
    return {"width": "stretch"} if (major, minor) >= (1, 50) else {"use_container_width": True}


STRETCH = _stretch_kwargs()

# --- colours ---------------------------------------------------------------
# Light mode uses deeper colours, dark mode uses brighter ones (readable on a dark background).
COLOR_SETS = {
    "light": {"blue": "#2563eb", "green": "#059669", "amber": "#d97706", "red": "#dc2626", "purple": "#7c3aed",
              "cyan": "#0891b2", "pink": "#db2777", "lime": "#65a30d", "orange": "#ea580c", "teal": "#0d9488",
              "violet": "#9333ea", "yellow": "#ca8a04", "slate": "#94a3b8"},
    "dark": {"blue": "#60a5fa", "green": "#34d399", "amber": "#fbbf24", "red": "#f87171", "purple": "#a78bfa",
             "cyan": "#22d3ee", "pink": "#f472b6", "lime": "#a3e635", "orange": "#fb923c", "teal": "#2dd4bf",
             "violet": "#c084fc", "yellow": "#facc15", "slate": "#94a3b8"},
}
PALETTE_ORDER = ["blue", "green", "amber", "red", "purple", "cyan", "pink", "lime", "orange", "teal", "violet", "yellow"]

# Runtime theme state (set by apply_theme on every full run)
COLORS = dict(COLOR_SETS["light"])
PALETTE = [COLORS[k] for k in PALETTE_ORDER]
PLOT_TEMPLATE = "plotly_white"
PLOT_TEXT = "#0f172a"
PLOT_MUTED = "#475569"
PLOT_GRID = "#e2e8f0"
HEAT_SCALE = "Blues"
IS_DARK = False


def pal():
    """Categorical colour list for the active mode."""
    return list(PALETTE)


THEMES = {
    "light": {
        "APP_BG": "#f4f6fb", "PANEL_BG": "#ffffff", "CARD_BG": "#f8fafc", "CHIP_BG": "#e8eef8",
        "BORDER": "#dfe5ef", "TEXT": "#0f172a", "MUTED": "#5b6b82", "ACCENT": "#2563eb",
        "TITLE_BG": "#111827", "TITLE_TEXT": "#ffffff",
        "TH_BG": "#1f2a44", "TH_TEXT": "#ffffff", "ROW_ALT": "#f8fafc", "ROW_HOVER": "#eef4ff",
        "TOTAL_BG": "#e3ecff", "TOTAL_TEXT": "#0b2a6b",
        "GOOD_BG": "#dcfce7", "GOOD_TX": "#166534", "MID_BG": "#fef3c7", "MID_TX": "#92400e",
        "BAD_BG": "#fee2e2", "BAD_TX": "#991b1b",
        "BAR": "rgba(37, 99, 235, 0.17)", "BAR_NEG": "rgba(220, 38, 38, 0.15)",
        "SHADOW": "0 2px 10px rgba(15, 23, 42, 0.06)", "SCHEME": "light",
    },
    "dark": {
        "APP_BG": "#0e1525", "PANEL_BG": "#161f33", "CARD_BG": "#1b2640", "CHIP_BG": "#243150",
        "BORDER": "#2c3a58", "TEXT": "#e6edf7", "MUTED": "#9aabc6", "ACCENT": "#60a5fa",
        "TITLE_BG": "#1b2640", "TITLE_TEXT": "#f1f5fb",
        "TH_BG": "#24324f", "TH_TEXT": "#e6edf7", "ROW_ALT": "#19233a", "ROW_HOVER": "#22304d",
        "TOTAL_BG": "#1e3a66", "TOTAL_TEXT": "#e6f0ff",
        "GOOD_BG": "rgba(52, 211, 153, 0.18)", "GOOD_TX": "#6ee7b7", "MID_BG": "rgba(251, 191, 36, 0.16)",
        "MID_TX": "#fcd34d", "BAD_BG": "rgba(248, 113, 113, 0.16)", "BAD_TX": "#fca5a5",
        "BAR": "rgba(96, 165, 250, 0.26)", "BAR_NEG": "rgba(248, 113, 113, 0.24)",
        "SHADOW": "0 2px 12px rgba(0, 0, 0, 0.35)", "SCHEME": "dark",
    },
}

# KPI tile tints (cycled), similar to the coloured KPI boxes in the Google Sheet dashboards
KPI_TONES = ["blue", "green", "amber", "red", "orange", "purple", "teal", "pink"]

CSS_TEMPLATE = """
<style>
:root {
    --app-bg: __APP_BG__; --panel-bg: __PANEL_BG__; --card-bg: __CARD_BG__; --text-color: __TEXT__;
    --muted-color: __MUTED__; --border-color: __BORDER__; --chip-bg: __CHIP_BG__; --accent: __ACCENT__;
    --title-bg: __TITLE_BG__; --title-text: __TITLE_TEXT__; --th-bg: __TH_BG__; --th-text: __TH_TEXT__;
    --row-alt: __ROW_ALT__; --row-hover: __ROW_HOVER__; --total-bg: __TOTAL_BG__; --total-text: __TOTAL_TEXT__;
    --good-bg: __GOOD_BG__; --good-tx: __GOOD_TX__; --mid-bg: __MID_BG__; --mid-tx: __MID_TX__;
    --bad-bg: __BAD_BG__; --bad-tx: __BAD_TX__; --shadow: __SHADOW__; --bar: __BAR__; --bar-neg: __BAR_NEG__;
}
.stApp { background-color: var(--app-bg); color: var(--text-color); color-scheme: __SCHEME__;
         font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
.block-container { padding-top: 3.6rem; padding-bottom: 3rem; max-width: 1650px; }
[data-testid="stHeader"] { background: var(--app-bg); }
[data-testid="stHeader"] button, [data-testid="stHeader"] svg, [data-testid="stToolbar"] * { color: var(--text-color) !important; }
[data-testid="stDecoration"] { display: none; }

/* ---------- text ---------- */
.stApp h1, .stApp h2, .stApp h3, .stApp h4, .stApp h5, .stApp h6,
.stApp .stMarkdown, .stApp .stMarkdown p, .stApp .stMarkdown li,
.stApp [data-testid="stWidgetLabel"] p, .stApp label, .stApp [data-testid="stCheckbox"] p,
.stApp [data-testid="stToggle"] p, .stApp [data-testid="stSpinner"] * { color: var(--text-color); }
.stApp [data-testid="stCaptionContainer"], .stApp [data-testid="stCaptionContainer"] p { color: var(--muted-color); }
.stApp hr { border-color: var(--border-color); margin: 1.1rem 0; }
.stApp [data-testid="stWidgetLabel"] p { font-weight: 650; font-size: .8rem; white-space: normal; }

/* ---------- sidebar ---------- */
[data-testid="stSidebar"] { background-color: var(--panel-bg); border-right: 1px solid var(--border-color); }
[data-testid="stSidebar"] h3 { font-size: .8rem; font-weight: 750; text-transform: uppercase; letter-spacing: 0.08em;
                               color: var(--muted-color); margin: 0.4rem 0 0.5rem 0; }
[data-testid="stSidebar"] label, [data-testid="stSidebar"] label p { font-size: .8rem !important; font-weight: 650 !important;
                               color: var(--text-color) !important; }
[data-testid="stSidebarCollapseButton"] svg, [data-testid="stSidebarCollapsedControl"] svg { color: var(--text-color) !important; }

/* ---------- inputs / dropdowns ---------- */
[data-baseweb="select"] > div, [data-baseweb="input"] > div {
    background-color: var(--panel-bg) !important; border: 1px solid var(--border-color) !important;
    border-radius: 8px !important; color: var(--text-color) !important; min-height: 38px; }
[data-baseweb="select"] > div:hover { border-color: var(--accent) !important; }
[data-baseweb="select"] span, [data-baseweb="select"] input, [data-baseweb="select"] div,
[data-baseweb="input"] input { color: var(--text-color) !important; }
[data-baseweb="select"] input::placeholder, [data-baseweb="input"] input::placeholder { color: var(--muted-color) !important; opacity: 1; }
[data-baseweb="select"] svg { fill: var(--muted-color) !important; color: var(--muted-color) !important; }
[data-baseweb="popover"] > div, [data-baseweb="popover"] [data-baseweb="menu"], ul[role="listbox"] {
    background-color: var(--panel-bg) !important; color: var(--text-color) !important;
    border: 1px solid var(--border-color); border-radius: 10px; }
li[role="option"], li[role="option"] * { background-color: transparent !important; color: var(--text-color) !important; }
li[role="option"]:hover, li[role="option"][aria-selected="true"] { background-color: var(--chip-bg) !important; }

/* Streamlit 1.5x+ (react-aria) dropdowns */
.react-aria-ComboBox [role="group"], .react-aria-Select button {
    background-color: var(--panel-bg) !important; border: 1px solid var(--border-color) !important;
    border-radius: 8px !important; color: var(--text-color) !important; }
.react-aria-ComboBox [role="group"][data-hovered], .react-aria-ComboBox [role="group"][data-focus-within] {
    border-color: var(--accent) !important; }
.react-aria-ComboBox input { color: var(--text-color) !important; -webkit-text-fill-color: var(--text-color); }
.react-aria-ComboBox input::placeholder { color: var(--muted-color) !important; -webkit-text-fill-color: var(--muted-color); opacity: 1; }
.react-aria-ComboBox button, .react-aria-ComboBox button svg { color: var(--muted-color) !important; }
[data-testid$="Dropdown"], .react-aria-Popover, [data-testid$="Dropdown"] [role="listbox"] {
    background-color: var(--panel-bg) !important; color: var(--text-color) !important;
    border-color: var(--border-color) !important; }
[role="listbox"] [role="option"] { color: var(--text-color) !important; background-color: transparent !important; }
[role="listbox"] [role="option"] * { color: inherit !important; }
[role="listbox"] [role="option"][data-focused], [role="listbox"] [role="option"][data-hovered] { background-color: var(--chip-bg) !important; }
[role="listbox"] [role="option"][aria-selected="true"] { color: var(--accent) !important; font-weight: 700; }
[data-tag] { background-color: var(--chip-bg) !important; color: var(--text-color) !important;
             border: 1px solid var(--border-color) !important; border-radius: 6px !important; max-width: 100%; }
[data-tag] *, [data-tag] svg { color: var(--text-color) !important; fill: currentColor; }
[data-tag] span { font-size: .76rem !important; font-weight: 600 !important; }
/* selected tags wrap onto new lines instead of being cut off */
.react-aria-ComboBox [role="group"] { height: auto !important; min-height: 38px; }
[data-testid="stMultiSelectTagsContainer"] { flex-wrap: wrap !important; max-height: 112px; overflow-x: hidden !important;
                                             overflow-y: auto !important; }
[data-testid="stMultiSelectTagsContainer"] > span[role="group"] { flex-wrap: wrap !important; max-width: 100%; }

/* toggle switch (Streamlit 1.5x+) */
label:has(input[role="switch"]) > span + div { background-color: var(--chip-bg) !important; border: 1px solid var(--border-color); }
label[data-selected]:has(input[role="switch"]) > span + div { background-color: var(--accent) !important; border-color: var(--accent); }
label:has(input[role="switch"]) > span + div > div { background-color: #ffffff !important; }
[data-testid="stTooltipIcon"] svg, [data-testid="stTooltipHoverTarget"] svg { opacity: 1; color: var(--muted-color) !important; }

/* ---------- FLO splash (logo while data loads) ---------- */
.flo-splash { position: fixed; inset: 0; z-index: 1000000; background: var(--app-bg); display: flex;
              flex-direction: column; align-items: center; justify-content: center; gap: 22px; padding: 24px;
              opacity: 1 !important; }
.flo-splash img { max-width: min(240px, 55vw); max-height: 150px; object-fit: contain;
                  animation: flo-pulse 1.8s ease-in-out infinite; }
.flo-splash .word { font-size: clamp(2.6rem, 9vw, 4rem); font-weight: 900; letter-spacing: .14em; color: var(--text-color);
                    animation: flo-pulse 1.8s ease-in-out infinite; line-height: 1; }
.flo-splash .bar { width: min(260px, 64vw); height: 4px; border-radius: 4px; background: var(--chip-bg);
                   overflow: hidden; position: relative; }
.flo-splash .bar::after { content: ""; position: absolute; top: 0; left: -40%; width: 40%; height: 100%;
                          background: var(--accent); border-radius: 4px; animation: flo-slide 1.1s ease-in-out infinite; }
[data-testid="stElementContainer"]:has(.flo-splash), [data-testid="stMarkdown"]:has(.flo-splash) { opacity: 1 !important; }
.flo-splash .msg { color: var(--muted-color); font-size: .9rem; text-align: center; line-height: 1.5; }
@keyframes flo-slide { to { left: 100%; } }
@keyframes flo-pulse { 0%, 100% { opacity: 1; transform: scale(1); } 50% { opacity: .78; transform: scale(.97); } }

/* ---------- month filter (button + panel) ---------- */
.mf-label { font-size: .8rem; font-weight: 650; color: var(--text-color); line-height: 1.6; margin: 0 0 8px 0;
            white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
[data-testid="stPopover"], [data-testid="stPopover"] > div { width: 100%; }
[data-testid="stPopoverButton"] { width: 100%; min-height: 40px; justify-content: space-between !important;
    background-color: var(--panel-bg) !important; color: var(--text-color) !important;
    border: 1px solid var(--border-color) !important; border-radius: 8px !important; padding: 0 10px !important; }
[data-testid="stPopoverButton"]:hover, [data-testid="stPopoverButton"]:focus-visible { border-color: var(--accent) !important; }
[data-testid="stPopoverButton"] > div { width: 100%; display: flex; align-items: center; justify-content: space-between;
    gap: 6px; min-width: 0; }
[data-testid="stPopoverButton"] > div > div:first-child { flex: 1 1 auto; min-width: 0; display: flex; justify-content: flex-start; }
[data-testid="stPopoverButton"] [data-testid="stMarkdownContainer"], [data-testid="stPopoverButton"] [data-testid="stMarkdownContainer"] p {
    text-align: left !important; width: 100%; }
[data-testid="stPopoverButton"] p, [data-testid="stPopoverButton"] div { color: var(--text-color) !important; text-align: left;
    font-weight: 500; font-size: .88rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
[data-testid="stPopoverButton"] svg { color: var(--muted-color) !important; flex-shrink: 0; }
[data-testid="stPopoverBody"] { background-color: var(--panel-bg) !important; color: var(--text-color) !important;
    border: 1px solid var(--border-color) !important; border-radius: 12px !important; box-shadow: var(--shadow);
    min-width: min(340px, 92vw); }
[data-testid="stPopoverBody"] p, [data-testid="stPopoverBody"] label, [data-testid="stPopoverBody"] span,
[data-testid="stPopoverBody"] [data-testid="stWidgetLabel"] p { color: var(--text-color); }
[data-testid="stPopoverBody"] [data-testid="stWidgetLabel"] p { font-weight: 650; font-size: .8rem; }
[data-testid="stPopoverBody"] [data-testid="stCaptionContainer"],
[data-testid="stPopoverBody"] [data-testid="stCaptionContainer"] p { color: var(--muted-color) !important; }
[data-testid="stPopoverBody"] [role="radiogroup"] { gap: 4px 14px; flex-wrap: wrap; }
[data-testid="stRadioOption"] > div > div:first-child { background-color: var(--panel-bg) !important;
    border-color: var(--muted-color) !important; }
[data-testid="stRadioOption"] > div > div:first-child > div { background-color: var(--panel-bg) !important; }
[data-testid="stRadioOption"][data-selected] > div > div:first-child { background-color: var(--accent) !important;
    border-color: var(--accent) !important; }
[data-testid="stRadioOption"][data-selected] > div > div:first-child > div { background-color: #ffffff !important; }
.stMultiSelect [data-baseweb="tag"] { background-color: var(--chip-bg) !important; border: 1px solid var(--border-color) !important;
    border-radius: 6px !important; padding: 1px 6px !important; margin: 2px !important; max-width: 100%; }
.stMultiSelect [data-baseweb="tag"] span, .stMultiSelect [data-baseweb="tag"] svg { color: var(--text-color) !important;
    font-size: 0.76rem !important; font-weight: 600 !important; fill: var(--text-color) !important; }
[data-testid="stTooltipIcon"] svg, [data-testid="stTooltipHoverTarget"] svg { color: var(--muted-color) !important; }

/* ---------- toggle / checkbox ---------- */
[data-testid="stToggle"] label > div:first-child, [data-baseweb="checkbox"] > div:first-child { border-color: var(--border-color) !important; }

/* ---------- buttons / expander / tabs ---------- */
.stButton > button, .stDownloadButton > button {
    background-color: var(--panel-bg); color: var(--text-color); border: 1px solid var(--border-color);
    border-radius: 8px; font-weight: 650; min-height: 38px; }
.stButton > button p, .stDownloadButton > button p { color: inherit; font-size: .82rem; white-space: nowrap; }
.stButton > button, .stDownloadButton > button { padding-left: 10px; padding-right: 10px; }
.stButton > button:hover, .stDownloadButton > button:hover { border-color: var(--accent); color: var(--accent); background: var(--card-bg); }
.stButton > button:focus:not(:active), .stDownloadButton > button:focus:not(:active) { color: var(--text-color); border-color: var(--accent); }
[data-testid="stExpander"] { background: var(--panel-bg); border: 1px solid var(--border-color); border-radius: 12px; }
[data-testid="stExpander"] details, [data-testid="stExpander"] summary { border: none; background: transparent; }
[data-testid="stExpander"] summary:hover p, [data-testid="stExpander"] summary:hover svg { color: var(--accent) !important; }
[data-testid="stExpander"] summary p, [data-testid="stExpander"] summary span, [data-testid="stExpander"] summary svg {
    color: var(--text-color) !important; font-weight: 650; }
[data-testid="stTabs"] [role="tablist"] { gap: 4px; flex-wrap: wrap; border-bottom: 1px solid var(--border-color); }
[data-testid="stTabs"] [role="tab"] { border-radius: 8px 8px 0 0; padding: 10px 16px; background: transparent; height: auto; }
[data-testid="stTabs"] [role="tab"] p { color: var(--muted-color) !important; font-weight: 700; font-size: .92rem; white-space: nowrap; }
[data-testid="stTabs"] [role="tab"]:hover { background: var(--chip-bg); }
[data-testid="stTabs"] [role="tab"]:hover p { color: var(--text-color) !important; }
[data-testid="stTabs"] [role="tab"][aria-selected="true"] { background: var(--panel-bg); }
[data-testid="stTabs"] [role="tab"][aria-selected="true"] p { color: var(--accent) !important; }
[data-baseweb="tab-highlight"], .react-aria-SelectionIndicator { background-color: var(--accent) !important; }
[data-baseweb="tab-border"] { background-color: var(--border-color) !important; }

/* ---------- cards (charts) ---------- */
[class*="st-key-card_"], [data-testid="stVerticalBlockBorderWrapper"]:has(.chart-title) {
    background: var(--panel-bg) !important; border: 1px solid var(--border-color) !important;
    border-radius: 12px !important; box-shadow: var(--shadow); padding: 12px 14px 6px 14px !important; }
.chart-title { font-size: .82rem; font-weight: 800; letter-spacing: .02em; text-transform: uppercase;
               color: var(--text-color); line-height: 1.35; margin: 0 0 2px 0; overflow-wrap: anywhere; }
.chart-sub { font-size: .74rem; color: var(--muted-color); margin: 0 0 4px 0; line-height: 1.3; }
[data-testid="stPlotlyChart"] { background: transparent; }

/* ---------- KPI tiles ---------- */
.kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(165px, 1fr)); gap: 12px; margin: 6px 0 12px 0; }
.kpi { background: var(--panel-bg); border: 1px solid var(--border-color); border-top: 4px solid var(--tone);
       border-radius: 12px; padding: 12px 14px; box-shadow: var(--shadow); min-width: 0;
       background-image: linear-gradient(180deg, var(--tone-soft) 0%, transparent 85%); }
.kpi-label { color: var(--muted-color); font-size: .7rem; font-weight: 750; text-transform: uppercase; letter-spacing: .04em;
             line-height: 1.3; overflow-wrap: anywhere; }
.kpi-value { color: var(--text-color); font-weight: 800; font-size: 1.32rem; line-height: 1.25; margin-top: 6px;
             overflow-wrap: anywhere; }

/* ---------- tables ---------- */
.tbl-wrap { border: 1px solid var(--border-color); border-radius: 12px; overflow: auto; background: var(--panel-bg);
            box-shadow: var(--shadow); margin-bottom: 6px; }
.tbl-wrap table { border-collapse: separate; border-spacing: 0; width: 100%; font-size: .8rem; color: var(--text-color); margin: 0; }
.tbl-wrap th { position: sticky; top: 0; z-index: 1; background: var(--th-bg); color: var(--th-text); font-weight: 700;
               text-transform: uppercase; font-size: .7rem; letter-spacing: .03em; padding: 9px 10px; text-align: right;
               border: none; white-space: normal; line-height: 1.25; vertical-align: bottom; min-width: 64px; }
.tbl-wrap th:first-child, .tbl-wrap td:first-child { text-align: left; min-width: 110px; }
.tbl-wrap td { padding: 7px 10px; text-align: right; border: none; border-bottom: 1px solid var(--border-color);
               white-space: nowrap; font-variant-numeric: tabular-nums; color: var(--text-color); }
.tbl-wrap td:first-child { white-space: normal; font-weight: 600; overflow-wrap: anywhere; }
.tbl-wrap tbody tr:nth-child(even) td { background: var(--row-alt); }
.tbl-wrap tbody tr:hover td { background: var(--row-hover); }
.tbl-wrap tr.tot td { background: var(--total-bg) !important; color: var(--total-text); font-weight: 800;
                      position: sticky; bottom: 0; border-top: 1px solid var(--accent); }
.tbl-wrap tr.ref td { font-style: italic; font-weight: 700; color: var(--muted-color); }
.tbl-wrap td.r-good { background: var(--good-bg) !important; color: var(--good-tx); font-weight: 700; }
.tbl-wrap td.r-mid { background: var(--mid-bg) !important; color: var(--mid-tx); font-weight: 700; }
.tbl-wrap td.r-bad { background: var(--bad-bg) !important; color: var(--bad-tx); font-weight: 700; }
.tbl-wrap td.zero { color: var(--muted-color); }
.tbl-wrap th.key { box-shadow: inset 0 -3px 0 var(--accent); }
.tbl-wrap th.key .arw { font-size: .62rem; margin-left: 3px; opacity: .9; }
.tbl-wrap td.key { font-weight: 700; background-repeat: no-repeat; }

/* ---------- notes ---------- */
.note { border: 1px solid var(--border-color); border-left: 4px solid var(--accent); background: var(--panel-bg);
        color: var(--text-color); border-radius: 10px; padding: 9px 12px; font-size: .84rem; margin: 6px 0 10px 0;
        line-height: 1.45; overflow-wrap: anywhere; }
.note-success { border-left-color: var(--good-tx); }
.note-warning { border-left-color: var(--mid-tx); }
.note-error { border-left-color: var(--bad-tx); }

/* ---------- titles / hero / badges ---------- */
.dashboard-hero { background: linear-gradient(135deg, __HERO_GRAD__); border: 1px solid var(--border-color);
    border-radius: 14px; padding: 16px 22px; margin-bottom: 14px; box-shadow: var(--shadow); }
.dashboard-hero .hero-t { margin: 0; padding: 0; font-size: 1.4rem; font-weight: 800; letter-spacing: -0.01em; color: var(--text-color);
                     line-height: 1.3; }
.dashboard-hero p { color: var(--muted-color); margin: 4px 0 0 0; font-size: .86rem; line-height: 1.5; }
.dash-title { background: var(--title-bg); color: var(--title-text); border: 1px solid var(--border-color); border-left: 5px solid var(--accent);
              border-radius: 10px; padding: 11px 16px; margin: 6px 0 12px 0; display: flex; flex-wrap: wrap; align-items: center;
              justify-content: space-between; gap: 6px 14px; }
.dash-title .t { font-size: 1.02rem; font-weight: 800; letter-spacing: .03em; text-transform: uppercase; line-height: 1.35; }
.chip { display: inline-block; background: var(--chip-bg); color: var(--text-color); padding: 3px 10px; border-radius: 20px;
        font-size: .74rem; font-weight: 650; white-space: nowrap; }
.dash-title .chip { background: rgba(255, 255, 255, 0.12); color: var(--title-text); }
.section-badge { display: inline-flex; align-items: center; background: var(--chip-bg); color: var(--text-color);
    padding: 5px 12px; border-radius: 6px; font-size: 0.78rem; font-weight: 750; margin: 8px 0 10px 0;
    text-transform: uppercase; letter-spacing: .03em; line-height: 1.35; max-width: 100%; overflow-wrap: anywhere; }
code { background: var(--chip-bg) !important; color: var(--text-color) !important; }
[data-testid="stAlert"] { border-radius: 10px; }
__DARK_EXTRA__
</style>
"""

DARK_EXTRA_CSS = """
[data-testid="stAlert"] { background-color: var(--card-bg) !important; border: 1px solid var(--border-color); }
[data-testid="stAlert"] *, [data-testid="stAlert"] p { color: var(--text-color) !important; }
"""


def _streamlit_theme_is_dark():
    """Streamlit's own (browser/system) theme - used only as the default for the Dark mode toggle."""
    try:
        return str(st.context.theme.type).lower() == "dark"
    except Exception:
        return False


def _hex_to_rgba(hex_color, alpha):
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r}, {g}, {b}, {alpha})"


def apply_theme():
    """Sidebar dark-mode toggle + global CSS + chart colours."""
    global COLORS, PALETTE, PLOT_TEMPLATE, PLOT_TEXT, PLOT_MUTED, PLOT_GRID, HEAT_SCALE, IS_DARK
    if "dashboard_dark_mode" not in st.session_state:
        st.session_state.dashboard_dark_mode = _streamlit_theme_is_dark()

    with st.sidebar:
        st.markdown("### 🎨 Preferences")
        st.toggle("Dark mode", key="dashboard_dark_mode", help="Dashboard appearance mode toggle")

    IS_DARK = bool(st.session_state.dashboard_dark_mode)
    mode = "dark" if IS_DARK else "light"
    c = THEMES[mode]
    COLORS = dict(COLOR_SETS[mode])
    PALETTE = [COLORS[k] for k in PALETTE_ORDER]
    if IS_DARK:
        PLOT_TEMPLATE, PLOT_TEXT, PLOT_MUTED, PLOT_GRID = "plotly_dark", "#e6edf7", "#9aabc6", "#26344f"
        HEAT_SCALE = [[0.0, "#1b2640"], [0.5, "#2b5aa8"], [1.0, "#7cb8ff"]]
        hero_grad = "#16213a 0%, #1c2d50 100%"
    else:
        PLOT_TEMPLATE, PLOT_TEXT, PLOT_MUTED, PLOT_GRID = "plotly_white", "#0f172a", "#475569", "#e5e9f0"
        HEAT_SCALE = "Blues"
        hero_grad = "#ffffff 0%, #eaf1ff 100%"

    css = CSS_TEMPLATE
    for k, v in c.items():
        css = css.replace(f"__{k}__", v)
    css = css.replace("__HERO_GRAD__", hero_grad).replace("__DARK_EXTRA__", DARK_EXTRA_CSS if IS_DARK else "")
    st.markdown(css, unsafe_allow_html=True)


# -----------------------------------------------------------------------------
# 2. HELPER UTILITIES & DATA SANITIZATION
# -----------------------------------------------------------------------------
def col_to_idx(letter):
    n = 0
    for c in letter.upper():
        n = n * 26 + ord(c) - 64
    return n - 1


def norm_key(x):
    return re.sub(r"[^A-Z0-9]", "", str(x).upper())


def num(s):
    """Convert a Series (numbers or text like '1,234', '(50)', '12%', '5 Days') to float; blanks -> 0."""
    if not isinstance(s, pd.Series):
        s = pd.Series(s)
    out = pd.to_numeric(s, errors="coerce").astype(float)
    need = out.isna() & s.notna()
    if need.any():
        text = s[need].astype(str).str.replace("\xa0", "", regex=False).str.strip()
        neg = text.str.match(r"^\(.*\)$") | text.str.startswith("−")
        cleaned = text.str.replace(r"[^0-9.\-]", "", regex=True)
        cleaned = cleaned.where(cleaned.str.count(r"\.") <= 1, "")
        val = pd.to_numeric(cleaned, errors="coerce")
        val = val.where(~neg, -val.abs())
        out.loc[need] = val
    return out.fillna(0).astype(float)


def clean_delay_days(s):
    parsed = num(s)
    # Blank-date formulas (e.g. TODAY()-blank) produce huge numbers -> treat as 0
    return parsed.mask(parsed.abs() > 1000, 0)


def clean_text(s):
    t = s.astype("string").fillna("")
    t = t.str.replace("\xa0", " ", regex=False).str.replace(r"\s+", " ", regex=True).str.strip()
    t = t.mask(t.str.lower().isin(["nan", "none", "<na>", "#n/a", "#ref!", "#value!"]), "")
    return t.astype(str)


def _parse_one_date(v):
    for dayfirst in (True, False):
        try:
            d = pd.to_datetime(v, dayfirst=dayfirst, errors="coerce")
        except Exception:
            d = pd.NaT
        if pd.notna(d):
            return d
    return pd.NaT


def parse_dates(s):
    """Handles Google Sheets serial numbers (unformatted) and text dates (dd/mm/yyyy first)."""
    s = pd.Series(s)
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    numeric = pd.to_numeric(s, errors="coerce")
    serial = numeric.between(20000, 80000)  # valid sheet serials ~1954..2119
    if serial.any():
        out.loc[serial] = pd.to_datetime(numeric[serial].astype(float), unit="D", origin="1899-12-30")
    text = s.astype(str).str.strip()
    rest = (~serial) & numeric.isna() & text.ne("") & ~text.str.lower().isin(["nan", "none", "<na>"])
    if rest.any():
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            out.loc[rest] = pd.to_datetime(text[rest].apply(_parse_one_date), errors="coerce")
    return out


def month_label(dts):
    lab = pd.Series("(NO DATE)", index=dts.index, dtype=object)
    valid = dts.notna()
    lab[valid] = [f"{MONTH_NAME_BY_NUM[d.month]}-{d.year % 100:02d}" for d in dts[valid]]
    return lab


def month_sort_key(label):
    m = re.match(r"\s*([A-Za-z]+)[\s\-']*(\d{2,4})?", str(label))
    if not m:
        return (9999, 99)
    mon = MONTH_NUM.get(m.group(1)[:3].lower(), 99)
    yr = int(m.group(2)) if m.group(2) else 0
    if 0 < yr < 100:
        yr += 2000
    return (yr if yr else 9998, mon)


def fmt(x):
    return f"{x:,.0f}"


def fmt_kpi(value, kind):
    if kind == "orders":
        return f"{value:,.0f} Orders"
    if kind == "pcs":
        return f"{value:,.0f} Pcs"
    if kind == "days":
        return f"{value:.1f} Days"
    if kind == "pct":
        return f"{value:.1f}%"
    if kind == "text":
        return str(value)
    return f"{value:,.0f}"


def _esc(v):
    return html.escape(str(v)).replace("$", "&#36;")


def _html(markup):
    st.markdown(markup, unsafe_allow_html=True)


def metrics(items):
    """KPI tiles. items = [(label, value_text), ...]. Text wraps, so long values never overlap or get cut."""
    tiles = []
    for i, (label, value) in enumerate(items):
        tone = COLORS[KPI_TONES[i % len(KPI_TONES)]]
        style = f"--tone:{tone};--tone-soft:{_hex_to_rgba(tone, 0.16 if IS_DARK else 0.09)};"
        tiles.append(f'<div class="kpi" style="{style}"><div class="kpi-label">{_esc(label)}</div>'
                     f'<div class="kpi-value">{_esc(value)}</div></div>')
    _html('<div class="kpi-grid">' + "".join(tiles) + "</div>")


def render_kpis(kpis, per_row=None):
    """kpis: list of (label, sheet_label, value, kind)."""
    metrics([(k[0], fmt_kpi(k[2], k[3])) for k in kpis])


def badge(text):
    _html(f'<div class="section-badge">{_esc(text)}</div>')


def dash_title(text, chip=None):
    chip_html = f'<span class="chip">{_esc(chip)}</span>' if chip else ""
    _html(f'<div class="dash-title"><span class="t">{_esc(text)}</span>{chip_html}</div>')


def notice(text, kind="info"):
    icons = {"info": "ℹ️", "success": "✅", "warning": "⚠️", "error": "❌"}
    _html(f'<div class="note note-{kind}">{icons.get(kind, "")} {_esc(text)}</div>')


def style_fig(fig, height=360, legend="top"):
    """Chart look for the active mode. Titles are drawn outside the plot (see chart()), so nothing overlaps."""
    if legend == "bottom":
        leg = dict(orientation="h", yanchor="top", y=-0.04, xanchor="center", x=0.5, title_text="")
    elif legend == "none":
        leg = dict(title_text="")
    elif legend == "right":
        leg = dict(orientation="v", yanchor="middle", y=0.5, xanchor="left", x=1.02, title_text="")
    else:
        leg = dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, title_text="")
    leg.update(font=dict(size=11, color=PLOT_TEXT), bgcolor="rgba(0,0,0,0)")
    fig.update_layout(
        template=PLOT_TEMPLATE,
        title=None,
        height=height,
        margin=dict(l=8, r=8, t=30 if legend == "top" else 10, b=8),
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend=leg,
        showlegend=(legend != "none"),
        font=dict(size=11, color=PLOT_TEXT),
        hoverlabel=dict(font_size=12),
        uniformtext=dict(minsize=9, mode="hide"),
    )
    fig.update_xaxes(gridcolor=PLOT_GRID, linecolor=PLOT_GRID, zerolinecolor=PLOT_GRID, automargin=True,
                     tickfont=dict(color=PLOT_MUTED), title_font=dict(color=PLOT_MUTED))
    fig.update_yaxes(gridcolor=PLOT_GRID, linecolor=PLOT_GRID, zerolinecolor=PLOT_GRID, automargin=True,
                     tickfont=dict(color=PLOT_MUTED), title_font=dict(color=PLOT_MUTED))
    return fig


def _card(key):
    try:
        return st.container(border=True, key=key)
    except TypeError:          # older Streamlit without `key`
        return st.container(border=True)


def _slug(text):
    return re.sub(r"[^a-z0-9]+", "_", str(text).lower()).strip("_")[:60] or "chart"


def chart(fig, title, height=360, ns="c", legend="top", subtitle=None):
    """Chart inside a bordered card: HTML title on top (wraps on small screens), plot below."""
    with _card(f"card_{ns}_{_slug(title)}"):
        _html(f'<div class="chart-title">{_esc(title)}</div>'
              + (f'<div class="chart-sub">{_esc(subtitle)}</div>' if subtitle else ""))
        st.plotly_chart(style_fig(fig, height, legend), theme=None, config={"displaylogo": False}, **STRETCH)


def empty_card(title, text, ns="c"):
    with _card(f"card_{ns}_{_slug(title)}"):
        _html(f'<div class="chart-title">{_esc(title)}</div>')
        notice(text)


def add_total(df, label_col, label="TOTAL"):
    tot = {c: (df[c].sum() if pd.api.types.is_numeric_dtype(df[c]) else "") for c in df.columns}
    tot[label_col] = label
    return pd.concat([df, pd.DataFrame([tot])], ignore_index=True)


TOTAL_LABELS = {"TOTAL", "GRAND TOTAL", "ALL FABRICATORS"}
RATE_COLS = {"Approval %", "RCVD %"}     # coloured green / amber / red like the sheet


def _rate_class(v):
    if v >= 50:
        return "r-good"
    if v >= 25:
        return "r-mid"
    return "r-bad"


def _is_pinned(label):
    t = str(label).strip().upper()
    return t in TOTAL_LABELS or t.startswith("ALL OVER")


def order_rows(df, key_col=None, sort=True, sort_abs=False):
    """Rows sorted by the important column (high -> low); TOTAL / ALL OVER rows stay at the bottom.
    sort=False keeps the existing (month / timeline) order."""
    if df is None or df.empty or not key_col or key_col not in df.columns or not sort:
        return df
    pinned = df.iloc[:, 0].map(_is_pinned)
    body, tail = df[~pinned], df[pinned]
    vals = pd.to_numeric(body[key_col], errors="coerce")
    if sort_abs:
        vals = vals.abs()
    order = vals.fillna(-np.inf).sort_values(ascending=False, kind="mergesort").index
    return pd.concat([body.loc[order], tail], ignore_index=True)


def show_table(df, pct_cols=(), dec_cols=(), height=None, money_cols=(), rate_cols=None, rate_fn=None,
               key_col=None, sort=True, sort_abs=False):
    """Theme-aware HTML table (sticky header, TOTAL row highlighted, scrolls instead of overlapping).
    key_col = important column: rows sorted by it (high -> low, unless sort=False) and shown with colour bars."""
    if df is None or df.empty:
        notice("Selected filters ke liye data nahi hai.")
        return
    if key_col not in df.columns:
        key_col = None
    df = order_rows(df, key_col, sort, sort_abs)
    cols = list(df.columns)
    numeric = {c: pd.api.types.is_numeric_dtype(df[c]) for c in cols}
    bar_max = 0.0
    if key_col and numeric[key_col]:
        kv = pd.to_numeric(df.loc[~df.iloc[:, 0].map(_is_pinned), key_col], errors="coerce").abs()
        bar_max = float(kv.max()) if kv.notna().any() else 0.0

    def _th(c):
        if c != key_col:
            return f"<th>{_esc(c)}</th>"
        tip = "Is column ke hisaab se bada → chhota" if sort else "Is column ko rang se highlight kiya gaya hai"
        arrow = '<span class="arw">▼</span>' if sort else ""
        return f'<th class="key" title="{tip}">{_esc(c)}{arrow}</th>'
    head = "".join(_th(c) for c in cols)
    body = []
    rate_cols = RATE_COLS if rate_cols is None else set(rate_cols)
    rate_fn = rate_fn or _rate_class
    value_cols = [c for c in cols[1:] if numeric[c] and c not in pct_cols]
    for _, row in df.iterrows():
        first = str(row.iloc[0]).strip().upper()
        has_data = any(float(row[c] or 0) != 0 for c in value_cols if pd.notna(row[c])) if value_cols else True
        cls = "tot" if first in TOTAL_LABELS else ("ref" if first.startswith("ALL OVER") else "")
        cells = []
        for c in cols:
            v = row[c]
            klass = ""
            if numeric[c] and pd.notna(v):
                fv = float(v)
                if c in pct_cols:
                    txt = f"{fv:.1f}%"
                    if c in rate_cols and cls != "ref" and has_data:
                        klass = rate_fn(fv)
                elif c in money_cols:
                    txt = inr(fv)
                elif c in dec_cols:
                    txt = f"{fv:,.2f}"
                else:
                    txt = f"{fv:,.0f}"
                if fv == 0 and not klass:
                    klass = "zero"
            else:
                txt = "" if (v is None or (isinstance(v, float) and np.isnan(v))) else str(v)
            if c == key_col and not cls:
                klass = (klass + " key").strip()
                if bar_max > 0 and numeric[c] and pd.notna(v) and float(v) != 0:
                    w = min(100.0, abs(float(v)) / bar_max * 100)
                    bar = "var(--bar-neg)" if float(v) < 0 else "var(--bar)"
                    cells.append(f'<td class="{klass}" style="background-image:linear-gradient(90deg,{bar} {w:.1f}%,'
                                 f'transparent {w:.1f}%)">{_esc(txt)}</td>')
                    continue
            cells.append(f'<td class="{klass}">{_esc(txt)}</td>' if klass else f"<td>{_esc(txt)}</td>")
        body.append(f'<tr class="{cls}">' + "".join(cells) + "</tr>" if cls else "<tr>" + "".join(cells) + "</tr>")
    style = f' style="max-height:{int(height)}px"' if height else ""
    _html(f'<div class="tbl-wrap"{style}><table><thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>')


class SheetAccessError(Exception):
    pass



# -----------------------------------------------------------------------------
# 3. GOOGLE SHEETS AUTHENTICATION & LOADERS
# -----------------------------------------------------------------------------
def show_access_error(e=None):
    email = ""
    try:
        email = st.secrets["gcp_service_account"]["client_email"]
    except Exception:
        pass
    notice("Google Sheet open nahi ho payi. Sheet ko is service account ke saath share karein: "
           + (email if email else "(secrets wala client_email)")
           + (f"  ·  Details: {e}" if e else ""), "error")


@st.cache_resource(show_spinner=False)
def get_client():
    if "gcp_service_account" not in st.secrets:
        st.error("Google credentials missing hain. Streamlit Settings → Secrets me add karein.")
        st.stop()

    creds_dict = dict(st.secrets["gcp_service_account"])
    if "private_key" in creds_dict:
        creds_dict["private_key"] = str(creds_dict["private_key"]).replace("\\n", "\n")

    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=[
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ],
    )
    return gspread.authorize(creds)


@st.cache_resource(ttl=BOOK_TTL, show_spinner=False)
def _book_cached(sheet_id):
    """Opening a sheet costs one Google request - done once per hour per sheet instead of on every fetch."""
    return get_client().open_by_key(sheet_id)


def _open_book(sheet_id):
    try:
        return _book_cached(sheet_id)
    except Exception as e:
        # Raised (not st.stop) so one failing sheet does not blank the other tabs.
        raise SheetAccessError(str(e)) from e


def _api(fn, *args, **kwargs):
    """Google API call -> SheetAccessError on failure (shown inside its own tab). Also forgets the opened sheets
    so the next try starts fresh (e.g. sheet re-shared, token expired)."""
    try:
        return fn(*args, **kwargs)
    except SheetAccessError:
        raise
    except Exception as e:
        try:
            _book_cached.clear()
        except Exception:
            pass
        raise SheetAccessError(str(e)) from e


def _a1_tab(title):
    return "'" + str(title).replace("'", "''") + "'"


@st.cache_data(ttl=TAB_TTL, show_spinner=False)
def list_tabs(sheet_id):
    return [ws.title for ws in _api(_open_book(sheet_id).worksheets)]


@st.cache_data(ttl=LIVE_TTL, show_spinner="Google Sheet se live data load ho raha hai...")
def load_tab_raw(sheet_id, tab_title):
    """Unformatted values: numbers as numbers, dates as serial numbers (no locale/format parsing errors)."""
    book = _open_book(sheet_id)
    resp = _api(book.values_get, _a1_tab(tab_title), params={
        "valueRenderOption": "UNFORMATTED_VALUE",
        "dateTimeRenderOption": "SERIAL_NUMBER",
    })
    return resp.get("values", [])


def find_tab(tabs, wanted):
    w = norm_key(wanted)
    for t in tabs:
        if norm_key(t) == w:
            return t
    return None


# -----------------------------------------------------------------------------
# 4. MASTER SHEET – DATA PREPARATION
# -----------------------------------------------------------------------------
CATEGORY_ALIASES = {norm_key(c): c for c in CATEGORIES_LIST}
CATEGORY_ALIASES.update({
    "COORD": "CO-ORD SET", "COORDSETS": "CO-ORD SET", "CORDSET": "CO-ORD SET",
    "KURTAPANTS": "KURTA PANT", "KURTAPANTSET": "KURTA PANT",
    "TOPS": "TOP",
    "KURTAPANTDUPPATA": "KURTA PANT DUPATTA", "KURTAPANTSDUPATTA": "KURTA PANT DUPATTA",
    "KURTAPANTDUPATTASET": "KURTA PANT DUPATTA",
    "DRESSES": "DRESS", "KAFTANS": "KAFTAN", "KURTAS": "KURTA",
})


def canon_category(v):
    v = str(v).strip()
    if not v:
        return ""
    return CATEGORY_ALIASES.get(norm_key(v), v.upper())


def build_raw(values):
    """Returns data rows (index = actual sheet row number) and the first data row number."""
    needed_width = col_to_idx("AX") + 1

    def is_row_no(v):
        t = str(v).strip()
        return bool(re.fullmatch(r"\d+(\.0+)?", t)) and float(t) > 0

    first = None
    for i, r in enumerate(values):
        if i < 2 or not r:          # row 1 = title, row 2 = totals row
            continue
        if is_row_no(r[0]) and len(r) > 1 and str(r[1]).strip():
            first = i
            break
    if first is None:
        first = next((i for i, r in enumerate(values) if i >= 2 and r and is_row_no(r[0])),
                     min(4, len(values)))

    rows = values[first:]
    if not rows:
        return pd.DataFrame(), first + 1
    width = max([len(r) for r in rows] + [needed_width])
    df = pd.DataFrame([list(r) + [""] * (width - len(r)) for r in rows],
                      index=range(first + 1, first + 1 + len(rows)))

    b = df[col_to_idx("B")].astype(str).str.strip()
    j_val = num(df[col_to_idx("J")])
    first_cells = df[[0, 1, 2]].apply(lambda c: c.map(norm_key))
    is_total_row = first_cells.isin(["TOTAL", "GRANDTOTAL"]).any(axis=1)
    keep = ((b != "") | (j_val != 0)) & ~is_total_row
    return df[keep], first + 1


def prepare(raw):
    def col(key):
        i = col_to_idx(MASTER_COLS[key])
        if i in raw.columns:
            return raw[i]
        return pd.Series([""] * len(raw), index=raw.index, dtype=object)

    d = pd.DataFrame(index=raw.index)
    d.index.name = "sheet_row"
    d["date_client"] = parse_dates(col("date_client"))
    d["date_prod"] = parse_dates(col("date_prod"))
    d["designer"] = clean_text(col("designer")).str.upper().replace("", "(NOT ASSIGNED)")

    t = clean_text(col("type")).str.upper()
    d["type_raw"] = t
    d["order_type"] = np.select(
        [t.eq("NEW"), t.eq("REPEAT"), t.str.contains("REPEAT"), t.str.contains("NEW")],
        ["NEW", "REPEAT", "REPEAT", "NEW"], default="OTHER")

    d["category"] = clean_text(col("category")).map(canon_category).replace("", "(NO CATEGORY)")
    d["qty"] = num(col("qty"))
    d["client"] = clean_text(col("client")).str.upper().replace("", "(NO CLIENT)")
    d["fabricator"] = clean_text(col("fabricator")).str.upper().replace("", "(NOT ASSIGNED)")
    d["delay"] = clean_delay_days(col("delay"))
    d["production"] = num(col("production"))
    d["received"] = num(col("received"))
    d["pending"] = num(col("pending"))
    d["status"] = clean_text(col("status")).str.upper().replace("", "(BLANK)")
    d["month"] = month_label(d["date_client"])
    return d


def month_list(d):
    """12 FY months always, plus any other month labels that actually carry data (e.g. (NO DATE))."""
    active = d[(d["qty"] != 0) | (d["production"] != 0)]
    extra = [m for m in active["month"].unique() if m not in FY_MONTH_LABELS]
    return FY_MONTH_LABELS + sorted(extra, key=month_sort_key)


def opt_list(d, col, known=(), first=()):
    present = {v for v in d[col].unique() if v}
    rest = sorted((present | set(known)) - set(first))
    return ["All"] + list(first) + rest


def filter_data(d, f):
    x = d
    for field in ["month", "designer", "client", "fabricator", "status", "category", "order_type"]:
        v = f.get(field, "All")
        if isinstance(v, (list, tuple)):          # month filter: list of months ([] = all)
            if v:
                x = x[x[field].isin(v)]
        elif v != "All":
            x = x[x[field] == v]
    risk = f.get("risk", "All")
    if risk.startswith("Overdue"):
        x = x[x["delay"] > 0]
    elif risk.startswith("On-time"):
        x = x[x["delay"] <= 0]
    return x


def filter_row(spec, key_prefix):
    weights = [1.35 if field == "month" else 1 for field, _, _ in spec]   # month button shows a range text
    cols = st.columns(weights, vertical_alignment="bottom")
    out = {}
    for c, (field, label, options) in zip(cols, spec):
        if field == "month":
            out[field] = month_filter(c, label, [o for o in options if o != "All"], f"{key_prefix}_{field}",
                                      placeholder="All")
        else:
            out[field] = c.selectbox(label, options, key=f"{key_prefix}_{field}")
    return out


# --- KPI definitions: (display label, label used on the sheet dashboard, value, format) ---
def designer_kpis(x):
    return [
        ("TOTAL DESIGNS", "TOTAL DESIGNS", int((x["qty"] > 0).sum()), "int"),
        ("TOTAL QUANTITY (PCS)", "TOTAL QUANTITY (PCS)", x["qty"].sum(), "int"),
        ("NEW ORDER QTY", "NEW ORDER QTY", x.loc[x["order_type"] == "NEW", "qty"].sum(), "int"),
        ("REPEAT ORDER QTY", "REPEAT ORDER QTY", x.loc[x["order_type"] == "REPEAT", "qty"].sum(), "int"),
    ]


def client_kpis(x):
    return [
        ("TOTAL DESIGNS", "TOTAL DESIGNS", int((x["qty"] > 0).sum()), "int"),
        ("TOTAL QTY (PCS)", "TOTAL QTY (PCS)", x["qty"].sum(), "int"),
        ("IN PRODUCTION", "IN PRODUCTION", x["production"].sum(), "int"),
        ("RECEIVED / DONE", "RECEIVED / DONE", x["received"].sum(), "int"),
        ("TOTAL PENDING QTY", "TOTAL PENDING QTY", x["pending"].sum(), "int"),
        ("REPEAT ORDERS (PCS)", "REPEAT ORDERS (PCS)", x.loc[x["order_type"] == "REPEAT", "qty"].sum(), "int"),
        ("NEW ORDERS (PCS)", "NEW ORDERS (PCS)", x.loc[x["order_type"] == "NEW", "qty"].sum(), "int"),
        ("CATEGORY TOTAL (PCS)", "CATEGORY TOTAL (PCS)", x.loc[x["category"].isin(CATEGORIES_LIST), "qty"].sum(), "int"),
    ]


def production_kpis(x):
    base = x[x["production"] > 0]                 # an "order" = a design that is in production
    overdue = base[base["delay"] > 0]
    ontime = base[base["delay"] <= 0]
    n = len(base)
    return [
        ("TOTAL DESIGN", "TOTAL DESIGN", n, "orders"),
        ("INPRODUCTION PCS (AS)", "INPRODUCTION PCS (AS)", x["production"].sum(), "pcs"),
        ("RECEIVED PCS (AT)", "RECIEVED PCS (AT)", x["received"].sum(), "pcs"),
        ("PENDING PCS (AU)", "PENDING PCS (AU)", x["pending"].sum(), "pcs"),
        ("OVERDUE PCS (AH > 0)", "OVERDUE PCS (AH > 0)", overdue["production"].sum(), "pcs"),
        ("AVG OVERDUE DAYS", "AVG OVERDUE DAYS", overdue["delay"].mean() if len(overdue) else 0.0, "days"),
        ("ON-TIME DELIVERY %", "ON-TIME DELIVERY %", (len(ontime) / n * 100) if n else 0.0, "pct"),
        ("ON-TIME PCS (AH <= 0)", "ON-TIME PCS (AH <= 0)", ontime["production"].sum(), "pcs"),
    ]


# -----------------------------------------------------------------------------
# 5. MASTER SHEET – DASHBOARDS
# -----------------------------------------------------------------------------
def designer_dashboard(d_all, key):
    dash_title("👨‍🎨 Executive Designer Performance & Category Tracking Dashboard")
    badge("🔎 Interactive Designer Dashboard Filters")
    f = filter_row([
        ("month", "Select Month", ["All"] + month_list(d_all)),
        ("designer", "Select Designer", opt_list(d_all, "designer", known=KNOWN_DESIGNERS)),
        ("order_type", "Order Type", opt_list(d_all, "order_type", first=("NEW", "REPEAT"))),
    ], f"{key}_des")
    x = filter_data(d_all, f)

    # Designer rows: fixed list (like the sheet) + any designer with volume
    if f["designer"] != "All":
        designers = [f["designer"]]
    else:
        with_vol = {v for v in d_all.loc[d_all["qty"] > 0, "designer"].unique()}
        designers = sorted((set(KNOWN_DESIGNERS) | with_vol) - {"(NOT ASSIGNED)"})
        if "(NOT ASSIGNED)" in with_vol:
            designers.append("(NOT ASSIGNED)")

    total_qty = x["qty"].sum()
    rows = []
    for des in designers:
        sub = x[x["designer"] == des]
        vol = sub["qty"].sum()
        rows.append({
            "Designer Name": des,
            "New Orders (Pcs)": sub.loc[sub["order_type"] == "NEW", "qty"].sum(),
            "Repeat Orders (Pcs)": sub.loc[sub["order_type"] == "REPEAT", "qty"].sum(),
            "Total Volume (Pcs)": vol,
            "Share %": (vol / total_qty * 100) if total_qty else 0.0,
            "Total Design Count": int((sub["qty"] > 0).sum()),
        })
    g = pd.DataFrame(rows, columns=["Designer Name", "New Orders (Pcs)", "Repeat Orders (Pcs)",
                                    "Total Volume (Pcs)", "Share %", "Total Design Count"])

    kpis = designer_kpis(x)
    if not g.empty and g["Total Volume (Pcs)"].max() > 0:
        top = g.loc[g["Total Volume (Pcs)"].idxmax()]
        top_txt = f"{top['Designer Name']} ({fmt(top['Total Volume (Pcs)'])} Pcs)"
    else:
        top_txt = "—"
    metrics([(k[0], fmt_kpi(k[2], k[3])) for k in kpis] + [("🏆 TOP PERFORMER", top_txt)])

    st.markdown("---")
    badge("📊 Designer Volume & Share Breakdown")
    c1, c2 = st.columns([1.2, 1], gap="medium")
    with c1:
        show_table(add_total(g, "Designer Name"), pct_cols=("Share %",), key_col="Total Volume (Pcs)")
    with c2:
        melted = g.melt(id_vars="Designer Name", value_vars=["New Orders (Pcs)", "Repeat Orders (Pcs)"],
                        var_name="Order Type", value_name="Qty")
        fig = px.bar(melted, x="Designer Name", y="Qty", color="Order Type", barmode="group",
                     color_discrete_sequence=[COLORS["green"], COLORS["purple"]])
        fig.update_xaxes(title="")
        chart(fig, "Designer Performance: New vs Repeat Orders (Pcs)", 380, ns=f"{key}_des")

    st.markdown("---")
    badge("🧩 Category Wise Designer Breakdown (Pcs)")
    pv = x.groupby(["designer", "category"])["qty"].sum().unstack(fill_value=0)
    other_cats = [c for c in pv.columns if c not in CATEGORIES_LIST and pv[c].sum() != 0]
    cats = CATEGORIES_LIST + other_cats
    pv = pv.reindex(index=designers, columns=cats, fill_value=0)
    pv["TOTAL"] = pv[cats].sum(axis=1)
    cat_df = pv.reset_index()
    cat_df.columns = ["Designer"] + list(cat_df.columns[1:])

    c3, c4 = st.columns([1.25, 1], gap="medium")
    with c3:
        show_table(add_total(cat_df, "Designer"), key_col="TOTAL")
    with c4:
        months = month_list(d_all)
        trend = x[x["qty"] != 0].groupby(["month", "designer"], as_index=False)["qty"].sum()
        title = "Monthly Designer Performance Trend (Pcs)"
        if not trend.empty:
            fig = px.bar(trend, x="month", y="qty", color="designer", color_discrete_sequence=pal(),
                         category_orders={"month": months},
                         labels={"month": "Month", "qty": "Pcs", "designer": "Designer"})
            fig.update_layout(barmode="stack")
            fig.update_xaxes(title="")
            chart(fig, title, 400, ns=f"{key}_des")
        else:
            empty_card(title, "Selected filters ke liye monthly data nahi hai.", ns=f"{key}_des")


def client_dashboard(d_all, key):
    dash_title("🏢 Executive Client Performance Dashboard")
    badge("🔎 Interactive Client Dashboard Filters")
    f = filter_row([
        ("month", "Month", ["All"] + month_list(d_all)),
        ("client", "Client Name", opt_list(d_all, "client")),
        ("order_type", "Order Type", opt_list(d_all, "order_type", first=("NEW", "REPEAT"))),
        ("category", "Category", opt_list(d_all, "category", first=tuple(CATEGORIES_LIST))),
    ], f"{key}_cli")
    x = filter_data(d_all, f)

    render_kpis(client_kpis(x))

    st.markdown("---")
    months = month_list(d_all)
    badge("📅 Client Fulfillment Breakdown (Monthly)")
    m = x.groupby("month")[["qty", "production", "received", "pending"]].sum().reindex(months, fill_value=0)
    m = m.reset_index()
    m.columns = ["MONTH", "ORDER QTY", "IN PRODUCTION", "RECEIVED", "PENDING"]

    c1, c2, c3 = st.columns([1.25, 1, 1], gap="medium")
    with c1:
        show_table(add_total(m, "MONTH"), height=520, key_col="ORDER QTY", sort=False)
    with c2:
        pie_df = m[m["ORDER QTY"] > 0]
        title = "Client Monthly Order Trend (Pcs)"
        if not pie_df.empty:
            fig = px.pie(pie_df, names="MONTH", values="ORDER QTY", color_discrete_sequence=pal(),
                         category_orders={"MONTH": months})
            fig.update_traces(textposition="inside", textinfo="percent", insidetextorientation="horizontal")
            chart(fig, title, 430, ns=f"{key}_cli", legend="bottom")
        else:
            empty_card(title, "Selected filters ke liye order data nahi hai.", ns=f"{key}_cli")
    with c3:
        rec, pen = x["received"].sum(), x["pending"].sum()
        title = "Order Fulfillment Ratio (Received vs Pending)"
        if rec + pen > 0:
            q = pd.DataFrame({"Status": ["RECEIVED QTY", "PENDING QTY"], "Qty": [rec, pen]})
            fig = px.pie(q, names="Status", values="Qty", hole=.5, color="Status",
                         color_discrete_map={"RECEIVED QTY": COLORS["red"], "PENDING QTY": COLORS["amber"]})
            fig.update_traces(textposition="inside", textinfo="percent")
            chart(fig, title, 430, ns=f"{key}_cli", legend="bottom")
        else:
            empty_card(title, "Received / pending data nahi hai.", ns=f"{key}_cli")

    st.markdown("---")
    badge("🧩 Category Wise Order Breakdown (Pcs)")
    pv = x.groupby(["month", "category"])["qty"].sum().unstack(fill_value=0)
    other_cats = [c for c in pv.columns if c not in CATEGORIES_LIST and pv[c].sum() != 0]
    cats = CATEGORIES_LIST + other_cats
    pv = pv.reindex(index=months, columns=cats, fill_value=0)
    pv["TOTAL"] = pv[cats].sum(axis=1)
    cat_df = pv.reset_index()
    cat_df.columns = ["MONTH"] + list(cat_df.columns[1:])

    c4, c5 = st.columns([1.35, 1], gap="medium")
    with c4:
        show_table(add_total(cat_df, "MONTH"), height=520, key_col="TOTAL", sort=False)
    with c5:
        long = cat_df.melt(id_vars="MONTH", value_vars=cats, var_name="Category", value_name="Pcs")
        long = long[long["Pcs"] != 0]
        title = "Category Distribution by Month"
        if not long.empty:
            fig = px.bar(long, x="MONTH", y="Pcs", color="Category", color_discrete_sequence=pal(),
                         category_orders={"MONTH": months, "Category": cats})
            fig.update_layout(barmode="stack")
            fig.update_xaxes(title="")
            chart(fig, title, 470, ns=f"{key}_cli")
        else:
            empty_card(title, "Selected filters ke liye category data nahi hai.", ns=f"{key}_cli")


def production_dashboard(d_all, key):
    dash_title("🏭 Production Executive Process Dashboard")
    badge("🔎 Interactive Dashboard Filters")
    f = filter_row([
        ("month", "Month", ["All"] + month_list(d_all)),
        ("fabricator", "Fabricator", opt_list(d_all, "fabricator")),
        ("status", "Status", opt_list(d_all, "status")),
        ("category", "Category", opt_list(d_all, "category", first=tuple(CATEGORIES_LIST))),
        ("designer", "Designer", opt_list(d_all, "designer", known=KNOWN_DESIGNERS)),
        ("risk", "Delay Risk", ["All", "Overdue (>0 days)", "On-time (≤0 days)"]),
    ], f"{key}_prod")
    x = filter_data(d_all, f)

    badge("📊 Process Key Performance Indicators")
    render_kpis(production_kpis(x))

    st.markdown("---")
    badge("📊 Category-wise Production Performance Breakdown")
    cat = x.groupby("category")[["production", "received", "pending"]].sum()
    other_cats = [c for c in cat.index if c not in CATEGORIES_LIST and cat.loc[c].abs().sum() != 0]
    cat = cat.reindex(CATEGORIES_LIST + other_cats, fill_value=0).reset_index()
    cat.columns = ["CATEGORY", "IN-PRODUCTION (AS)", "RECEIVED (AT)", "PENDING (AU)"]

    c1, c2 = st.columns([1, 1.35], gap="medium")
    with c1:
        show_table(add_total(cat, "CATEGORY"), key_col="PENDING (AU)")
    with c2:
        chart_df = cat.melt(id_vars="CATEGORY", value_vars=["IN-PRODUCTION (AS)", "RECEIVED (AT)", "PENDING (AU)"],
                            var_name="Metric", value_name="PCS")
        fig = px.bar(chart_df, x="CATEGORY", y="PCS", color="Metric", barmode="group",
                     color_discrete_sequence=[COLORS["blue"], COLORS["red"], COLORS["amber"]])
        fig.update_xaxes(title="")
        chart(fig, "Category-wise Volume Comparison", 370, ns=f"{key}_prod")

    st.markdown("---")
    badge("🧵 Fabricator Pending PCS Share")
    fab = x.groupby("fabricator", as_index=False)["pending"].sum()
    fab = fab[fab["pending"] > 0].sort_values("pending", ascending=False)
    if not fab.empty:
        f1, f2 = st.columns([1.35, 1], gap="medium")
        with f1:
            fig = px.pie(fab, names="fabricator", values="pending", hole=0.5, color_discrete_sequence=pal())
            fig.update_traces(textposition="inside", textinfo="percent")
            chart(fig, "Fabricator Pending PCS Share", 400, ns=f"{key}_prod", legend="bottom")
        with f2:
            tbl = fab.rename(columns={"fabricator": "FABRICATOR", "pending": "PENDING PCS"})
            tbl["SHARE %"] = tbl["PENDING PCS"] / tbl["PENDING PCS"].sum() * 100
            show_table(add_total(tbl, "FABRICATOR"), pct_cols=("SHARE %",), height=440, key_col="PENDING PCS")
    else:
        notice("Selected filters ke liye fabricator pending data nahi hai.")

    st.markdown("---")
    badge("📅 Monthly Fulfillment Trend")
    months = month_list(d_all)
    grp = x.groupby("month")
    mdf = pd.DataFrame({
        "ORDERS": grp["production"].apply(lambda s: int((s > 0).sum())),
        "IN-PRODUCTION (AS)": grp["production"].sum(),
        "RECEIVED (AT)": grp["received"].sum(),
        "PENDING (AU)": grp["pending"].sum(),
    }).reindex(months, fill_value=0).reset_index()
    mdf.columns = ["MONTH", "ORDERS", "IN-PRODUCTION (AS)", "RECEIVED (AT)", "PENDING (AU)"]

    m1, m2 = st.columns([1, 1.35], gap="medium")
    with m1:
        show_table(add_total(mdf, "MONTH"), height=520, key_col="PENDING (AU)", sort=False)
    with m2:
        line_df = mdf.melt(id_vars="MONTH", value_vars=["IN-PRODUCTION (AS)", "RECEIVED (AT)", "PENDING (AU)"],
                           var_name="Metric", value_name="PCS")
        fig = px.line(line_df, x="MONTH", y="PCS", color="Metric", markers=True,
                      category_orders={"MONTH": months},
                      color_discrete_sequence=[COLORS["red"], COLORS["amber"], COLORS["blue"]])
        fig.update_xaxes(title="")
        chart(fig, "Monthly Fulfillment Trend Analysis", 470, ns=f"{key}_prod")


def load_master(cfg):
    """Loads + parses the Master Sheet source tab. Returns d_all, or None after showing a message."""
    sheet_id = cfg["id"]
    tabs = list_tabs(sheet_id)
    if not tabs:
        notice("Sheet me koi tab nahi mila.", "error")
        return None

    src_tab = None
    for cand in cfg.get("source_tabs", []):
        src_tab = find_tab(tabs, cand)
        if src_tab:
            break
    if src_tab is None:
        src_tab = next((t for t in tabs if "DASHBOARD" not in t.upper()), tabs[0])

    values = load_tab_raw(sheet_id, src_tab)
    if not values:
        notice("Order data tab empty hai.", "error")
        return None

    raw, _first_row = build_raw(values)
    if raw.empty:
        notice("Order data rows nahi mile (column A me serial number aur column B me date expected hai).", "error")
        return None
    return prepare(raw)


# -----------------------------------------------------------------------------
# 6. SHARED HELPERS FOR "SAMPLE" & "FABRICATOR" DASHBOARDS
# -----------------------------------------------------------------------------
try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None

APP_TZ = "Asia/Kolkata"


def now_label():
    try:
        return datetime.now(ZoneInfo(APP_TZ)).strftime("%d %b %Y, %I:%M:%S %p")
    except Exception:
        return datetime.now(timezone.utc).strftime("%d %b %Y, %H:%M:%S UTC")


def idx_to_col(i):
    s, i = "", i + 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def to_num(v):
    """Single cell -> float (numbers stay numbers, '1,234' / '(50)' / '12%' are cleaned, junk -> 0)."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    return float(num(pd.Series([v])).iloc[0])


def _is_number_text(v):
    return bool(re.fullmatch(r"[-+]?(\d[\d,]*\.?\d*|\.\d+)", str(v).strip()))


@st.cache_data(ttl=TAB_TTL, show_spinner=False)
def list_tabs_meta(sheet_id):
    return [(ws.title, ws.id) for ws in _api(_open_book(sheet_id).worksheets)]


@st.cache_data(ttl=LIVE_TTL, show_spinner="Google Sheet se live data load ho raha hai...")
def fetch_grid(sheet_id, tab_title, formatted=False):
    """Whole tab as a grid.
    formatted=False -> real values (numbers = numbers, dates = serial numbers) -> used for calculations
    formatted=True  -> text exactly as shown in the sheet -> used only to locate titles/headers."""
    book = _open_book(sheet_id)
    params = None if formatted else {"valueRenderOption": "UNFORMATTED_VALUE",
                                     "dateTimeRenderOption": "SERIAL_NUMBER"}
    resp = _api(book.values_get, _a1_tab(tab_title), params=params)
    return {"values": resp.get("values", []), "synced": now_label()}


def pick_default_tab(meta, names, gid=None, name_regex=None):
    titles = [t for t, _ in meta]
    for n in names:
        t = find_tab(titles, n)
        if t:
            return t
    if name_regex:
        for t in titles:
            if re.search(name_regex, t, re.I):
                return t
    if gid is not None:
        for t, g in meta:
            if str(g) == str(gid):
                return t
    for t in titles:
        if "DASHBOARD" not in t.upper():
            return t
    return titles[0]


def resolve_source_tab(cfg, name_regex=None):
    """Picks the data tab automatically (names in cfg['source_tabs'] -> regex -> gid -> first non-dashboard tab)."""
    meta = list_tabs_meta(cfg["id"])
    if not meta:
        st.error("Sheet me koi tab nahi mila.")
        return None
    return pick_default_tab(meta, cfg.get("source_tabs", []), cfg.get("gid"), name_regex)


def run_live(render_fn, *args):
    """Runs the dashboard; if auto-refresh is ON it re-runs by itself (Streamlit fragments)."""
    secs = st.session_state.get("live_secs", 0)
    runner = render_fn
    if secs and hasattr(st, "fragment"):
        try:
            runner = st.fragment(run_every=int(secs))(render_fn)
        except Exception:
            runner = render_fn
    runner(*args)


def hero(icon, title, subtitle_html, synced):
    """Dashboard title bar with the live-sync time on the right."""
    fy = f"FY {FY_START_YEAR}-{(FY_START_YEAR + 1) % 100:02d}"
    dash_title(f"{icon} {title} ({fy})", chip=f"🕒 Updated {synced}")


def multi(container, label, options, key, placeholder="All"):
    """Multiselect where 'nothing selected' = ALL. Drops stale selections safely."""
    options = list(options)
    st.session_state[key] = [v for v in st.session_state.get(key, []) if v in options]
    try:
        return container.multiselect(label, options, key=key, placeholder=placeholder)
    except TypeError:
        return container.multiselect(label, options, key=key)


MONTH_MODES = ["All months", "Range", "Pick months"]


def _short_month(m):
    """'April-26' -> 'Apr-26' (other labels unchanged)."""
    mm = re.match(r"([A-Za-z]+)-(\d{2})$", str(m))
    return f"{mm.group(1)[:3]}-{mm.group(2)}" if mm else str(m)


def _month_popover(container, text):
    for kw in (STRETCH, {"use_container_width": True}, {}):
        try:
            return container.popover(text, **kw)
        except TypeError:
            continue
    return container.expander(text)


def month_filter(container, label, options, key, placeholder="All months"):
    """Month filter used on every dashboard: All months / Range (From -> To) / Pick months.
    Returns the list of selected month labels ([] = all months), same as the old multiselect."""
    options = list(options)
    rng = sorted([m for m in options if month_sort_key(m)[0] < 9998], key=month_sort_key)   # dated months only
    mode_key, from_key, to_key = f"{key}_mode", f"{key}_from", f"{key}_to"

    # keep saved state valid (options can change when the sheet changes)
    if st.session_state.get(mode_key) not in MONTH_MODES:
        st.session_state[mode_key] = MONTH_MODES[0]
    if rng:
        if st.session_state.get(from_key) not in rng:
            st.session_state[from_key] = rng[0]
        if st.session_state.get(to_key) not in rng:
            st.session_state[to_key] = rng[-1]
    picked = st.session_state.get(key, [])
    if not isinstance(picked, (list, tuple)):
        picked = []
    st.session_state[key] = [v for v in picked if v in options]

    if st.session_state[mode_key] == "Range" and not rng:
        st.session_state[mode_key] = MONTH_MODES[0]
    mode = st.session_state[mode_key]

    # current selection (computed first so the button can show it)
    if mode == "Range":
        i, j = rng.index(st.session_state[from_key]), rng.index(st.session_state[to_key])
        lo, hi = min(i, j), max(i, j)
        selected = rng[lo:hi + 1]
        text = _short_month(rng[lo]) if lo == hi else f"{_short_month(rng[lo])} → {_short_month(rng[hi])}"
    elif mode == "Pick months":
        selected = list(st.session_state[key])
        if not selected:
            text = placeholder
        elif len(selected) <= 2:
            text = ", ".join(_short_month(m) for m in selected)
        else:
            text = f"{len(selected)} months selected"
    else:
        selected, text = [], placeholder

    container.markdown(f'<div class="mf-label">{_esc(label)}</div>', unsafe_allow_html=True)
    with _month_popover(container, f"📅 {text}"):
        st.radio("Month filter type", MONTH_MODES if rng else [m for m in MONTH_MODES if m != "Range"],
                 key=mode_key, horizontal=True)
        if mode == "Range":
            a, b = st.columns(2)
            a.selectbox("From", rng, key=from_key, format_func=_short_month)
            b.selectbox("To", rng, key=to_key, format_func=_short_month)
            if rng.index(st.session_state[from_key]) > rng.index(st.session_state[to_key]):
                st.caption("From month 'To' ke baad hai - range ulta maan kar apply kiya gaya.")
            st.caption(f"{len(selected)} month(s): {_short_month(selected[0])} se {_short_month(selected[-1])} tak")
        elif mode == "Pick months":
            try:
                st.multiselect("Months", options, key=key, placeholder=placeholder, format_func=_short_month)
            except TypeError:
                st.multiselect("Months", options, key=key, format_func=_short_month)
            st.caption("Kuch select na karein = sabhi months.")
        else:
            st.caption("Sabhi months shamil hain. Range ya specific months ke liye upar option chunein.")
    return selected


def _reset_filters(defaults):
    for k, v in defaults.items():
        st.session_state[k] = v
        for suffix in ("_mode", "_from", "_to"):      # month filter extras -> back to "All months"
            st.session_state.pop(k + suffix, None)


def with_total(df, label_col, pct=None, label="TOTAL"):
    """Appends a TOTAL row. pct=(pct_col, numerator_col, denominator_col) is recomputed, never summed."""
    if df.empty:
        return df
    skip = {label_col} | ({pct[0]} if pct else set())
    row = {label_col: label}
    for c in df.columns:
        if c not in skip and pd.api.types.is_numeric_dtype(df[c]):
            row[c] = df[c].sum()
    if pct:
        den = row.get(pct[2], 0)
        row[pct[0]] = (row.get(pct[1], 0) / den * 100) if den else 0.0
    return pd.concat([df, pd.DataFrame([row])], ignore_index=True)


def table_with_download(df, name, **kw):
    df = order_rows(df, kw.get("key_col"), kw.get("sort", True), kw.get("sort_abs", False))
    show_table(df, **kw)
    st.download_button("⬇️ Download CSV", df.to_csv(index=False).encode("utf-8-sig"),
                       file_name=f"{name}.csv", mime="text/csv", key=f"dl_{name}")


def safe_div_pct(a, b):
    return (a / b * 100) if b else 0.0


# -----------------------------------------------------------------------------
# 7. SAMPLE SUMMARY DASHBOARD  (mirrors the "SAMPLE DASHBOARD" Apps Script)
#    Source tab columns:  B = Vendor | F = Qty | H = Process Date | L = Status | N = Designer
#    Row 1 = header, data from row 2.
# -----------------------------------------------------------------------------
SAMPLE_STATUSES = ["Approved", "Rejected", "On Hold", "In Process", "Other"]
def sample_colors():
    return {"Approved": COLORS["green"], "Rejected": COLORS["red"], "On Hold": COLORS["amber"],
            "In Process": COLORS["blue"], "Other": COLORS["slate"]}
SAMPLE_COLS = {"vendor": "B", "qty": "F", "date": "H", "status": "L", "designer": "N"}
FY_MONTH_NOS = [4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3]
MONTHNO_TO_LABEL = dict(zip(FY_MONTH_NOS, FY_MONTH_LABELS))
MON3 = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def _month_from_text(t):
    t = re.sub(r"[ .,\-]+", "/", str(t).strip().lower())
    for pat in (r"^\d{1,2}/(\d{1,2})/\d{2,4}$",      # dd/mm/yyyy
                r"^(\d{1,2})/\d{4}$",                  # mm/yyyy
                r"^\d{4}/(\d{1,2})/\d{1,2}$"):         # yyyy/mm/dd
        m = re.match(pat, t)
        if m:
            return int(m.group(1))
    m = re.search(r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", t)   # "Apr-26", "April 2026"
    return MON3.index(m.group(1)) + 1 if m else 0


def month_numbers(s):
    """Process-date column -> month number 1..12 (0 = blank / unreadable). Same rules as the sheet script."""
    s = pd.Series(s).fillna("")
    out = pd.Series(0, index=s.index, dtype="int64")
    numeric = pd.to_numeric(s, errors="coerce")
    isnum = numeric.notna() & numeric.between(1, 100000)
    if isnum.any():
        dts = pd.to_datetime(numeric[isnum].astype(float), unit="D", origin="1899-12-30", errors="coerce")
        out.loc[isnum] = dts.dt.month.fillna(0).astype("int64").to_numpy()
    txt = (~isnum) & numeric.isna() & s.astype(str).str.strip().ne("")
    if txt.any():
        out.loc[txt] = s[txt].map(_month_from_text).astype("int64").to_numpy()
    return out.where(out.between(1, 12), 0)


def classify_status(s):
    t = clean_text(s).str.upper()
    vals = np.select(
        [t.eq(""), t.str.contains("REJECT"), t.str.contains("APPROV"), t.str.contains("HOLD"), t.str.contains("PROCESS")],
        ["Other", "Rejected", "Approved", "On Hold", "In Process"], default="Other")
    return pd.Series(vals, index=t.index)


def canon_series(raw, skip_words):
    """Case-insensitive name cleanup: 'abc' / 'ABC ' -> one spelling. Blank -> (BLANK), numbers/headers -> (OTHER)."""
    skip = {w.upper() for w in skip_words}
    mapping = {}
    for v in raw:
        if v and not _is_number_text(v) and v.upper() not in skip:
            mapping.setdefault(v.upper(), v)
    return raw.map(lambda v: mapping.get(v.upper(), "(BLANK)" if v == "" else "(OTHER)"))


def build_samples(values):
    if len(values) < 2:
        return pd.DataFrame(), []
    header = list(values[0])
    rows = values[1:]
    width = max([len(r) for r in rows] + [col_to_idx("N") + 1])
    df = pd.DataFrame([list(r) + [""] * (width - len(r)) for r in rows], index=range(2, 2 + len(rows)))

    def col(key):
        return df[col_to_idx(SAMPLE_COLS[key])]

    d = pd.DataFrame(index=df.index)
    d.index.name = "sheet_row"
    d["qty"] = num(col("qty"))
    vendor_raw, designer_raw = clean_text(col("vendor")), clean_text(col("designer"))
    status_raw = clean_text(col("status"))
    d["vendor"] = canon_series(vendor_raw, ("VENDOR NAME", "VENDOR"))
    d["designer"] = canon_series(designer_raw, ("DESIGNER NAME", "DESIGNER"))
    d["month_no"] = month_numbers(col("date"))
    d["month"] = d["month_no"].map(MONTHNO_TO_LABEL).fillna("(NO DATE)")
    d["status"] = classify_status(col("status"))
    d["qty_raw"] = col("qty").astype(str).str.strip()
    d["date_raw"] = col("date").astype(str).str.strip()
    d["status_raw"] = status_raw
    keep = (vendor_raw != "") | (designer_raw != "") | (status_raw != "") | (d["qty"] != 0)
    return d[keep], header


def sample_filter(d, f, skip=()):
    x = d
    for field, col_name in (("month", "month"), ("designer", "designer"), ("status", "status"), ("vendor", "vendor")):
        if f.get(field) and field not in skip:
            x = x[x[col_name].isin(f[field])]
    return x


def status_table(x, group_col, index_values):
    if x.empty:
        p = pd.DataFrame(0.0, index=index_values, columns=SAMPLE_STATUSES)
    else:
        p = (x.groupby([group_col, "status"])["qty"].sum().unstack(fill_value=0)
             .reindex(index=index_values, columns=SAMPLE_STATUSES, fill_value=0).astype(float))
    p["Total"] = p[SAMPLE_STATUSES].sum(axis=1)
    p["Approval %"] = np.where(p["Total"] > 0, p["Approved"] / p["Total"].replace(0, np.nan) * 100, 0.0)
    return p


def sample_monthly(x):
    p = status_table(x, "month", FY_MONTH_LABELS + ["(NO DATE)"])
    if p.loc["(NO DATE)", "Total"] == 0:
        p = p.drop(index="(NO DATE)")
    p.index.name = "Month"
    return p.reset_index()


def sample_entity_table(x, group_col, names, label):
    p = status_table(x, group_col, list(names) + ["(BLANK)", "(OTHER)"])
    p = p[(p["Total"] != 0) | ~p.index.isin(["(BLANK)", "(OTHER)"])]
    p = p.sort_values("Total", ascending=False)
    p.index.name = label
    return p.reset_index()


def entity_chart(tbl, label, title, ns="smp"):
    t = tbl[tbl["Total"] > 0].head(20)
    if t.empty:
        empty_card(title, "Selected filters ke liye data nahi hai.", ns=ns)
        return
    long = t.melt(id_vars=label, value_vars=SAMPLE_STATUSES, var_name="Status", value_name="Pcs")
    fig = px.bar(long, y=label, x="Pcs", color="Status", orientation="h",
                 color_discrete_map=sample_colors(),
                 category_orders={label: list(t[label])[::-1], "Status": SAMPLE_STATUSES})
    fig.update_yaxes(title="")
    fig.update_xaxes(title="Pcs")
    chart(fig, title, max(340, 28 * len(t) + 140), ns=ns)


def render_sample_summary_dashboard(sheet_name, cfg):
    tab = resolve_source_tab(cfg)
    if tab:
        run_live(_sample_body, sheet_name, cfg, tab)


def _sample_body(sheet_name, cfg, tab):
    sheet_id = cfg["id"]
    try:
        pack = fetch_grid(sheet_id, tab)
    except SheetAccessError as e:
        show_access_error(e)
        return
    d, header = build_samples(pack["values"])
    hero("⚡", "Executive Sample Performance & Designer Dashboard", None, pack["synced"])
    if d.empty:
        notice("Sample data nahi mila. Expected: row 1 = header, row 2 se data "
               "(Vendor = B, Qty = F, Process Date = H, Status = L, Designer = N).", "warning")
        return

    # header sanity check
    hints = {"vendor": "vendor", "qty": "qty|quant|sample", "date": "date", "status": "status", "designer": "designer"}
    hits = 0
    for k, pat in hints.items():
        i = col_to_idx(SAMPLE_COLS[k])
        if i < len(header) and re.search(pat, str(header[i]), re.I):
            hits += 1
    if hits < 2:
        notice("Sample sheet ka header expected columns (B Vendor, F Qty, H Date, L Status, N Designer) se "
               "match nahi karta - numbers galat ho sakte hain.", "warning")

    designer_names = [n for n in d["designer"].unique() if not n.startswith("(")]
    vendor_names = [n for n in d["vendor"].unique() if not n.startswith("(")]

    # ---------------- filters ----------------
    keys = {"month": "smp_month", "designer": "smp_designer", "status": "smp_status", "vendor": "smp_vendor"}
    badge("🔎 Interactive Filters  (kuch select na karein = ALL)")
    c = st.columns([1.1, 1.2, 1.1, 1.2, 0.75], vertical_alignment="bottom")
    f = {
        "month": month_filter(c[0], "Month", FY_MONTH_LABELS, keys["month"], "All months"),
        "designer": multi(c[1], "Designer", designer_names, keys["designer"], "All designers"),
        "status": multi(c[2], "Status", SAMPLE_STATUSES, keys["status"], "All status"),
        "vendor": multi(c[3], "Vendor", vendor_names, keys["vendor"], "All vendors"),
    }
    c[4].button("↺ Reset", key="smp_reset", on_click=_reset_filters, args=({k: [] for k in keys.values()},),
                **STRETCH)

    x = sample_filter(d, f)
    x_nd = sample_filter(d, f, skip=("designer",))     # designer comparison ignores the designer filter
    x_nv = sample_filter(d, f, skip=("vendor",))       # vendor comparison ignores the vendor filter

    # ---------------- KPIs ----------------
    by = x.groupby("status")["qty"].sum()
    tot = float(x["qty"].sum())
    app, rej, hld, prc, oth = (float(by.get(s, 0.0)) for s in SAMPLE_STATUSES)
    ratio = safe_div_pct(app, tot)

    des_tbl = sample_entity_table(x_nd, "designer", designer_names, "Designer")
    real = des_tbl[~des_tbl["Designer"].str.startswith("(")]
    if not real.empty and real["Approved"].max() > 0:
        top = real.loc[real["Approved"].idxmax()]
        top_txt = f"{top['Designer']} ({fmt(top['Approved'])} Pcs)"
    else:
        top_txt = "N/A"

    badge("📊 Sample Process Key Performance Indicators")
    metrics([("Total Samples Qty", f"{fmt(tot)} Samples"), ("Approved Samples", f"{fmt(app)} Pcs"),
             ("Rejected Samples", f"{fmt(rej)} Pcs"), ("On Hold Samples", f"{fmt(hld)} Pcs"),
             ("On Process", f"{fmt(prc)} Pcs"), ("Approval Ratio %", f"{ratio:.1f}%"),
             ("🏆 Top Performer (Approved)", top_txt), ("Rejection Rate", f"{safe_div_pct(rej, tot):.1f}%"),
             ("Open Pipeline (Hold + Process)", f"{fmt(hld + prc)} Pcs"), ("Other / Blank Status", f"{fmt(oth)} Pcs")])
    st.markdown("---")

    mt = sample_monthly(x)
    colors = sample_colors()

    # ---------------- Monthly breakdown + status distribution ----------------
    badge("📅 Monthly Sample Breakdown (Process Date)")
    c1, c2 = st.columns([1.15, 1], gap="medium")
    with c1:
        shown = with_total(mt, "Month", pct=("Approval %", "Approved", "Total"))
        table_with_download(shown, "sample_monthly", pct_cols=("Approval %",), height=560, key_col="Total", sort=False)
        nd = mt.loc[mt["Month"] == "(NO DATE)", "Total"].sum()
        if nd > 0:
            notice(f"{fmt(nd)} samples ki Process Date khaali ya unreadable hai - table me '(NO DATE)' row dekhein.", "warning")
        else:
            notice("Sab samples ki Process Date sahi padhi gayi.", "success")
    with c2:
        title = "Monthly Sample Status Distribution (Pcs)"
        if tot == 0:
            empty_card(title, "Selected filters ke liye koi sample data nahi hai.", ns="smp")
        else:
            long = mt.melt(id_vars="Month", value_vars=SAMPLE_STATUSES, var_name="Status", value_name="Pcs")
            fig = px.bar(long, x="Month", y="Pcs", color="Status", barmode="stack", color_discrete_map=colors,
                         category_orders={"Month": list(mt["Month"]), "Status": SAMPLE_STATUSES})
            fig.update_xaxes(title="")
            chart(fig, title, 470, ns="smp")

    # ---------------- Trend + overall share ----------------
    if tot > 0:
        c1, c2 = st.columns([1.35, 1], gap="medium")
        with c1:
            trend = mt.melt(id_vars="Month", value_vars=["Total", "Approved", "Rejected"], var_name="Series",
                            value_name="Pcs")
            fig = px.line(trend, x="Month", y="Pcs", color="Series", markers=True,
                          color_discrete_map={"Total": COLORS["purple"], "Approved": colors["Approved"],
                                              "Rejected": colors["Rejected"]},
                          category_orders={"Month": list(mt["Month"])})
            fig.update_xaxes(title="")
            chart(fig, "Monthly Process Volume Trend", 380, ns="smp")
        with c2:
            share = pd.DataFrame({"Status": SAMPLE_STATUSES, "Pcs": [app, rej, hld, prc, oth]})
            share = share[share["Pcs"] > 0]
            fig = px.pie(share, names="Status", values="Pcs", hole=0.5, color="Status", color_discrete_map=colors,
                         category_orders={"Status": SAMPLE_STATUSES})
            fig.update_traces(textinfo="percent", textposition="inside")
            chart(fig, "Overall Sample Approval Share", 380, ns="smp", legend="bottom")

    # ---------------- Designer scorecard ----------------
    st.markdown("---")
    badge("👩‍🎨 Designer Performance Scorecard")
    st.caption("Designer filter yahan apply nahi hota taaki sab designers ki tulna ho sake; "
               "Month / Status / Vendor filter apply hote hain.")
    c1, c2 = st.columns([1.25, 1], gap="medium")
    with c1:
        table_with_download(with_total(des_tbl, "Designer", pct=("Approval %", "Approved", "Total")),
                            "sample_designer", pct_cols=("Approval %",), height=560, key_col="Total")
    with c2:
        entity_chart(des_tbl, "Designer", "Designer-wise Status (Pcs)")

    # ---------------- Vendor scorecard ----------------
    st.markdown("---")
    badge("🏭 Vendor Performance Scorecard")
    st.caption("Vendor filter yahan apply nahi hota taaki sab vendors ki tulna ho sake; "
               "Month / Status / Designer filter apply hote hain.")
    ven_tbl = sample_entity_table(x_nv, "vendor", vendor_names, "Vendor")
    c1, c2 = st.columns([1.25, 1], gap="medium")
    with c1:
        table_with_download(with_total(ven_tbl, "Vendor", pct=("Approval %", "Approved", "Total")),
                            "sample_vendor", pct_cols=("Approval %",), height=560, key_col="Total")
    with c2:
        entity_chart(ven_tbl, "Vendor", "Vendor-wise Status (Pcs)")


# -----------------------------------------------------------------------------
# 8. FABRICATOR DASHBOARD  (mirrors the "FABRICATOR DASHBOARD" Apps Script)
#    Source tab: MONTHWISE SUMMARY -> blocks titled "Position Of Stock <NAME>"
#    Each block: header row "Month", then 12 rows April..March, 5 value columns to the right of Month:
#    Fabric Issue (mtr) | Pcs Under Production | RCVD | Remaining | Monthly Incoming
# -----------------------------------------------------------------------------
FAB_METRICS = ["Fabric Issue (mtr)", "Pcs Under Production", "RCVD (Pcs)", "Remaining (Pcs)", "Monthly Incoming (Pcs)"]
FY_MONTH_SHORT = [m[:3] for m in MONTHS]


def detect_fab_blocks(disp):
    blocks, used = [], {}
    for r, row in enumerate(disp):
        for c, cell in enumerate(row):
            m = re.match(r"^position\s+of\s+stock\s*(.*)$", str(cell).strip(), re.I)
            if not m:
                continue
            hdr = -1
            for rr in range(r, min(r + 7, len(disp))):
                if c < len(disp[rr]) and re.fullmatch(r"month", str(disp[rr][c]).strip(), re.I):
                    hdr = rr
                    break
            if hdr < 0:
                continue
            first = -1
            for rr in range(hdr + 1, min(hdr + 7, len(disp))):
                if c < len(disp[rr]) and re.match(r"apr", str(disp[rr][c]).strip(), re.I):
                    first = rr
                    break
            if first < 0:
                continue
            raw = re.sub(r"[()]", "", m.group(1)).strip().upper()
            raw = re.sub(r"^[\s:\-–]+", "", raw).strip()
            is_all = bool(re.search(r"ALL\s*OVER", raw))
            name = "ALL OVER" if is_all else (raw or "BLOCK")
            if name in used:
                used[name] += 1
                name = f"{name} ({used[name]})"
            else:
                used[name] = 1
            blocks.append({"name": name, "is_all": is_all, "title_row": r, "first_row": first, "col": c,
                           "title": str(cell).strip()})
    return blocks


def _cell(grid, r, c):
    if r < len(grid) and c < len(grid[r]):
        return grid[r][c]
    return ""


def build_fab_data(disp, raw):
    blocks = detect_fab_blocks(disp)
    recs = []
    for b in blocks:
        for k in range(12):
            vals = [to_num(_cell(raw, b["first_row"] + k, b["col"] + f)) for f in range(1, 6)]
            rec = {"Fabricator": b["name"], "is_all": b["is_all"], "Month": FY_MONTH_LABELS[k]}
            rec.update(dict(zip(FAB_METRICS, vals)))
            recs.append(rec)
    return blocks, pd.DataFrame(recs)


def render_fabricator_dashboard(sheet_name, cfg):
    tab = resolve_source_tab(cfg, name_regex=r"monthwise|monthly\s*summary")
    if tab:
        run_live(_fab_body, sheet_name, cfg, tab)


def _fab_body(sheet_name, cfg, tab):
    sheet_id = cfg["id"]
    try:
        disp = fetch_grid(sheet_id, tab, True)["values"]
        pack = fetch_grid(sheet_id, tab)
    except SheetAccessError as e:
        show_access_error(e)
        return
    blocks, df = build_fab_data(disp, pack["values"])
    hero("🏭", "Fabricator Stock Position Dashboard", None, pack["synced"])
    if df.empty:
        notice("Fabricator 'Position Of Stock ...' tables nahi mile (title ke neeche 'Month' header aur 'April' se "
               "shuru 12 rows chahiye).", "error")
        return

    all_over = df[df["is_all"]]
    fabs_df = df[~df["is_all"]]
    base = fabs_df if not fabs_df.empty else all_over
    fab_names = list(dict.fromkeys(base["Fabricator"]))

    # ---------------- filters ----------------
    keys = {"fab": "fab_names", "month": "fab_month"}
    default_metric = "Fabric Issue (mtr)"
    st.session_state.setdefault("fab_metric", default_metric)
    badge("🔎 Interactive Filters  (kuch select na karein = ALL)")
    c = st.columns([1.4, 1.3, 1.2, 0.75], vertical_alignment="bottom")
    sel_fab = multi(c[0], "Fabricator", fab_names, keys["fab"], "All fabricators")
    sel_month = month_filter(c[1], "Month", FY_MONTH_LABELS, keys["month"], "All months")
    metric = c[2].selectbox("Metric (Matrix / Heatmap)", FAB_METRICS, key="fab_metric")
    c[3].button("↺ Reset", key="fab_reset", on_click=_reset_filters,
                args=({keys["fab"]: [], keys["month"]: [], "fab_metric": default_metric},), **STRETCH)

    def flt(frame, fabs=True, months=True):
        y = frame
        if fabs and sel_fab:
            y = y[y["Fabricator"].isin(sel_fab)]
        if months and sel_month:
            y = y[y["Month"].isin(sel_month)]
        return y

    x = flt(base)                                  # KPIs, matrix
    x_by_month = flt(base, months=False)           # monthly table/charts ignore the month filter
    x_by_fab = flt(base, fabs=False)               # scorecard compares all fabricators
    ao_f = flt(all_over, fabs=False)

    # ---------------- KPIs ----------------
    tot = x[FAB_METRICS].sum()
    recv_pct = safe_div_pct(tot["RCVD (Pcs)"], tot["Pcs Under Production"])
    g = x.groupby("Fabricator")[FAB_METRICS].sum()

    def top_of(col):
        if len(g) and g[col].max() > 0:
            return f"{g[col].idxmax()} ({fmt(g[col].max())} Pcs)"
        return "N/A"

    badge("📊 Key Performance Indicators")
    metrics([("Fabric Issued (mtr)", f"{tot['Fabric Issue (mtr)']:,.0f} mtr"),
             ("Pcs Under Production", f"{fmt(tot['Pcs Under Production'])} Pcs"),
             ("RCVD (Received)", f"{fmt(tot['RCVD (Pcs)'])} Pcs"),
             ("Remaining", f"{fmt(tot['Remaining (Pcs)'])} Pcs"),
             ("Monthly Incoming", f"{fmt(tot['Monthly Incoming (Pcs)'])} Pcs"),
             ("Received %", f"{recv_pct:.1f}%"),
             ("🏆 Top Receiver", top_of("RCVD (Pcs)")), ("⚠ Most Pending", top_of("Remaining (Pcs)"))])
    if tot[FAB_METRICS].abs().sum() == 0:
        notice("Selected filters ke liye koi data nahi hai.")
    st.markdown("---")

    # ---------------- tables ----------------
    mm = x_by_month.groupby("Month")[FAB_METRICS].sum().reindex(FY_MONTH_LABELS, fill_value=0).astype(float)
    mm["RCVD %"] = np.where(mm["Pcs Under Production"] > 0,
                            mm["RCVD (Pcs)"] / mm["Pcs Under Production"].replace(0, np.nan) * 100, 0.0)
    mm.index.name = "Month"
    monthly = mm.reset_index()

    sc = x_by_fab.groupby("Fabricator", sort=False)[FAB_METRICS].sum().astype(float)
    sc["RCVD %"] = np.where(sc["Pcs Under Production"] > 0,
                            sc["RCVD (Pcs)"] / sc["Pcs Under Production"].replace(0, np.nan) * 100, 0.0)
    sc.index.name = "Fabricator"
    score = sc.reset_index()

    # ---------------- Monthly position of stock ----------------
    badge("📅 Monthly Position Of Stock")
    st.caption("Fabricator filter apply hota hai; Month filter yahan apply nahi hota (poora saal dikhta hai).")
    c1, c2 = st.columns([1.15, 1], gap="medium")
    with c1:
        table_with_download(with_total(monthly, "Month", pct=("RCVD %", "RCVD (Pcs)", "Pcs Under Production")),
                            "fabricator_monthly", pct_cols=("RCVD %",), dec_cols=("Fabric Issue (mtr)",), height=560,
                            key_col="Pcs Under Production", sort=False)
    with c2:
        fig = go.Figure()
        for col_name, color in (("Pcs Under Production", COLORS["blue"]), ("RCVD (Pcs)", COLORS["green"]),
                                ("Remaining (Pcs)", COLORS["red"])):
            fig.add_trace(go.Bar(name=col_name, x=FY_MONTH_LABELS, y=mm[col_name], marker_color=color))
        fig.update_layout(barmode="group")
        chart(fig, "Monthly: Production vs RCVD vs Remaining (Pcs)", 470, ns="fab")

    c1, c2 = st.columns([1, 1], gap="medium")
    with c1:
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=FY_MONTH_LABELS, y=mm["Fabric Issue (mtr)"], name="Fabric Issue (mtr)",
                                 mode="lines+markers", line=dict(color=PLOT_TEXT if IS_DARK else "#111827", width=2)))
        fig.add_trace(go.Scatter(x=FY_MONTH_LABELS, y=mm["Monthly Incoming (Pcs)"], name="Monthly Incoming (Pcs)",
                                 mode="lines+markers", line=dict(color=COLORS["orange"], width=2), yaxis="y2"))
        fig.update_layout(yaxis=dict(title="Mtr"),
                          yaxis2=dict(title="Pcs", overlaying="y", side="right", showgrid=False,
                                      tickfont=dict(color=PLOT_MUTED), title_font=dict(color=PLOT_MUTED)))
        chart(fig, "Monthly Fabric Issue & Incoming Trend", 400, ns="fab")
    with c2:
        fig = go.Figure()
        order = score.sort_values("RCVD (Pcs)")
        fig.add_trace(go.Bar(name="RCVD", y=order["Fabricator"], x=order["RCVD (Pcs)"], orientation="h",
                             marker_color=COLORS["green"]))
        fig.add_trace(go.Bar(name="Remaining", y=order["Fabricator"], x=order["Remaining (Pcs)"], orientation="h",
                             marker_color=COLORS["red"]))
        fig.update_layout(barmode="stack", legend_traceorder="normal")
        chart(fig, "Fabricator Wise: RCVD vs Remaining (Pcs)", max(400, 30 * len(order) + 120), ns="fab",
              subtitle="Month filter apply hota hai; Fabricator filter nahi (sab ki tulna).")

    # ---------------- Fabricator scorecard ----------------
    st.markdown("---")
    badge("🏭 Fabricator Scorecard")
    st.caption("Fabricator filter yahan apply nahi hota (sab ki tulna); Month filter apply hota hai.")
    shown = with_total(score, "Fabricator", pct=("RCVD %", "RCVD (Pcs)", "Pcs Under Production"))
    if not ao_f.empty:
        ao_sum = ao_f[FAB_METRICS].sum()
        ao_row = {"Fabricator": "ALL OVER (SHEET)", **{k: float(ao_sum[k]) for k in FAB_METRICS},
                  "RCVD %": safe_div_pct(ao_sum["RCVD (Pcs)"], ao_sum["Pcs Under Production"])}
        shown = pd.concat([shown, pd.DataFrame([ao_row])], ignore_index=True)
    c1, c2 = st.columns([1.3, 1], gap="medium")
    with c1:
        table_with_download(shown, "fabricator_scorecard", pct_cols=("RCVD %",), dec_cols=("Fabric Issue (mtr)",),
                            height=520, key_col="Pcs Under Production")
        if not ao_f.empty:
            diff = float((score[FAB_METRICS].sum() - ao_f[FAB_METRICS].sum()).abs().sum())
            if diff < 1:
                notice("Fabricators ka total sheet ke ALL OVER table se match karta hai.", "success")
            else:
                notice(f"Fabricators ka total ALL OVER table se alag hai (difference = {diff:,.0f}) - "
                       "koi fabricator table summary me missing ho sakta hai.", "warning")
        else:
            notice("Sheet me ALL OVER table nahi mila, isliye cross-check nahi ho paya.")
    with c2:
        order = score.sort_values("RCVD %")
        fig = px.bar(order, x="RCVD %", y="Fabricator", orientation="h", text_auto=".1f",
                     color="RCVD %", range_color=[0, 100],
                     color_continuous_scale=[[0, COLORS["red"]], [0.5, COLORS["amber"]], [1, COLORS["green"]]])
        fig.update_layout(coloraxis_showscale=False)
        fig.update_traces(textposition="outside", cliponaxis=False)
        fig.update_xaxes(range=[0, 115], title="Received %")
        fig.update_yaxes(title="")
        chart(fig, "Fabricator Wise: Received %", max(400, 30 * len(order) + 120), ns="fab", legend="none")

    # ---------------- Matrix ----------------
    st.markdown("---")
    badge(f"🧮 Month × Fabricator Matrix — Metric: {metric}")
    xm = flt(base, months=False)
    if xm.empty:
        notice("Selected filters ke liye data nahi hai.")
    else:
        mat = (xm.groupby(["Fabricator", "Month"])[metric].sum().unstack(fill_value=0)
               .reindex(index=[n for n in fab_names if n in set(xm["Fabricator"])], columns=FY_MONTH_LABELS, fill_value=0))
        mat.columns = FY_MONTH_SHORT
        tbl = mat.copy()
        tbl["TOTAL"] = tbl.sum(axis=1)
        tbl.index.name = "Fabricator"
        tbl = tbl.reset_index()
        tbl = with_total(tbl, "Fabricator", label="ALL FABRICATORS")
        dec = ("TOTAL", *FY_MONTH_SHORT) if metric.startswith("Fabric") else ()
        table_with_download(tbl, "fabricator_matrix", dec_cols=dec, key_col="TOTAL")

        fig = px.imshow(mat, text_auto=",.0f", aspect="auto", color_continuous_scale=HEAT_SCALE,
                        labels=dict(x="Month", y="Fabricator", color=""))
        fig.update_layout(coloraxis_colorbar=dict(title="", tickfont=dict(color=PLOT_MUTED)))
        fig.update_xaxes(side="top", showgrid=False)
        fig.update_yaxes(showgrid=False)
        chart(fig, f"Heatmap — {metric}", max(320, 36 * len(mat) + 120), ns="fab", legend="none")


# -----------------------------------------------------------------------------
# 9. FABRIC PO DASHBOARD  (source: "INDEX" tab of the FABRIC PO sheet)
#    Row 1 = header, data below. Columns (A..T):
#    S.NO | VENDOR NAME | PO NO. | ORDER DATE | DELIVERY DATE | RCVD DATE | DELAY DAYS | ORDER TYPE |
#    CLIENT NAME | DESIGNER NAME | COLLECTION NAME | STATUS | ACTUAL ORDER QTY. | TOTAL MTR OF FABRIC ORDERED |
#    TOTAL MTR OF FABRIC RCVD | PENDING | EXCESS | REMARK | TOTAL PC | ITEM
#    Delivery rule: vendor gets 15 days from ORDER DATE (DELIVERY DATE column). If the fabric is received after
#    the delivery date -> DELAYED. If not yet received and today is past the delivery date -> OVERDUE.
# -----------------------------------------------------------------------------
FABPO_LEAD_DAYS = 15     # used only when the DELIVERY DATE cell is blank
# field -> (normalised header name, fallback column position A=0 ...)
FABPO_COLS = {
    "sno": ("SNO", 0), "vendor": ("VENDORNAME", 1), "po": ("PONO", 2), "order_date": ("ORDERDATE", 3),
    "delivery_date": ("DELIVERYDATE", 4), "rcvd_date": ("RCVDDATE", 5), "delay_sheet": ("DELAYDAYS", 6),
    "order_type": ("ORDERTYPE", 7), "client": ("CLIENTNAME", 8), "designer": ("DESIGNERNAME", 9),
    "collection": ("COLLECTIONNAME", 10), "status": ("STATUS", 11), "qty": ("ACTUALORDERQTY", 12),
    "ordered": ("TOTALMTROFFABRICORDERED", 13), "rcvd": ("TOTALMTROFFABRICRCVD", 14),
    "pending": ("PENDING", 15), "total_pc": ("TOTALPC", 18), "item": ("ITEM", 19),
}
FABPO_TIMINGS = ["On Time", "Delayed", "Overdue (Pending)", "Open (Within Deadline)", "No Date", "Cancelled"]
_DATE_IN_TEXT = re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})")


def fabpo_colors():
    return {"On Time": COLORS["green"], "Delayed": COLORS["red"], "Overdue (Pending)": COLORS["orange"],
            "Open (Within Deadline)": COLORS["blue"], "No Date": COLORS["slate"], "Cancelled": COLORS["slate"]}


def _today_ist():
    try:
        return pd.Timestamp(datetime.now(ZoneInfo(APP_TZ)).date())
    except Exception:
        return pd.Timestamp(datetime.now(timezone.utc).date())


def parse_rcvd_cell(v):
    """RCVD DATE cell -> list of dates. Handles one date, serial numbers and part-lots like '24/04/2026+15/05/2026'."""
    if v is None:
        return []
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        if 20000 <= float(v) <= 80000:
            return [pd.Timestamp("1899-12-30") + pd.Timedelta(days=float(v))]
        return []
    text = re.sub(r"\s+", "", str(v))
    if not text:
        return []
    out = []
    for d, m, y in _DATE_IN_TEXT.findall(text):
        yr = int(y) + (2000 if int(y) < 100 else 0)
        try:
            out.append(pd.Timestamp(year=yr, month=int(m), day=int(d)))
        except ValueError:
            continue
    if not out:
        one = parse_dates(pd.Series([v])).iloc[0]
        if pd.notna(one):
            out = [one]
    return [d.normalize() for d in out]


def _fabpo_header_row(grid):
    for r in range(min(8, len(grid))):
        keys = {norm_key(c) for c in grid[r]}
        if "VENDORNAME" in keys or ("PONO" in keys and "ORDERDATE" in keys):
            return r
    return 0


def _fabpo_col_map(header):
    keys = [norm_key(h) for h in header]
    out = {}
    for field, (name, pos) in FABPO_COLS.items():
        idx = keys.index(name) if name in keys else next((i for i, k in enumerate(keys) if k.startswith(name)), None)
        out[field] = idx if idx is not None else pos
    return out


def build_fabric_po(values):
    """Raw INDEX grid (unformatted) -> clean one-row-per-PO DataFrame. Returns (df, header_row_cells)."""
    if not values:
        return pd.DataFrame(), []
    hr = _fabpo_header_row(values)
    header = values[hr]
    cmap = _fabpo_col_map(header)
    rows = values[hr + 1:]
    if not rows:
        return pd.DataFrame(), header
    width = max(max(len(r) for r in rows), max(cmap.values()) + 1)
    grid = pd.DataFrame([list(r) + [""] * (width - len(r)) for r in rows])

    def col(field):
        return grid[cmap[field]].reset_index(drop=True)

    vendor_raw = col("vendor")
    vendor_txt = clean_text(vendor_raw)
    po_txt = clean_text(col("po"))
    sno_ok = num(col("sno")) > 0
    bad_vendor = (vendor_txt == "") | vendor_txt.map(_is_number_text) | \
        vendor_txt.str.match(r"^\d{1,2}[/\-.]\d{1,2}[/\-.]\d{2,4}$") | vendor_txt.str.upper().str.startswith("TOTAL")
    keep = (~bad_vendor) & (sno_ok | (po_txt != ""))     # drops the helper date row, blanks and TOTAL rows

    d = pd.DataFrame({"vendor": vendor_txt.str.upper(), "po": po_txt})
    for f in ("order_type", "client", "designer", "collection", "status", "item"):
        t = clean_text(col(f)).str.upper()
        d[f] = t.where(t != "", "(BLANK)")
    d["order_date"] = parse_dates(col("order_date"))
    d["delivery_date"] = parse_dates(col("delivery_date"))
    for f in ("qty", "ordered", "rcvd", "pending", "total_pc"):
        d[f] = num(col(f))
    d["sheet_delay"] = clean_delay_days(col("delay_sheet")).clip(lower=0)
    rcvd_lists = col("rcvd_date").map(parse_rcvd_cell)
    d["last_rcvd"] = pd.to_datetime(rcvd_lists.map(lambda L: max(L) if L else pd.NaT))
    d["rcvd_text"] = rcvd_lists.map(lambda L: " + ".join(x.strftime("%d/%m/%Y") for x in sorted(L)))
    d = d[keep.values].reset_index(drop=True)
    if d.empty:
        return d, header

    # --- delivery due date: sheet DELIVERY DATE, else ORDER DATE + 15 days
    due = d["delivery_date"].copy()
    fill = due.isna() & d["order_date"].notna()
    due[fill] = d.loc[fill, "order_date"] + pd.Timedelta(days=FABPO_LEAD_DAYS)
    d["due"] = due

    # --- fulfilment class from STATUS (falls back to pending / received meters when status is blank)
    sn = d["status"].map(norm_key)
    blank_recv = (d["status"] == "(BLANK)") & (d["pending"] <= 0) & (d["rcvd"] > 0)
    d["fulfil"] = np.select(
        [sn.str.contains("CANCEL"), sn.str.contains("PART"),
         sn.str.contains("PEND|NOTR|WAIT|TRANSIT|PROCESS"),
         sn.str.contains("RCVD|RECEIVED|RECD|COMPLETE|DELIVERED") | blank_recv],
        ["Cancelled", "Partial", "Pending", "Received"], default="Pending")
    recv = d["fulfil"] == "Received"
    openpo = d["fulfil"].isin(["Pending", "Partial"])

    # --- delay days
    today = _today_ist()
    has_dates = d["last_rcvd"].notna() & d["due"].notna()
    delay = pd.Series(0.0, index=d.index)
    m1 = recv & has_dates
    delay[m1] = (d.loc[m1, "last_rcvd"] - d.loc[m1, "due"]).dt.days
    m2 = openpo & d["due"].notna()
    delay[m2] = (today - d.loc[m2, "due"]).dt.days
    m3 = recv & ~has_dates
    delay[m3] = d.loc[m3, "sheet_delay"]
    d["delay_days"] = delay.clip(lower=0).fillna(0).astype(int)

    d["timing"] = np.select(
        [d["fulfil"] == "Cancelled", recv & (d["delay_days"] > 0), recv & ~has_dates & (d["sheet_delay"] <= 0),
         recv, openpo & (d["delay_days"] > 0)],
        ["Cancelled", "Delayed", "No Date", "On Time", "Overdue (Pending)"], default="Open (Within Deadline)")
    d["is_late"] = (d["delay_days"] > 0).astype(int)
    d["is_recv"] = recv.astype(int)
    d["is_ontime"] = (d["timing"] == "On Time").astype(int)
    d["month"] = month_label(d["order_date"])
    d["days_to_due"] = np.where(openpo & d["due"].notna(), (d["due"] - today).dt.days, np.nan)
    return d, header


def _fpo_date(s):
    return s.dt.strftime("%d/%m/%Y").fillna("")


def fabpo_stats(x):
    """One summary row for any slice of POs."""
    ordered, rcvd = float(x["ordered"].sum()), float(x["rcvd"].sum())
    late = int(x["is_late"].sum())
    recv_n = int(x["is_recv"].sum())
    return {
        "POs": int(len(x)),
        "Fabric Ordered (mtr)": ordered,
        "Fabric Rcvd (mtr)": rcvd,
        "Pending (mtr)": float(x["pending"].sum()),
        "RCVD %": safe_div_pct(rcvd, ordered),
        "Total Pcs": float(x["total_pc"].sum()),
        "Delayed POs": late,
        "Avg Delay (days)": (float(x["delay_days"].sum()) / late) if late else 0.0,
        "On-Time %": safe_div_pct(int(x["is_ontime"].sum()), recv_n),
    }


def fabpo_group(x, col, label, order_by="Fabric Ordered (mtr)", total=True):
    rows = [{label: k, **fabpo_stats(g)} for k, g in x.groupby(col, sort=False)]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows).sort_values(order_by, ascending=False).reset_index(drop=True)
    if total:
        df = pd.concat([df, pd.DataFrame([{label: "TOTAL", **fabpo_stats(x)}])], ignore_index=True)
    return df


FPO_DEC = ("Fabric Ordered (mtr)", "Fabric Rcvd (mtr)", "Pending (mtr)", "Avg Delay (days)")
FPO_PCT = ("RCVD %", "On-Time %")


def render_fabric_po_dashboard(sheet_name, cfg):
    tab = resolve_source_tab(cfg, name_regex=r"^\s*index\s*$")
    if tab:
        run_live(_fabpo_body, sheet_name, cfg, tab)


def _fabpo_body(sheet_name, cfg, tab):
    sheet_id = cfg["id"]
    try:
        pack = fetch_grid(sheet_id, tab)
    except SheetAccessError as e:
        show_access_error(e)
        return
    d, header = build_fabric_po(pack["values"])
    hero("🧾", "Fabric PO Tracking Dashboard", None, pack["synced"])
    if d.empty:
        notice("Fabric PO data nahi mila. Expected: 'INDEX' tab, row 1 = header (VENDOR NAME, PO NO., ORDER DATE, "
               "DELIVERY DATE, RCVD DATE ...), row 2 se PO data.", "warning")
        return

    months = sorted(d["month"].unique(), key=month_sort_key)

    def opts(c):
        return sorted(v for v in d[c].unique() if v)

    # ---------------- filters ----------------
    keys = {"month": "fpo_month", "vendor": "fpo_vendor", "type": "fpo_type", "client": "fpo_client",
            "designer": "fpo_designer", "item": "fpo_item", "status": "fpo_status"}
    badge("🔎 Interactive Filters  (kuch select na karein = ALL)")
    c = st.columns([1.1, 1.2, 1.0, 1.2, 0.75], vertical_alignment="bottom")
    f_month = month_filter(c[0], "Order Month", months, keys["month"], "All months")
    f_vendor = multi(c[1], "Vendor", opts("vendor"), keys["vendor"], "All vendors")
    f_type = multi(c[2], "Order Type", opts("order_type"), keys["type"], "NEW / REPEAT")
    f_client = multi(c[3], "Client", opts("client"), keys["client"], "All clients")
    c[4].button("↺ Reset", key="fpo_reset", on_click=_reset_filters, args=({k: [] for k in keys.values()},),
                **STRETCH)
    c = st.columns([1.2, 1.2, 1.2], vertical_alignment="bottom")
    f_designer = multi(c[0], "Designer", opts("designer"), keys["designer"], "All designers")
    f_item = multi(c[1], "Item", opts("item"), keys["item"], "All items")
    f_status = multi(c[2], "Status", opts("status"), keys["status"], "All status")

    x = d
    for sel, colname in ((f_month, "month"), (f_vendor, "vendor"), (f_type, "order_type"), (f_client, "client"),
                         (f_designer, "designer"), (f_item, "item"), (f_status, "status")):
        if sel:
            x = x[x[colname].isin(sel)]
    st.caption(f"Delivery rule: Order Date se {FABPO_LEAD_DAYS} din (sheet ka DELIVERY DATE column). "
               "RCVD DATE us se baad ho to DELAYED; abhi tak receive nahi hua aur date nikal gayi to OVERDUE. "
               "Part lots (jaise 24/04/2026+15/05/2026) me aakhri date count hoti hai.")
    if x.empty:
        notice("Selected filters ke liye koi PO nahi mila.")
        return

    # ---------------- KPIs ----------------
    s = fabpo_stats(x)
    overdue = int((x["timing"] == "Overdue (Pending)").sum())
    worst = x.loc[x["delay_days"].idxmax()]
    worst_txt = f"{int(worst['delay_days'])} Days ({worst['po']})" if worst["delay_days"] > 0 else "None"
    badge("📊 Key Performance Indicators")
    metrics([("Total POs", f"{s['POs']:,} POs"),
             ("Fabric Ordered", f"{s['Fabric Ordered (mtr)']:,.0f} mtr"),
             ("Fabric Received", f"{s['Fabric Rcvd (mtr)']:,.0f} mtr"),
             ("Pending Fabric", f"{s['Pending (mtr)']:,.0f} mtr"),
             ("Received %", f"{s['RCVD %']:.1f}%"),
             ("Total Pcs", f"{s['Total Pcs']:,.0f} Pcs"),
             ("Delayed POs", f"{s['Delayed POs']:,} POs"),
             ("Overdue (Not Received)", f"{overdue:,} POs"),
             ("On-Time Delivery %", f"{s['On-Time %']:.1f}%"),
             ("Avg Delay", f"{s['Avg Delay (days)']:.1f} Days"),
             ("⚠ Longest Delay", worst_txt)])
    st.markdown("---")

    # ---------------- monthly ----------------
    badge("📅 Monthly Position (Order Month wise)")
    mon = x.groupby("month").agg(ordered=("ordered", "sum"), rcvd=("rcvd", "sum"), pending=("pending", "sum"),
                                 pcs=("total_pc", "sum")).reindex(sorted(x["month"].unique(), key=month_sort_key))
    mon["pct"] = np.where(mon["ordered"] > 0, mon["rcvd"] / mon["ordered"].replace(0, np.nan) * 100, 0.0)
    c1, c2 = st.columns([1.15, 1], gap="medium")
    with c1:
        fig = go.Figure()
        for name, colname, color in (("Ordered (mtr)", "ordered", COLORS["blue"]),
                                     ("Received (mtr)", "rcvd", COLORS["green"]),
                                     ("Pending (mtr)", "pending", COLORS["red"])):
            fig.add_trace(go.Bar(name=name, x=list(mon.index), y=mon[colname], marker_color=color))
        fig.update_layout(barmode="group")
        chart(fig, "Monthly Fabric: Ordered vs Received vs Pending (mtr)", 400, ns="fpo")
    with c2:
        ot = x["order_type"].value_counts()
        fig = go.Figure(go.Pie(labels=list(ot.index), values=list(ot.values), hole=0.55,
                               marker=dict(colors=pal()[:max(1, len(ot))]), textinfo="label+value"))
        chart(fig, "Order Type Split (POs)", 400, ns="fpo", legend="bottom")

    mrows = [{"Month": m, **fabpo_stats(x[x["month"] == m])} for m in mon.index]
    mtbl = pd.DataFrame(mrows)[["Month", "POs", "Fabric Ordered (mtr)", "Fabric Rcvd (mtr)", "Pending (mtr)",
                                "RCVD %", "Total Pcs"]]
    mtbl = pd.concat([mtbl, pd.DataFrame([{"Month": "TOTAL", **{k: s[k] for k in mtbl.columns[1:]}}])],
                     ignore_index=True)
    badge("🗓️ Month Wise Summary")
    table_with_download(mtbl, "fabpo_monthly", pct_cols=("RCVD %",), dec_cols=FPO_DEC,
                        key_col="Fabric Ordered (mtr)", sort=False,
                        height=min(520, 64 + 38 * len(mtbl)))
    st.markdown("---")

    # ---------------- vendor ----------------
    badge("🏭 Vendor Scorecard")
    vend = fabpo_group(x, "vendor", "Vendor")
    vv = vend[vend["Vendor"] != "TOTAL"]
    table_with_download(vend, "fabpo_vendor", pct_cols=FPO_PCT, dec_cols=FPO_DEC, height=420,
                        key_col="Fabric Ordered (mtr)")

    # Received vs Pending: vendors across the width (same layout as the monthly charts)
    o = vv.sort_values("Fabric Ordered (mtr)", ascending=False)
    fig = go.Figure()
    fig.add_trace(go.Bar(name="Received (mtr)", x=list(o["Vendor"]), y=o["Fabric Rcvd (mtr)"],
                         marker_color=COLORS["green"], hovertemplate="%{x}<br>Received: %{y:,.0f} mtr<extra></extra>"))
    fig.add_trace(go.Bar(name="Pending (mtr)", x=list(o["Vendor"]), y=o["Pending (mtr)"],
                         marker_color=COLORS["red"], hovertemplate="%{x}<br>Pending: %{y:,.0f} mtr<extra></extra>"))
    fig.update_layout(barmode="stack")
    fig.update_xaxes(title="", tickangle=-30 if len(o) > 4 else 0)
    fig.update_yaxes(title="Fabric (mtr)")
    chart(fig, "Vendor Wise: Received vs Pending Fabric (mtr)", 420, ns="fpo")

    # Vendor share: donut (percent inside, names in a side legend -> no overlapping labels) + share table
    share = vv[vv["Fabric Ordered (mtr)"] > 0].sort_values("Fabric Ordered (mtr)", ascending=False)
    c1, c2 = st.columns([1.2, 1], gap="medium")
    with c1:
        if share.empty:
            empty_card("Vendor Share of Fabric Ordered (mtr)", "Selected filters ke liye fabric ordered data nahi hai.",
                       ns="fpo")
        else:
            base = pal()
            colors = [base[i % len(base)] for i in range(len(share))]
            fig = go.Figure(go.Pie(labels=list(share["Vendor"]), values=list(share["Fabric Ordered (mtr)"]), hole=0.55,
                                   sort=False, direction="clockwise", marker=dict(colors=colors),
                                   textinfo="percent", textposition="inside", insidetextorientation="horizontal",
                                   hovertemplate="%{label}<br>%{value:,.0f} mtr (%{percent})<extra></extra>"))
            fig.update_layout(margin=dict(l=8, r=8, t=10, b=8))
            chart(fig, "Vendor Share of Fabric Ordered (mtr)", 440, ns="fpo", legend="right")
    with c2:
        if not share.empty:
            badge("📋 Vendor Share")
            stbl = share[["Vendor", "Fabric Ordered (mtr)"]].copy()
            stbl["Share %"] = stbl["Fabric Ordered (mtr)"] / stbl["Fabric Ordered (mtr)"].sum() * 100
            stbl = with_total(stbl, "Vendor", pct=("Share %", "Fabric Ordered (mtr)", "Fabric Ordered (mtr)"))
            show_table(stbl, pct_cols=("Share %",), dec_cols=("Fabric Ordered (mtr)",), height=380,
                       key_col="Fabric Ordered (mtr)")


# -----------------------------------------------------------------------------
# 10. TARGET & REVENUE DASHBOARD  (sheet: "DESIGNER'S TARGET ACHIEVED 2026-27")
#     Source tabs: "NEW" and "REPEAT"  ->  row 1 = title, row 2 = header, data from row 3
#     Columns (A..K): S.NO | ORDER DATE | IMAGE | BARCODE | ITEM NAME | SIZE | NO.OF PCS | RATE |
#                     ORDER BY | DESIGNER | TOTAL AMOUNT
#     Revenue = NO.OF PCS x RATE  (RATE = price of ONE piece).  The sheet's TOTAL AMOUNT column is only
#     used to cross-check the calculation (see "Data Quality Check" at the bottom of the dashboard).
#     Yearly target (50.2 Cr) is for NEW orders only; achievement is tracked by ORDER DATE month.
# -----------------------------------------------------------------------------
NEW_TARGET_CR = 50.2                 # yearly NEW-order target in crore (can also be changed on the dashboard)
NEW_MONTHLY_WEIGHTS = [1] * 12       # Apr..Mar. Equal = target / 12 per month. Seasonal split? e.g. [1, 1, 1, 1, 1, 1, 1.5, 1.5, 1.5, 1, 1, 1]
TGT_COLS = {                         # field -> (normalised header name, fallback column position A=0 ...)
    "sno": ("SNO", 0), "date": ("ORDERDATE", 1), "image": ("IMAGE", 2), "barcode": ("BARCODE", 3), "item": ("ITEMNAME", 4),
    "size": ("SIZE", 5), "pcs": ("NOOFPCS", 6), "rate": ("RATE", 7), "order_by": ("ORDERBY", 8),
    "designer": ("DESIGNER", 9), "sheet_total": ("TOTALAMOUNT", 10),
}
REV_MONEY = ("New Revenue", "Repeat Revenue", "Total Revenue", "Avg Rate (₹/pc)")


def inr(v):
    """Full rupee amount with Indian digit grouping: 5020000 -> ₹50,20,000."""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return ""
    if not np.isfinite(v):
        return ""
    s = f"{int(round(abs(v))):d}"
    if len(s) > 3:
        s = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", s[:-3]) + "," + s[-3:]
    return f"{'-' if v < 0 and round(abs(v)) else ''}₹{s}"


def inr_short(v):
    """Compact amount for KPI tiles: ₹50.20 Cr / ₹3.29 L / ₹4,500."""
    v = float(v)
    a, sign = abs(v), ("-" if v < 0 else "")
    if a >= 1e7:
        return f"{sign}₹{a / 1e7:,.2f} Cr"
    if a >= 1e5:
        return f"{sign}₹{a / 1e5:,.2f} L"
    return f"{sign}₹{a:,.0f}"


def _tgt_header_row(grid):
    for r in range(min(10, len(grid))):
        keys = {norm_key(c) for c in grid[r]}
        if "ORDERDATE" in keys and ({"NOOFPCS", "RATE", "BARCODE"} & keys):
            return r
    return None


def _tgt_col_map(header):
    keys = [norm_key(h) for h in header]
    out = {}
    for field, (name, pos) in TGT_COLS.items():
        idx = keys.index(name) if name in keys else next((i for i, k in enumerate(keys) if k.startswith(name)), None)
        out[field] = idx if idx is not None else pos
    return out


def build_target_orders(values, source):
    """Raw tab grid (unformatted) -> one clean row per order line. Returns (df, header_found)."""
    hr = _tgt_header_row(values)
    header_ok = hr is not None
    if hr is None:
        hr = 1 if len(values) > 1 else 0
    if not values:
        return pd.DataFrame(), header_ok
    cmap = _tgt_col_map(values[hr])
    rows = values[hr + 1:]
    if not rows:
        return pd.DataFrame(), header_ok
    width = max(max(len(r) for r in rows), max(cmap.values()) + 1)
    # index = real sheet row number (header is on sheet row hr+1, so first data row is hr+2)
    grid = pd.DataFrame([list(r) + [""] * (width - len(r)) for r in rows], index=range(hr + 2, hr + 2 + len(rows)))

    def col(field):
        return grid[cmap[field]]

    pcs, rate, sno = num(col("pcs")), num(col("rate")), num(col("sno"))
    barcode = clean_text(col("barcode")).str.upper()
    item = clean_text(col("item")).str.upper()
    label_cells = pd.concat([clean_text(col(f)).str.upper() for f in ("sno", "date", "barcode", "item")], axis=1)
    totalish = label_cells.apply(lambda c: c.str.contains(r"\bTOTAL\b", regex=True)).any(axis=1)
    has_id = (barcode != "") | (item != "") | (sno > 0)
    keep = (pcs > 0) & has_id & ~totalish            # drops blank rows, TOTAL rows, helper rows

    d = pd.DataFrame(index=grid.index)
    d.index.name = "sheet_row"
    d["source"] = source
    d["date"] = parse_dates(col("date"))
    d["month"] = month_label(d["date"])
    d["barcode"] = barcode.replace("", "(NO BARCODE)")
    d["item"] = item.replace("", "(NO ITEM)")
    d["size"] = clean_text(col("size"))
    d["pcs"] = pcs
    d["rate"] = rate
    d["order_by"] = clean_text(col("order_by")).str.upper().replace("", "(BLANK)")
    d["designer"] = clean_text(col("designer")).str.upper().replace("", "(NOT ASSIGNED)")
    d["sheet_total"] = num(col("sheet_total"))
    d["revenue"] = d["pcs"] * d["rate"]
    return d[keep], header_ok


def _img_url(cell):
    """=IMAGE("url") formula ya seedha link -> usable image URL ('' agar nahi mila). Drive links thumbnail me badle jate hain."""
    t = str(cell or "").strip()
    m = re.search(r'IMAGE\(\s*"([^"]+)"', t, re.I)
    url = m.group(1) if m else (t if re.match(r"https?://", t, re.I) else "")
    if not url:
        return ""
    d = re.search(r"drive\.google\.com/(?:file/d/|open\?id=|uc\?(?:export=\w+&)?id=)([\w-]+)", url)
    return f"https://drive.google.com/thumbnail?id={d.group(1)}&sz=w300" if d else url


@st.cache_data(ttl=LIVE_TTL, show_spinner=False)
def fetch_image_urls(sheet_id, tab_title, col_idx):
    """IMAGE column ke formulas padhta hai -> {sheet_row: url}. Fail hone par khaali dict (dashboard chalta rahta hai)."""
    try:
        c = idx_to_col(col_idx)
        resp = _open_book(sheet_id).values_get(f"{_a1_tab(tab_title)}!{c}:{c}", params={"valueRenderOption": "FORMULA"})
        out = {}
        for i, row in enumerate(resp.get("values", []), start=1):
            u = _img_url(row[0] if row else "")
            if u:
                out[i] = u
        return out
    except Exception:
        return {}


def load_target_data(cfg):
    """Reads NEW + REPEAT tabs (cached, LIVE_TTL). Returns (df, notes, synced_label)."""
    sheet_id = cfg["id"]
    titles = [t for t, _ in list_tabs_meta(sheet_id)]
    frames, notes, synced = [], [], now_label()
    for source, names in (("NEW", cfg.get("source_tabs", ["NEW"])), ("REPEAT", cfg.get("repeat_tabs", ["REPEAT"]))):
        tab = next((t for t in (find_tab(titles, n) for n in names) if t), None)
        if tab is None:
            notes.append(f"'{source}' tab sheet me nahi mila (expected: {', '.join(names)}) - uska data count nahi hua.")
            continue
        pack = fetch_grid(sheet_id, tab)
        synced = pack["synced"]
        df, header_ok = build_target_orders(pack["values"], source)
        if not df.empty:
            _hr = _tgt_header_row(pack["values"])
            _cmap = _tgt_col_map(pack["values"][_hr if _hr is not None else 1])
            imgs = fetch_image_urls(sheet_id, tab, _cmap["image"])
            df["image"] = [imgs.get(r, "") for r in df.index]
        if not header_ok:
            notes.append(f"'{tab}' tab me header row (ORDER DATE / NO.OF PCS / RATE) nahi mili - default columns "
                         "(A..K) use hue hain, numbers check kar lein.")
        if not df.empty:
            df["tab"] = tab
            frames.append(df)
    if not frames:
        return pd.DataFrame(), notes, synced
    return pd.concat(frames).reset_index(), notes, synced


def _month_start(i):
    return pd.Timestamp(year=FY_START_YEAR if i < 9 else FY_START_YEAR + 1, month=FY_MONTH_NOS[i], day=1)


def target_monthly(d, annual_target, today):
    """Month-wise NEW target vs achieved. Returns (table without TOTAL row, FY month start dates)."""
    extra = sorted({m for m in d["month"].unique() if m not in FY_MONTH_LABELS}, key=month_sort_key)
    months = FY_MONTH_LABELS + extra
    w = np.asarray(NEW_MONTHLY_WEIGHTS, dtype=float)
    w = w / w.sum() if w.sum() > 0 else np.full(12, 1 / 12)
    tgt = pd.Series(0.0, index=months)
    tgt.loc[FY_MONTH_LABELS] = annual_target * w
    is_new = d["source"] == "NEW"
    new = d[is_new].groupby("month")["revenue"].sum().reindex(months, fill_value=0.0)
    rep = d[~is_new].groupby("month")["revenue"].sum().reindex(months, fill_value=0.0)
    starts = pd.Series([_month_start(i) for i in range(12)] + [pd.NaT] * len(extra), index=months)
    in_fy = pd.Series([True] * 12 + [False] * len(extra), index=months)
    future = in_fy & (starts > today) & (new == 0)          # month not started yet -> no % / variance shown

    pct = pd.Series(np.where(tgt > 0, new / tgt.replace(0, np.nan) * 100, np.nan), index=months)
    pct[future] = np.nan
    var = (new - tgt).where(in_fy & ~future)
    cum_t = tgt.where(in_fy).cumsum()
    cum_a = new.where(in_fy).cumsum().where(~future)
    cum_p = cum_a / cum_t.replace(0, np.nan) * 100
    df = pd.DataFrame({
        "Month": months, "Target": tgt.values, "Achieved (New)": new.values, "Achieved %": pct.values,
        "Variance": var.values, "Cum. Target": cum_t.values, "Cum. Achieved": cum_a.values, "Cum. %": cum_p.values,
        "Repeat Revenue": rep.values, "New + Repeat": (new + rep).values})
    return df, starts


def target_rate_class(v):
    return "r-good" if v >= 100 else ("r-mid" if v >= 75 else "r-bad")


def _rev_stats(g):
    rev, pcs = float(g["revenue"].sum()), float(g["pcs"].sum())
    return {"New Revenue": float(g.loc[g["source"] == "NEW", "revenue"].sum()),
            "Repeat Revenue": float(g.loc[g["source"] == "REPEAT", "revenue"].sum()),
            "Total Revenue": rev, "Total Pcs": pcs, "Orders": int(len(g)),
            "Avg Rate (₹/pc)": (rev / pcs) if pcs else 0.0}


def rev_group(x, col, label, index=None, share=True):
    """Revenue table grouped by `col` (+ TOTAL row, totals recomputed - averages are never summed)."""
    total = float(x["revenue"].sum())
    keys = list(index) if index is not None else list(dict.fromkeys(x[col]))
    rows = []
    for k in keys:
        s = _rev_stats(x[x[col] == k])
        if share:
            s["Share %"] = safe_div_pct(s["Total Revenue"], total)
        rows.append({label: k, **s})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    if index is None:
        df = df.sort_values("Total Revenue", ascending=False).reset_index(drop=True)
    tot = {label: "TOTAL", **_rev_stats(x)}
    if share:
        tot["Share %"] = 100.0 if total else 0.0
    cols = [label, "New Revenue", "Repeat Revenue", "Total Revenue"] + (["Share %"] if share else []) + \
           ["Total Pcs", "Orders", "Avg Rate (₹/pc)"]
    return pd.concat([df, pd.DataFrame([tot])], ignore_index=True)[cols]


def donut(labels, values, title, ns="tgt", height=400):
    base = pal()
    fig = go.Figure(go.Pie(labels=list(labels), values=list(values), hole=0.55, sort=False, direction="clockwise",
                           marker=dict(colors=[base[i % len(base)] for i in range(len(labels))]),
                           textinfo="percent", textposition="inside", insidetextorientation="horizontal",
                           hovertemplate="%{label}<br>₹%{value:,.0f} (%{percent})<extra></extra>"))
    chart(fig, title, height, ns=ns, legend="right")


def render_target_dashboard(sheet_name, cfg):
    run_live(_target_body, sheet_name, cfg)


def _target_body(sheet_name, cfg):
    try:
        d, notes, synced = load_target_data(cfg)
    except SheetAccessError as e:
        show_access_error(e)
        return
    hero("🎯", "Designer Target & Revenue Dashboard", None, synced)
    for n in notes:
        notice(n, "warning")
    if d.empty:
        notice("Target sheet me order data nahi mila. Expected: 'NEW' aur 'REPEAT' tab, row 2 = header (ORDER DATE, "
               "BARCODE, ITEM NAME, NO.OF PCS, RATE, ORDER BY, DESIGNER, TOTAL AMOUNT), row 3 se data.", "warning")
        return

    # ---------------- target settings ----------------
    with st.expander("⚙️ Target Settings", expanded=False):
        st.session_state.setdefault("tgt_annual_cr", float(cfg.get("annual_target_cr", NEW_TARGET_CR)))
        annual_cr = st.number_input("Yearly NEW order target (₹ Cr)", min_value=0.0, step=0.1, format="%.2f",
                                    key="tgt_annual_cr")
        st.caption("Monthly target = yearly target ÷ 12 (barabar split). Seasonal split chahiye to code me "
                   "NEW_MONTHLY_WEIGHTS badal dein. Target sirf NEW orders par lagta hai, REPEAT alag dikhaya jata hai.")
    annual_target = float(annual_cr) * 1e7
    today = _today_ist()

    # =============== A) TARGET TRACKER (overall - filters apply nahi hote) ===============
    mt, starts = target_monthly(d, annual_target, today)
    new_total = float(d.loc[d["source"] == "NEW", "revenue"].sum())
    rep_total = float(d.loc[d["source"] == "REPEAT", "revenue"].sum())
    ach_pct = safe_div_pct(new_total, annual_target)
    remaining = max(annual_target - new_total, 0.0)
    elapsed = [m for m in FY_MONTH_LABELS if starts[m] <= today]
    months_left = sum(1 for m in FY_MONTH_LABELS if (starts[m] + pd.offsets.MonthEnd(0)) >= today)
    sched = float(mt.loc[mt["Month"].isin(elapsed), "Target"].sum())
    gap = new_total - sched
    if not elapsed:
        sched_txt = "FY not started"
    else:
        sched_txt = f"{'Ahead' if gap >= 0 else 'Behind'} {inr_short(abs(gap))}"
    req_txt = f"{inr_short(remaining / months_left)} ({months_left} mo left)" if months_left and remaining > 0 else \
        ("Target Achieved 🎉" if remaining <= 0 and annual_target > 0 else "—")

    badge("🎯 NEW Order Target Tracker (overall - niche ke filters yahan apply nahi hote)")
    metrics([("Yearly Target (New)", inr_short(annual_target)),
             ("Achieved (New)", inr_short(new_total)),
             ("Achieved %", f"{ach_pct:.1f}%"),
             ("Remaining Target", inr_short(remaining)),
             (f"Vs Schedule (till {elapsed[-1] if elapsed else '-'})", sched_txt),
             ("Required / Month", req_txt),
             ("Repeat Revenue", inr_short(rep_total)),
             ("Total Revenue (New + Repeat)", inr_short(new_total + rep_total))])

    st.caption("Vs Schedule = ab tak ke mahino (current month ko milakar) ka cumulative target minus achieved. "
               "Required / Month = remaining target ÷ bache hue mahine (current month ko milakar).")
    top = max(100, int(np.ceil(ach_pct / 20.0) * 20))
    steps = [dict(range=[0, 50], color=_hex_to_rgba(COLORS["red"], 0.25)),
             dict(range=[50, 100], color=_hex_to_rgba(COLORS["amber"], 0.25))]
    if top > 100:
        steps.append(dict(range=[100, top], color=_hex_to_rgba(COLORS["green"], 0.25)))
    gauge = go.Figure(go.Indicator(
        mode="gauge+number", value=float(ach_pct), number=dict(suffix="%", valueformat=".1f", font=dict(size=38, color=PLOT_TEXT)),
        gauge=dict(axis=dict(range=[0, top], tickfont=dict(color=PLOT_MUTED)), bar=dict(color=COLORS["blue"]),
                   bgcolor="rgba(0,0,0,0)", borderwidth=0, steps=steps,
                   threshold=dict(line=dict(color=PLOT_TEXT, width=3), thickness=0.8, value=100))))
    body = mt.copy()
    labels = list(body["Month"])
    cr = 1e7
    c1, c2 = st.columns([1, 2], gap="medium")
    with c1:
        chart(gauge, "Yearly Target Achieved %", 330, ns="tgt", legend="none",
              subtitle=f"{inr_short(new_total)} of {inr_short(annual_target)} (New orders)")
    with c2:
        ach_cr = (body["Achieved (New)"] / cr).tolist()
        tgt_cr = (body["Target"] / cr).tolist()
        colors = [COLORS["green"] if (t > 0 and a >= t) else COLORS["blue"] for a, t in zip(ach_cr, tgt_cr)]
        fig = go.Figure()
        fig.add_trace(go.Bar(name="Achieved (New)", x=labels, y=ach_cr, marker_color=colors,
                             text=[f"{v:.2f}" if v else "" for v in ach_cr], textposition="outside", cliponaxis=False,
                             hovertemplate="%{x}<br>Achieved: ₹%{y:.2f} Cr<extra></extra>"))
        fig.add_trace(go.Scatter(name="Monthly Target", x=labels, y=tgt_cr, mode="lines+markers",
                                 line=dict(color=COLORS["amber"], width=2, dash="dash"),
                                 hovertemplate="%{x}<br>Target: ₹%{y:.2f} Cr<extra></extra>"))
        fig.update_yaxes(title="₹ Cr")
        fig.update_xaxes(title="")
        chart(fig, "Month-wise NEW Order: Achieved vs Target (₹ Cr)", 330, ns="tgt",
              subtitle="Green bar = month ka target poora hua")

    # month-wise table (+ TOTAL row)
    tot = {"Month": "TOTAL", "Target": float(body.loc[body["Month"].isin(FY_MONTH_LABELS), "Target"].sum()),
           "Achieved (New)": new_total, "Achieved %": ach_pct if annual_target > 0 else np.nan,
           "Variance": new_total - annual_target, "Cum. Target": np.nan, "Cum. Achieved": np.nan, "Cum. %": np.nan,
           "Repeat Revenue": rep_total, "New + Repeat": new_total + rep_total}
    shown = pd.concat([body, pd.DataFrame([tot])], ignore_index=True)
    money = ("Target", "Achieved (New)", "Variance", "Cum. Target", "Cum. Achieved", "Repeat Revenue", "New + Repeat")
    badge("📅 Month-wise Target vs Achieved (Order Date month, NEW orders)")
    c1, c2 = st.columns([1.55, 1], gap="medium")
    with c1:
        table_with_download(shown, "target_monthly", pct_cols=("Achieved %", "Cum. %"), money_cols=money,
                            rate_cols=("Achieved %", "Cum. %"), rate_fn=target_rate_class, height=560,
                            key_col="Achieved (New)", sort=False)
        st.caption("Achieved % ka rang: 🟢 ≥100%  🟡 75-99%  🔴 <75%. Jo mahina abhi shuru nahi hua uska % khaali dikhta hai.")
        nd = float(d.loc[(d["month"] == "(NO DATE)") & (d["source"] == "NEW"), "revenue"].sum())
        if nd > 0:
            notice(f"{inr(nd)} ke NEW orders ki Order Date khaali/unreadable hai - ye total me count hue, "
                   "par kisi month me nahi (table me '(NO DATE)' row).", "warning")
    with c2:
        fig = go.Figure()
        fig.add_trace(go.Scatter(name="Cumulative Target", x=labels, y=(body["Cum. Target"] / cr).tolist(),
                                 mode="lines+markers", line=dict(color=COLORS["amber"], width=2, dash="dash"),
                                 hovertemplate="%{x}<br>Cum. Target: ₹%{y:.2f} Cr<extra></extra>"))
        fig.add_trace(go.Scatter(name="Cumulative Achieved", x=labels, y=(body["Cum. Achieved"] / cr).tolist(),
                                 mode="lines+markers", line=dict(color=COLORS["green"], width=3), fill="tozeroy",
                                 fillcolor=_hex_to_rgba(COLORS["green"], 0.12),
                                 hovertemplate="%{x}<br>Cum. Achieved: ₹%{y:.2f} Cr<extra></extra>"))
        fig.update_yaxes(title="₹ Cr")
        fig.update_xaxes(title="")
        chart(fig, "Cumulative NEW Order Achievement vs Target (₹ Cr)", 560, ns="tgt")

    # =============== B) FILTERED ANALYTICS ===============
    st.markdown("---")
    keys = {"month": "tgt_month", "designer": "tgt_designer", "item": "tgt_item", "order_by": "tgt_orderby",
            "source": "tgt_source"}
    extra_m = sorted({m for m in d["month"].unique() if m not in FY_MONTH_LABELS}, key=month_sort_key)
    all_months = FY_MONTH_LABELS + extra_m

    def opts(c):
        return sorted(v for v in d[c].unique() if v)

    badge("🔎 Interactive Filters  (kuch select na karein = ALL)")
    c = st.columns([1.1, 1.2, 1.3, 1.1, 1.0, 0.75], vertical_alignment="bottom")
    f_month = month_filter(c[0], "Order Month", all_months, keys["month"], "All months")
    f_des = multi(c[1], "Designer", opts("designer"), keys["designer"], "All designers")
    f_item = multi(c[2], "Item Name", opts("item"), keys["item"], "All items")
    f_by = multi(c[3], "Order By", opts("order_by"), keys["order_by"], "All")
    f_src = multi(c[4], "Order Type", ["NEW", "REPEAT"], keys["source"], "NEW + REPEAT")
    c[5].button("↺ Reset", key="tgt_reset", on_click=_reset_filters, args=({k: [] for k in keys.values()},), **STRETCH)

    x = d
    for sel, colname in ((f_month, "month"), (f_des, "designer"), (f_item, "item"), (f_by, "order_by"),
                         (f_src, "source")):
        if sel:
            x = x[x[colname].isin(sel)]
    if x.empty:
        notice("Selected filters ke liye koi order nahi mila.")
    else:
        _target_sections(x, all_months)

    # =============== C) DATA QUALITY ===============
    _target_quality(d)


def _target_sections(x, all_months):
    s = _rev_stats(x)
    des = rev_group(x, "designer", "Designer")
    itm = rev_group(x, "item", "Item Name")
    byc = rev_group(x, "order_by", "Order By")
    des_b, itm_b, byc_b = (t[t.iloc[:, 0] != "TOTAL"] for t in (des, itm, byc))
    top_des = f"{des_b.iloc[0, 0]} ({inr_short(des_b.iloc[0]['Total Revenue'])})" if len(des_b) else "N/A"
    top_itm = f"{itm_b.iloc[0, 0]} ({inr_short(itm_b.iloc[0]['Total Revenue'])})" if len(itm_b) else "N/A"

    badge("📊 Revenue Key Performance Indicators (filters ke hisab se)")
    metrics([("Total Revenue", inr_short(s["Total Revenue"])),
             ("New Order Revenue", inr_short(s["New Revenue"])),
             ("Repeat Order Revenue", inr_short(s["Repeat Revenue"])),
             ("Repeat Share %", f"{safe_div_pct(s['Repeat Revenue'], s['Total Revenue']):.1f}%"),
             ("Total Pcs", f"{s['Total Pcs']:,.0f} Pcs"),
             ("Order Lines", f"{s['Orders']:,}"),
             ("Unique Designs (Barcode)", f"{x['barcode'].nunique():,}"),
             ("Avg Rate / Pc", inr(s["Avg Rate (₹/pc)"])),
             ("🏆 Top Designer", top_des), ("🏆 Top Item", top_itm)])
    st.markdown("---")

    # ---------------- month-wise revenue ----------------
    badge("📅 Month-wise Revenue (New vs Repeat)")
    mrev = rev_group(x, "month", "Month", index=all_months, share=False)
    mbody = mrev[mrev["Month"] != "TOTAL"]
    c1, c2 = st.columns([1.3, 1], gap="medium")
    with c1:
        table_with_download(mrev, "target_month_revenue", money_cols=REV_MONEY, height=560,
                            key_col="Total Revenue", sort=False)
    with c2:
        fig = go.Figure()
        fig.add_trace(go.Bar(name="New", x=list(mbody["Month"]), y=(mbody["New Revenue"] / 1e5).tolist(),
                             marker_color=COLORS["green"], hovertemplate="%{x}<br>New: ₹%{y:,.2f} L<extra></extra>"))
        fig.add_trace(go.Bar(name="Repeat", x=list(mbody["Month"]), y=(mbody["Repeat Revenue"] / 1e5).tolist(),
                             marker_color=COLORS["purple"], hovertemplate="%{x}<br>Repeat: ₹%{y:,.2f} L<extra></extra>"))
        fig.update_layout(barmode="stack")
        fig.update_yaxes(title="₹ Lakh")
        fig.update_xaxes(title="")
        chart(fig, "Monthly Revenue: New vs Repeat (₹ Lakh)", 560, ns="tgt")

    # ---------------- designer ----------------
    st.markdown("---")
    badge("👩‍🎨 Designer-wise Revenue")
    c1, c2 = st.columns([1.5, 1], gap="medium")
    with c1:
        table_with_download(des, "target_designer", pct_cols=("Share %",), money_cols=REV_MONEY, height=460,
                            key_col="Total Revenue")
    with c2:
        o = des_b.sort_values("Total Revenue")
        fig = go.Figure()
        fig.add_trace(go.Bar(name="New", y=list(o["Designer"]), x=(o["New Revenue"] / 1e5).tolist(), orientation="h",
                             marker_color=COLORS["green"], hovertemplate="%{y}<br>New: ₹%{x:,.2f} L<extra></extra>"))
        fig.add_trace(go.Bar(name="Repeat", y=list(o["Designer"]), x=(o["Repeat Revenue"] / 1e5).tolist(),
                             orientation="h", marker_color=COLORS["purple"],
                             hovertemplate="%{y}<br>Repeat: ₹%{x:,.2f} L<extra></extra>"))
        fig.update_layout(barmode="stack")
        fig.update_xaxes(title="₹ Lakh")
        fig.update_yaxes(title="")
        chart(fig, "Designer-wise Revenue (₹ Lakh)", max(340, 34 * len(o) + 130), ns="tgt")

    # ---------------- item ----------------
    st.markdown("---")
    badge("👗 Item-wise Revenue")
    c1, c2 = st.columns([1.5, 1], gap="medium")
    with c1:
        table_with_download(itm, "target_item", pct_cols=("Share %",), money_cols=REV_MONEY, height=460,
                            key_col="Total Revenue")
    with c2:
        donut(itm_b["Item Name"], itm_b["Total Revenue"], "Item Share of Revenue", height=420)

    # ---------------- order by ----------------
    st.markdown("---")
    badge("🏷️ Order By-wise Revenue")
    c1, c2 = st.columns([1.5, 1], gap="medium")
    with c1:
        table_with_download(byc, "target_orderby", pct_cols=("Share %",), money_cols=REV_MONEY, height=360,
                            key_col="Total Revenue")
    with c2:
        donut(byc_b["Order By"], byc_b["Total Revenue"], "Order By Share of Revenue", height=360)

    # ---------------- top repeat-order designs ----------------
    st.markdown("---")
    badge("🔁 Top 15 Designs by Repeat Orders")
    rp = x[x["source"] == "REPEAT"]
    if rp.empty:
        notice("Selected filters me koi REPEAT order nahi hai (Order Type filter ya baaki filters check karein).", "info")
    else:
        def _first(sr):
            v = [i for i in sr if i]
            return v[0] if v else ""
        g = rp.sort_values("date").groupby("barcode").agg(
            image=("image", _first), item=("item", "first"),
            designer=("designer", lambda v: v.mode().iloc[0] if len(v.mode()) else ""),
            orders=("pcs", "size"), pcs=("pcs", "sum"), revenue=("revenue", "sum"),
            last=("date", "max")).reset_index()
        g = g.sort_values(["orders", "pcs", "revenue"], ascending=False).head(15).reset_index(drop=True)
        g["last"] = g["last"].dt.strftime("%d/%m/%Y").fillna("")
        rows = []
        omax = float(g["orders"].max()) if len(g) else 0.0
        for i, r in g.iterrows():
            w = (float(r["orders"]) / omax * 100) if omax > 0 else 0.0
            key_td = (f'<td class="key" style="background-image:linear-gradient(90deg,var(--bar) {w:.1f}%,'
                      f'transparent {w:.1f}%)">{int(r["orders"]):,}</td>')
            img = (f'<img src="{html.escape(r["image"], quote=True)}" loading="lazy" referrerpolicy="no-referrer" '
                   f'style="height:64px;width:64px;object-fit:cover;border-radius:8px;">') if r["image"] else "—"
            rows.append(f"<tr><td>{i + 1}</td><td style='text-align:center'>{img}</td><td>{_esc(r['barcode'])}</td>"
                        f"<td>{_esc(r['item'])}</td><td>{_esc(r['designer'])}</td>{key_td}"
                        f"<td>{r['pcs']:,.0f}</td><td>{_esc(inr(r['revenue']))}</td><td>{_esc(r['last'])}</td></tr>")
        heads = ["#", "Image", "Barcode", "Item Name", "Designer", "Repeat Orders", "Repeat Pcs", "Repeat Revenue", "Last Repeat Date"]
        _html('<div class="tbl-wrap" style="max-height:640px"><table><thead><tr>'
              + "".join(f'<th class="key" title="Is column ke hisaab se bada → chhota">{h}<span class="arw">▼</span></th>'
                        if h == "Repeat Orders" else f"<th>{h}</th>" for h in heads)
              + "</tr></thead><tbody>" + "".join(rows) + "</tbody></table></div>")
        if not g["image"].astype(bool).any():
            st.caption("Images nahi dikh rahi? Sheet ke IMAGE column me =IMAGE(\"link\") formula ya direct link hona chahiye. "
                       "'Insert → Image in cell' / floating images API se read nahi hoti.")
        dl = g.rename(columns={"image": "Image URL", "barcode": "Barcode", "item": "Item Name", "designer": "Designer",
                               "orders": "Repeat Orders", "pcs": "Repeat Pcs", "revenue": "Repeat Revenue",
                               "last": "Last Repeat Date"})
        st.download_button("⬇️ Download CSV", dl.to_csv(index=False).encode("utf-8-sig"),
                           file_name="target_top_repeat_designs.csv", mime="text/csv", key="dl_target_top_repeat")

    with st.expander("📄 Filtered order lines (full data)"):
        full = x.sort_values(["date", "sheet_row"]).copy()
        full["Order Date"] = full["date"].dt.strftime("%d/%m/%Y").fillna("")
        full = full[["tab", "sheet_row", "Order Date", "barcode", "item", "size", "pcs", "rate", "revenue",
                     "order_by", "designer"]]
        full.columns = ["Tab", "Sheet Row", "Order Date", "Barcode", "Item Name", "Size", "Pcs", "Rate (₹/pc)",
                        "Revenue", "Order By", "Designer"]
        st.dataframe(full, hide_index=True, **STRETCH)
        st.download_button("⬇️ Download CSV", full.to_csv(index=False).encode("utf-8-sig"),
                           file_name="target_order_lines.csv", mime="text/csv", key="dl_target_lines")


def _target_quality(d):
    """Cross-checks Revenue (PCS x RATE) with the sheet's own TOTAL AMOUNT column and flags bad rows."""
    st.markdown("---")
    badge("🧪 Data Quality Check (poora data, filters ke bina)")
    calc, sheet = float(d["revenue"].sum()), float(d["sheet_total"].sum())
    mism = d[(d["sheet_total"] > 0) & ((d["revenue"] - d["sheet_total"]).abs() > 1)]
    no_total = d[(d["sheet_total"] <= 0) & (d["revenue"] > 0)]
    no_rate = d[d["rate"] <= 0]
    no_date = d[d["month"] == "(NO DATE)"]
    notice(f"Order lines: {len(d):,}  ·  Calculated revenue (Pcs × Rate): {inr(calc)}  ·  "
           f"Sheet 'TOTAL AMOUNT' sum: {inr(sheet)}", "info")
    if mism.empty and no_total.empty:
        notice("Calculation sheet ke TOTAL AMOUNT column se match karti hai.", "success")
    if not mism.empty:
        notice(f"{len(mism):,} rows me Pcs × Rate sheet ke TOTAL AMOUNT se alag hai (net difference "
               f"{inr(mism['revenue'].sum() - mism['sheet_total'].sum())}). Dashboard Pcs × Rate use karta hai.", "warning")
    if not no_total.empty:
        notice(f"{len(no_total):,} rows me TOTAL AMOUNT khaali/0 hai par Pcs × Rate se revenue bana - dashboard me count hua.",
               "warning")
    if not no_rate.empty:
        notice(f"{len(no_rate):,} rows me RATE khaali/0 hai - inka revenue 0 gina gaya ({int(no_rate['pcs'].sum()):,} pcs).",
               "warning")
    if not no_date.empty:
        notice(f"{len(no_date):,} rows ki ORDER DATE khaali/unreadable hai - month-wise table me '(NO DATE)' me jayengi.",
               "warning")
    bad = pd.concat([mism, no_rate, no_date]).drop_duplicates(subset=["tab", "sheet_row"])
    if not bad.empty:
        with st.expander(f"Check karne wali rows ({len(bad):,})"):
            b = pd.DataFrame({"Location": bad["tab"] + " · row " + bad["sheet_row"].astype(int).astype(str),
                              "Barcode": bad["barcode"].values, "Pcs": bad["pcs"].values,
                              "Rate (₹/pc)": bad["rate"].values, "Calculated": bad["revenue"].values,
                              "Sheet Total": bad["sheet_total"].values,
                              "Difference": (bad["revenue"] - bad["sheet_total"]).values})
            show_table(b, money_cols=("Rate (₹/pc)", "Calculated", "Sheet Total", "Difference"), height=360,
                       key_col="Difference", sort_abs=True)


# -----------------------------------------------------------------------------
# 10B. FLO FUTURE DASHBOARD  (sheet: "FLO_Future Nov 2026")
#      A) Source tab "Samples" -> row 1 = header, one row per promised sample (filled by hand). Columns by header name
#         (fallback position A=0 ...): Designer | Team | Collection / Sample | Vendor | Season | Qty Promised |
#         Completed | Pending | Status | Due Date | Overdue | Promised To | Contact | Remark | Key (auto)
#         Pending / Overdue / Days Left are re-calculated here with the SAME rule as the sheet formulas
#         (Pending = Qty - Completed, Overdue = Due Date passed and Status not 'Complete'); the sheet's own
#         Pending / Overdue columns are only cross-checked at the bottom.
#      B) Source tab "Visits & Meetings" -> blocks: "Causative Milestone | <title>", then a header row
#         "Sr | Promised Action | <freq> | QTY. | | (Promised) By Whom | (Promised) to Whom | (Promised) By When-Date |
#         Complete (marked as 'X') | STATUS | REMARK", then numbered rows (empty rows = nothing planned).
# -----------------------------------------------------------------------------
FF_COLS = {          # field -> (accepted normalised header names, fallback column position A=0 ...)
    "designer": (("DESIGNER",), 0), "team": (("TEAM",), 1), "item": (("COLLECTIONSAMPLE", "COLLECTION"), 2),
    "vendor": (("VENDOR", "CLIENT"), 3), "season": (("SEASON",), 4), "qty": (("QTYPROMISED", "QTY"), 5),
    "done": (("COMPLETED",), 6), "pending_sheet": (("PENDING",), 7), "status": (("STATUS",), 8),
    "due": (("DUEDATE",), 9), "overdue_sheet": (("OVERDUE",), 10), "to": (("PROMISEDTO",), 11),
    "contact": (("CONTACT",), 12), "remark": (("REMARK",), 13),
}
FF_DUE_BUCKETS = ["Overdue", "Due Today", "Due in 1-3 Days", "Due in 4-7 Days", "Due Later (> 7 Days)",
                  "No Due Date", "Completed"]
FF_OPEN_BUCKETS = FF_DUE_BUCKETS[:-1]
FF_STATUS_ORDER = ["Complete", "In Progress", "In-House Process", "Sampling Ordered", "Corrections", "CAD In Queue",
                   "CAD In Process", "Not Started", "Not Updated"]
# Visits & Meetings: block title -> (activity type, required per month; 0 = as many as planned rows)
FF_VISIT_TYPES = [(r"MARKET", "Market Visit (Accessories)", 1), (r"STORE\s*/?\s*EXHIBITION|EXHIBI", "Store / Exhibition Visit", 1),
                  (r"SURAT", "Surat Sampling Visit", 1), (r"FACTORY", "Factory / Store Visit", 2),
                  (r"MEETING", "Meetings", 0)]
FF_VISIT_STATES = ["Done", "Missed / Not Marked", "Today", "Upcoming", "Rescheduled", "Cancelled", "No Date"]


def ff_due_colors():
    return {"Overdue": COLORS["red"], "Due Today": COLORS["orange"], "Due in 1-3 Days": COLORS["amber"],
            "Due in 4-7 Days": COLORS["yellow"], "Due Later (> 7 Days)": COLORS["blue"],
            "No Due Date": COLORS["slate"], "Completed": COLORS["green"]}


def ff_visit_colors():
    return {"Done": COLORS["green"], "Missed / Not Marked": COLORS["red"], "Today": COLORS["orange"],
            "Upcoming": COLORS["blue"], "Rescheduled": COLORS["amber"], "Cancelled": COLORS["slate"],
            "No Date": COLORS["slate"]}


def ff_status_colors(statuses=()):
    base = {"Complete": COLORS["green"], "In Progress": COLORS["blue"], "In-House Process": COLORS["teal"],
            "Sampling Ordered": COLORS["purple"], "Corrections": COLORS["orange"], "CAD In Queue": COLORS["yellow"],
            "CAD In Process": COLORS["amber"], "Not Started": COLORS["red"], "Not Updated": COLORS["slate"]}
    p = pal()
    for i, s in enumerate(s_ for s_ in statuses if s_ not in base):
        base[s] = p[(i + 4) % len(p)]
    return base


def ff_rate_class(v):
    """Completion % colour: green >= 80 %, amber 40-79 %, red < 40 %."""
    return "r-good" if v >= 80 else ("r-mid" if v >= 40 else "r-bad")


# ============================== A) SAMPLES ==============================
def _ff_header_row(grid):
    for r in range(min(6, len(grid))):
        keys = {norm_key(c) for c in grid[r]}
        if "DESIGNER" in keys and ({"QTYPROMISED", "STATUS", "DUEDATE"} & keys):
            return r
    return None


def _ff_col_map(header):
    keys = [norm_key(h) for h in header]
    out = {}
    for field, (names, pos) in FF_COLS.items():
        idx = next((keys.index(n) for n in names if n in keys), None)
        if idx is None:
            idx = next((i for i, k in enumerate(keys) for n in names if k.startswith(n)), None)
        out[field] = idx if idx is not None else pos
    return out


def _ff_canon_status(s):
    """Status text -> one spelling (case / extra spaces ignored). Blank -> 'Not Updated'."""
    lookup = {norm_key(v): v for v in FF_STATUS_ORDER}
    return clean_text(s).map(lambda v: "Not Updated" if v == "" else lookup.get(norm_key(v), v.strip().title()))


def build_ff_samples(values, today=None):
    """Raw 'Samples' grid (unformatted values) -> one clean row per promised sample. Returns (df, header_found)."""
    if not values:
        return pd.DataFrame(), False
    hr = _ff_header_row(values)
    header_ok = hr is not None
    hr = hr if header_ok else 0
    cmap = _ff_col_map(values[hr])
    rows = values[hr + 1:]
    if not rows:
        return pd.DataFrame(), header_ok
    width = max(max(len(r) for r in rows), max(cmap.values()) + 1)
    # index = real sheet row number (header on sheet row hr+1 -> first data row hr+2)
    grid = pd.DataFrame([list(r) + [""] * (width - len(r)) for r in rows], index=range(hr + 2, hr + 2 + len(rows)))

    def col(field):
        return grid[cmap[field]]

    designer, item, qty_txt = clean_text(col("designer")), clean_text(col("item")), clean_text(col("qty"))
    qty, done = num(col("qty")), num(col("done"))
    keep = (designer != "") & ((item != "") | (qty != 0) | (done != 0))

    today = today if today is not None else _today_ist()
    d = pd.DataFrame(index=grid.index)
    d.index.name = "sheet_row"
    d["designer"] = designer.str.title()
    team = clean_text(col("team")).str.title()
    d["team"] = team.where(team != "", "(No Team)")
    d["item"] = item.where(item != "", "(No Name)")
    for f in ("vendor", "season", "to", "contact", "remark"):
        d[f] = clean_text(col(f))
    d["qty_blank"] = qty_txt == ""
    d["qty"], d["done"] = qty, done
    d["status"] = _ff_canon_status(col("status"))
    d["due"] = parse_dates(col("due")).dt.normalize()
    d["pending_sheet"] = num(col("pending_sheet"))
    d["overdue_sheet"] = clean_text(col("overdue_sheet")).str.upper().eq("OVERDUE")
    d = d[keep]
    if d.empty:
        return d, header_ok

    d["pending"] = np.where(d["qty_blank"], 0.0, (d["qty"] - d["done"]).clip(lower=0))
    d["is_complete"] = (d["status"] == "Complete") | (~d["qty_blank"] & (d["qty"] > 0) & (d["done"] >= d["qty"]))
    d["days_left"] = (d["due"] - today).dt.days            # NaN when no due date
    dl = d["days_left"]
    d["due_bucket"] = np.select(
        [d["is_complete"], dl.isna(), dl < 0, dl == 0, dl <= 3, dl <= 7],
        ["Completed", "No Due Date", "Overdue", "Due Today", "Due in 1-3 Days", "Due in 4-7 Days"],
        default="Due Later (> 7 Days)")
    d["is_overdue"] = d["due_bucket"] == "Overdue"
    d["days_overdue"] = np.where(d["is_overdue"], -dl, 0).astype(float)
    return d, header_ok


def _ff_days_text(row):
    if row["is_complete"]:
        return "Done"
    dl = row["days_left"]
    if pd.isna(dl):
        return "No date"
    dl = int(dl)
    if dl < 0:
        return f"{-dl} day{'s' if dl != -1 else ''} late"
    return "Due today" if dl == 0 else f"{dl} day{'s' if dl != 1 else ''} left"


def _ff_days_class(row):
    if row["is_complete"]:
        return "r-good"
    dl = row["days_left"]
    if pd.isna(dl):
        return "zero"
    return "r-bad" if dl < 0 else ("r-mid" if dl <= 3 else "r-good")


def _ff_date(s):
    return s.dt.strftime("%d-%b-%y").fillna("")


def ff_stats(g):
    """One summary row for any slice of promised samples (totals recomputed, % never summed)."""
    tgt, done, pen = float(g["qty"].sum()), float(g["done"].sum()), float(g["pending"].sum())
    od = g[g["is_overdue"]]
    soon = g[g["due_bucket"].isin(["Due Today", "Due in 1-3 Days"])]
    return {"Items": int(len(g)), "Target Qty": tgt, "Completed": done, "Pending": pen,
            "Done %": safe_div_pct(done, tgt), "Items Done": int(g["is_complete"].sum()),
            "Overdue Items": int(len(od)), "Overdue Pcs": float(od["pending"].sum()),
            "Due ≤ 3 Days": int(len(soon)), "No Due Date": int((g["due_bucket"] == "No Due Date").sum())}


def _ff_nearest_due(g):
    open_ = g[~g["is_complete"] & g["due"].notna()]
    if open_.empty:
        return "—"
    r = open_.loc[open_["due"].idxmin()]
    return f"{r['due'].strftime('%d-%b')} ({_ff_days_text(r)})"


def ff_group(x, col, label, order=None):
    keys = order if order is not None else list(dict.fromkeys(x[col]))
    rows = [{label: k, **ff_stats(x[x[col] == k]), "Nearest Open Due": _ff_nearest_due(x[x[col] == k])}
            for k in keys if (x[col] == k).any()]
    return pd.DataFrame(rows)


def _ff_chip(text, color):
    return (f'<span style="display:inline-block;padding:2px 9px;border-radius:12px;font-size:.72rem;font-weight:700;'
            f'background:{_hex_to_rgba(color, 0.22 if IS_DARK else 0.14)};color:{color};'
            f'border:1px solid {_hex_to_rgba(color, 0.45)};white-space:nowrap">{_esc(text)}</span>')


def ff_item_table(x, show_designer=False, height=None, total_label="TARGET"):
    """Item-level HTML table (like the sheet's 'Designer Wise' tab): status pill + coloured Days Left + total row."""
    if x.empty:
        notice("Selected filters ke liye data nahi hai.")
        return
    sc = ff_status_colors(x["status"].unique())
    heads = ["Sr"] + (["Designer"] if show_designer else []) + [
        "Collection / Sample", "Vendor", "Qty", "Completed", "Pending", "Status", "Due Date", "Days Left",
        "Promised To", "Remark"]
    body = []
    for i, (_, r) in enumerate(x.iterrows(), start=1):
        qty_txt = "—" if r["qty_blank"] else f"{r['qty']:,.0f}"
        due_txt = r["due"].strftime("%d-%b-%y") if pd.notna(r["due"]) else "—"
        pen_cls = ' class="zero"' if r["pending"] == 0 else ""
        cells = [f"<td style='min-width:36px;text-align:center'>{i}</td>"]
        if show_designer:
            cells.append(f"<td style='text-align:left;font-weight:600'>{_esc(r['designer'])}</td>")
        cells += [
            f"<td style='text-align:left;white-space:normal;min-width:190px'>{_esc(r['item'])}</td>",
            f"<td style='text-align:left'>{_esc(r['vendor'])}</td>",
            f"<td>{qty_txt}</td>", f"<td>{r['done']:,.0f}</td>", f"<td{pen_cls}>{r['pending']:,.0f}</td>",
            f"<td style='text-align:left'>{_ff_chip(r['status'], sc.get(r['status'], COLORS['slate']))}</td>",
            f"<td>{due_txt}</td>",
            f'<td class="{_ff_days_class(r)}">{_esc(_ff_days_text(r))}</td>',
            f"<td style='text-align:left'>{_esc(r['to'])}</td>",
            f"<td style='text-align:left;white-space:normal;min-width:150px'>{_esc(r['remark'])}</td>"]
        body.append("<tr>" + "".join(cells) + "</tr>")
    s = ff_stats(x)
    pad = 1 if show_designer else 0
    tot = (f'<tr class="tot"><td>{_esc(total_label)}</td>' + "<td></td>" * (2 + pad)
           + f"<td>{s['Target Qty']:,.0f}</td><td>{s['Completed']:,.0f}</td><td>{s['Pending']:,.0f}</td>"
           + f"<td style='text-align:left'>{s['Done %']:.0f}% done · {s['Items Done']}/{s['Items']} items</td>"
           + "<td></td>" + f"<td>{s['Overdue Items']} overdue</td>" + "<td></td>" * 2 + "</tr>")
    style = f' style="max-height:{int(height)}px"' if height else ""
    head = "".join(f"<th style='min-width:36px;text-align:center'>{_esc(h)}</th>" if h == "Sr" else f"<th>{_esc(h)}</th>"
                   for h in heads)
    _html(f'<div class="tbl-wrap"{style}><table><thead><tr>{head}</tr></thead>'
          f'<tbody>{"".join(body)}{tot}</tbody></table></div>')


# ============================== B) VISITS & MEETINGS ==============================
def _ff_visit_type(title):
    t = str(title).upper()
    for pat, name, req in FF_VISIT_TYPES:
        if re.search(pat, t):
            return name, req
    return (str(title).strip().title() or "Other"), 0


def build_ff_visits(values, today=None):
    """'Visits & Meetings' block grid -> (activities df, blocks df). Empty numbered rows = nothing planned."""
    today = today if today is not None else _today_ist()
    acts, blocks = [], []
    n = len(values)
    r = 0
    while r < n:
        row = values[r]
        ci = next((c for c, v in enumerate(row) if norm_key(v).startswith("CAUSATIVEMILESTONE")), None)
        if ci is None:
            r += 1
            continue
        title = next((str(v).strip() for v in row[ci + 1:] if str(v).strip()), "")
        vtype, req = _ff_visit_type(title)
        # header row = first row below with 'Sr' in it
        h = next((k for k in range(r + 1, min(r + 4, n)) if any(norm_key(v) == "SR" for v in values[k])), None)
        if h is None:
            r += 1
            continue
        keys = [norm_key(v) for v in values[h]]

        def find(*names, default=None):
            return next((i for i, k in enumerate(keys) for nm in names if k.startswith(nm)), default)

        c_sr = find("SR", default=0)
        c_act = find("PROMISEDACTION", default=c_sr + 1)
        c_freq = c_act + 1
        c_by = find("PROMISEDBYWHOM", default=c_act + 4)
        c_to = find("PROMISEDTOWHOM", "PROMISEDTO", default=c_act + 5)
        c_when = find("PROMISEDBYWHEN", default=c_act + 6)
        c_x = find("COMPLETE", default=c_act + 7)
        c_st = find("STATUS", default=c_act + 8)
        c_rm = find("REMARK", default=c_act + 9)
        freq_hdr = str(values[h][c_freq]).strip() if c_freq < len(values[h]) else ""
        planned = 0
        k = h + 1
        while k < n and not any(norm_key(v).startswith("CAUSATIVEMILESTONE") for v in values[k]):
            cells = list(values[k]) + [""] * 20

            def cell(i):
                return cells[i] if i is not None and i < len(cells) else ""

            act = str(cell(c_act)).strip()
            if act:
                planned += 1
                due = parse_dates(pd.Series([cell(c_when)])).iloc[0]
                due = due.normalize() if pd.notna(due) else pd.NaT
                st_txt = str(cell(c_st)).strip()
                stn = norm_key(st_txt)
                x_mark = norm_key(cell(c_x)) in ("X", "YES", "DONE", "TRUE", "Y")
                if x_mark or stn in ("DONE", "COMPLETE", "COMPLETED"):
                    state = "Done"
                elif stn.startswith("CANCEL"):
                    state = "Cancelled"
                elif stn.startswith("RESCHED"):
                    state = "Rescheduled"
                elif pd.isna(due):
                    state = "No Date"
                elif due < today:
                    state = "Missed / Not Marked"
                elif due == today:
                    state = "Today"
                else:
                    state = "Upcoming"
                acts.append({"sheet_row": k + 1, "type": vtype, "milestone": title, "action": act.title(),
                             "freq": (str(cell(c_freq)).strip() or freq_hdr).title(),
                             "by": str(cell(c_by)).strip().title(), "to": str(cell(c_to)).strip(),
                             "date": due, "x": "X" if x_mark else "", "sheet_status": st_txt,
                             "remark": str(cell(c_rm)).strip(), "state": state})
            k += 1
        blocks.append({"type": vtype, "milestone": title, "freq": freq_hdr.title(), "required": req,
                       "planned": planned, "sheet_row": r + 1})
        r = k
    a = pd.DataFrame(acts)
    if not a.empty:
        a["days_left"] = (a["date"] - today).dt.days
    return a, pd.DataFrame(blocks)


def ff_visit_summary(a, b):
    """One row per activity type: required this month vs planned vs done / missed / upcoming."""
    rows = []
    for vtype in dict.fromkeys(b["type"]) if not b.empty else []:
        g = a[a["type"] == vtype] if not a.empty else pd.DataFrame(columns=["state"])
        planned = int(b.loc[b["type"] == vtype, "planned"].sum())
        req = int(b.loc[b["type"] == vtype, "required"].max())
        req = max(req, planned) if req else planned
        done = int((g["state"] == "Done").sum())
        rows.append({"Activity": vtype, "Required (Month)": req, "Planned": planned, "Done": done,
                     "Missed / Not Marked": int((g["state"] == "Missed / Not Marked").sum()),
                     "Upcoming": int(g["state"].isin(["Upcoming", "Today"]).sum()),
                     "Not Planned Yet": max(req - planned, 0), "Done %": safe_div_pct(done, req)})
    return pd.DataFrame(rows)


# ============================== RENDER ==============================
def render_flo_future_dashboard(sheet_name, cfg):
    tab = resolve_source_tab(cfg, name_regex=r"^\s*samples?\s*$")
    if tab:
        run_live(_ff_body, sheet_name, cfg, tab)


def _ff_load_visits(cfg):
    """Visits & Meetings tab -> (activities, blocks, tab name or None). Never stops the samples dashboard."""
    try:
        titles = [t for t, _ in list_tabs_meta(cfg["id"])]
        tab = next((t for t in (find_tab(titles, nm) for nm in cfg.get("visits_tabs", ["Visits & Meetings"])) if t), None)
        if tab is None:
            tab = next((t for t in titles if re.search(r"visit|meeting", t, re.I)), None)
        if tab is None:
            return pd.DataFrame(), pd.DataFrame(), None
        a, b = build_ff_visits(fetch_grid(cfg["id"], tab)["values"])
        return a, b, tab
    except SheetAccessError:
        raise
    except Exception:
        return pd.DataFrame(), pd.DataFrame(), None


def _ff_body(sheet_name, cfg, tab):
    sheet_id = cfg["id"]
    try:
        pack = fetch_grid(sheet_id, tab)
        va, vb, vtab = _ff_load_visits(cfg)
    except SheetAccessError as e:
        show_access_error(e)
        return
    today = _today_ist()
    d, header_ok = build_ff_samples(pack["values"], today)
    period = cfg.get("period_label", "")
    hero("🔮", f"FLO Future – Promised Actions{(' · ' + period) if period else ''}", None, pack["synced"])
    if not header_ok:
        notice(f"'{tab}' tab me header row (Designer / Qty Promised / Status / Due Date) nahi mili - default columns "
               "(A..N) use hue hain, numbers check kar lein.", "warning")
    if d.empty:
        notice(f"'{tab}' tab me data nahi mila. Expected: row 1 = header (Designer, Team, Collection / Sample, Vendor, "
               "Season, Qty Promised, Completed, Pending, Status, Due Date ...), row 2 se data.", "warning")
        _ff_visits_chart(va, vb, vtab)
        return

    team_order = sorted(d["team"].unique(), key=lambda t: (t.startswith("("), t))
    des_order = list(dict.fromkeys(d.sort_values("team", key=lambda s: s.map(team_order.index), kind="mergesort")
                                   ["designer"]))
    statuses = [s for s in FF_STATUS_ORDER if s in set(d["status"])] + \
               sorted(s for s in d["status"].unique() if s not in FF_STATUS_ORDER)

    # ---------------- filters ----------------
    keys = {"designer": "ff_designer", "team": "ff_team", "status": "ff_status", "due": "ff_due"}
    badge("🔎 Filters  (kuch select na karein = ALL)  ·  Samples par lagte hain")
    c = st.columns([1.4, 1.0, 1.3, 1.3, 0.7], vertical_alignment="bottom")
    f_des = multi(c[0], "Designer", des_order, keys["designer"], "All designers")
    f_team = multi(c[1], "Team", team_order, keys["team"], "All teams")
    f_status = multi(c[2], "Status", statuses, keys["status"], "All status")
    f_due = multi(c[3], "Due Status", FF_DUE_BUCKETS, keys["due"], "All due status")
    c[4].button("↺ Reset", key="ff_reset", on_click=_reset_filters, args=({k: [] for k in keys.values()},),
                **STRETCH)

    def flt(skip=()):
        y = d
        for name, sel, colname in (("designer", f_des, "designer"), ("team", f_team, "team"),
                                   ("status", f_status, "status"), ("due", f_due, "due_bucket")):
            if sel and name not in skip:
                y = y[y[colname].isin(sel)]
        return y

    x = flt()
    x_nd = flt(skip=("designer",))       # designer comparison: all designers
    st.caption(f"Aaj: {today.strftime('%d %b %Y')}  ·  Pending = Qty Promised − Completed  ·  Overdue = Due Date nikal "
               "gayi aur Status 'Complete' nahi (sheet wala rule)  ·  Days Left = Due Date − aaj.")

    # ---------------- KPIs ----------------
    vs = ff_visit_summary(va, vb) if not vb.empty else pd.DataFrame()
    if x.empty:
        notice("Selected filters ke liye koi promised sample nahi mila.")
    else:
        s = ff_stats(x)
        dg = ff_group(x, "designer", "Designer", [n for n in des_order if n in set(x["designer"])])
        most_pen = dg.loc[dg["Pending"].idxmax()] if len(dg) and dg["Pending"].max() > 0 else None
        worst = x.loc[x["days_overdue"].idxmax()] if x["days_overdue"].max() > 0 else None
        soon_pcs = x.loc[x["due_bucket"].isin(["Due Today", "Due in 1-3 Days"]), "pending"].sum()
        tiles = [("Target Qty (Promised)", f"{fmt(s['Target Qty'])} Pcs"),
                 ("Completed", f"{fmt(s['Completed'])} Pcs"),
                 ("Pending", f"{fmt(s['Pending'])} Pcs"),
                 ("Completion %", f"{s['Done %']:.1f}%"),
                 ("Overdue (Date Passed)", f"{s['Overdue Items']} Items · {fmt(s['Overdue Pcs'])} Pcs"),
                 ("Due in Next 3 Days", f"{s['Due ≤ 3 Days']} Items · {fmt(soon_pcs)} Pcs"),
                 ("⚠ Most Pending", f"{most_pen['Designer']} ({fmt(most_pen['Pending'])} Pcs)"
                                   if most_pen is not None else "None"),
                 ("⏰ Longest Overdue", f"{int(worst['days_overdue'])} Days · {worst['designer']}"
                                       if worst is not None else "None")]
        if not vs.empty:
            tiles.append(("Visits & Meetings Done", f"{int(vs['Done'].sum())} / {int(vs['Required (Month)'].sum())}"))
        badge("📊 Key Performance Indicators")
        metrics(tiles)


        # ================= 1) CHART (full width, right after KPIs): designer-wise target vs completed =================
        dsc = ff_group(x_nd, "designer", "Designer", [n for n in des_order if n in set(x_nd["designer"])])
        team_of = d.groupby("designer")["team"].agg(lambda v: v.mode().iloc[0])
        dsc.insert(1, "Team", dsc["Designer"].map(team_of))
        st.markdown("---")
        badge("👩‍🎨 Designer-wise Target vs Completed")
        st.caption("Designer filter yahan apply nahi hota taaki sab designers ki tulna ho sake; baaki filters apply hote hain.")
        _ff_designer_chart(dsc, x_nd)

        # ================= 2) TABLE: designer-wise numbers =================
        st.markdown("---")
        badge("📊 Designer-wise Table")
        st.caption("Done % ka rang: 🟢 ≥80%  🟡 40-79%  🔴 <40%.")
        cols = ["Designer", "Team", "Items", "Target Qty", "Completed", "Pending", "Done %", "Overdue Items",
                "Due ≤ 3 Days", "Nearest Open Due"]
        tbl = with_total(dsc[cols], "Designer", pct=("Done %", "Completed", "Target Qty"))
        tbl.loc[tbl["Designer"] == "TOTAL", ["Team", "Nearest Open Due"]] = ["", _ff_nearest_due(x_nd)]
        table_with_download(tbl, "flo_future_designer", pct_cols=("Done %",), rate_cols=("Done %",),
                            rate_fn=ff_rate_class, key_col="Target Qty", height=560)

        # ================= 3) TABLE: every sample, most urgent first =================
        st.markdown("---")
        badge("📋 Sample Detail (sab items · sabse urgent upar)")
        st.caption("Upar ke saare filters yahan lagte hain - ek designer chuno to uski poori list. "
                   "Days Left: 🔴 late  🟡 0-3 din  🟢 3+ din / done.")
        rank = x["due_bucket"].map({b: i for i, b in enumerate(FF_DUE_BUCKETS)})
        ff_item_table(x.assign(_r=rank).sort_values(["_r", "days_left", "designer"], na_position="last"),
                      show_designer=True, height=620, total_label="TOTAL")
        full = _ff_export(x)
        st.download_button("⬇️ Download CSV", full.to_csv(index=False).encode("utf-8-sig"),
                           file_name="flo_future_samples.csv", mime="text/csv", key="dl_ff_samples")

    # ================= 4) CHART: visits & meetings =================
    _ff_visits_chart(va, vb, vtab, vs)


def _ff_designer_chart(dsc, x_nd):
    """One horizontal stacked bar per designer. Full bar = Target Qty, split into
    Completed | Pending: overdue | Pending: due in 0-3 days | Pending: due later | Pending: no due date.
    Label at the end: done / target · Done % · overdue / due-soon items. Hover shows everything for that designer."""
    if dsc.empty:
        notice("Selected filters ke liye data nahi hai.")
        return
    seg = {"Overdue": ["Overdue"], "Due in 0-3 Days": ["Due Today", "Due in 1-3 Days"],
           "Due Later": ["Due in 4-7 Days", "Due Later (> 7 Days)"], "No Due Date": ["No Due Date"]}
    pen = {k: x_nd[x_nd["due_bucket"].isin(v)].groupby("designer")["pending"].sum() for k, v in seg.items()}
    o = dsc.sort_values(["Target Qty", "Designer"], ascending=[True, False]).reset_index(drop=True)
    names = list(o["Designer"])
    ylab = [f"<b>{n}</b> · {t}" for n, t in zip(names, o["Team"])]
    parts = {k: o["Designer"].map(v).fillna(0.0).to_numpy() for k, v in pen.items()}
    hover = [(f"<b>{r['Designer']}</b> · {r['Team']}<br>Target: {fmt(r['Target Qty'])} pcs ({r['Items']} items)"
              f"<br>Completed: {fmt(r['Completed'])} pcs ({r['Done %']:.1f}%)<br>Pending: {fmt(r['Pending'])} pcs"
              f"<br>Overdue: {r['Overdue Items']} items · {fmt(parts['Overdue'][i])} pcs"
              f"<br>Due in 0-3 days: {r['Due ≤ 3 Days']} items · {fmt(parts['Due in 0-3 Days'][i])} pcs"
              f"<br>Nearest open due: {r['Nearest Open Due']}") for i, r in o.iterrows()]
    colors = {"Completed": COLORS["green"], "Overdue": COLORS["red"], "Due in 0-3 Days": COLORS["orange"],
              "Due Later": _hex_to_rgba(COLORS["blue"], 0.55), "No Due Date": _hex_to_rgba(COLORS["slate"], 0.55)}
    fig = go.Figure()
    series = [("Completed", o["Completed"].to_numpy())] + [(f"Pending · {k}", parts[k]) for k in seg]
    for name, vals in series:
        key = name.replace("Pending · ", "")
        fig.add_trace(go.Bar(name=name, y=ylab, x=vals, orientation="h", marker_color=colors[key],
                             marker_line=dict(width=0), text=[fmt(v) if v >= 1 else "" for v in vals],
                             textposition="inside", insidetextanchor="middle",
                             textfont=dict(color="#ffffff", size=11), customdata=hover,
                             hovertemplate="%{customdata}<extra>" + name + "</extra>"))
    # end label: done / target · % · alerts
    top = float(np.maximum(o["Target Qty"], o["Completed"] + o["Pending"]).max())
    for i, r in o.iterrows():
        tail = f"{fmt(r['Completed'])}/{fmt(r['Target Qty'])} · {r['Done %']:.0f}%"
        if r["Overdue Items"]:
            tail += f"  🔴 {r['Overdue Items']} late"
        if r["Due ≤ 3 Days"]:
            tail += f"  🟠 {r['Due ≤ 3 Days']} due soon"
        end = max(float(r["Target Qty"]), float(r["Completed"] + r["Pending"]))
        fig.add_annotation(x=end, y=ylab[i], text=tail, showarrow=False, xanchor="left", xshift=6,
                           font=dict(size=11, color=PLOT_TEXT))
    fig.update_layout(barmode="stack", bargap=0.28, legend_traceorder="normal")
    fig.update_xaxes(title="Pcs (poori bar = Target)", range=[0, top * 1.38 + 1])
    fig.update_yaxes(title="")
    tot = dsc[["Target Qty", "Completed", "Pending"]].sum()
    chart(fig, "Designer-wise Target vs Completed (Pcs)", max(440, 40 * len(o) + 150), ns="ff",
          subtitle=f"Total {fmt(tot['Completed'])} / {fmt(tot['Target Qty'])} done "
                   f"({safe_div_pct(tot['Completed'], tot['Target Qty']):.1f}%) · hari = done · laal = date nikal gayi · "
                   "orange = 3 din me due · blue = baad me due · grey = due date nahi · mouse le jao to poori detail")


def _ff_export(x):
    return pd.DataFrame({
        "Sheet Row": x.index, "Designer": x["designer"].values, "Team": x["team"].values,
        "Collection / Sample": x["item"].values, "Vendor": x["vendor"].values, "Season": x["season"].values,
        "Qty Promised": np.where(x["qty_blank"], np.nan, x["qty"]), "Completed": x["done"].values,
        "Pending": x["pending"].values, "Status": x["status"].values, "Due Date": _ff_date(x["due"]).values,
        "Days Left": x["days_left"].values, "Due Status": x["due_bucket"].values, "Promised To": x["to"].values,
        "Remark": x["remark"].values})


def _ff_visits_chart(va, vb, vtab, vs=None):
    """One chart: per activity -> Done / Missed (not marked) / Upcoming / Not planned yet. Hover lists the activities."""
    st.markdown("---")
    badge("🗓️ Visits & Meetings (is mahine)")
    if vtab is None or vb.empty:
        notice("'Visits & Meetings' tab nahi mila ya usme 'Causative Milestone' blocks nahi mile.", "warning")
        return
    vs = vs if vs is not None and not vs.empty else ff_visit_summary(va, vb)
    vc = ff_visit_colors()

    def names(vtype, states):
        if va.empty:
            return ""
        g = va[(va["type"] == vtype) & va["state"].isin(states)].sort_values("date")
        return "<br>".join(f"• {r['action']} · {r['by']} · "
                           f"{r['date'].strftime('%d %b') if pd.notna(r['date']) else 'no date'}" for _, r in g.iterrows())

    o = vs.iloc[::-1]
    fig = go.Figure()
    for name, colname, color, states in (
            ("Done", "Done", vc["Done"], ["Done"]),
            ("Missed / Not Marked", "Missed / Not Marked", vc["Missed / Not Marked"], ["Missed / Not Marked"]),
            ("Upcoming", "Upcoming", vc["Upcoming"], ["Upcoming", "Today"]),
            ("Not Planned Yet", "Not Planned Yet", COLORS["slate"], [])):
        fig.add_trace(go.Bar(name=name, y=list(o["Activity"]), x=o[colname], orientation="h", marker_color=color,
                             text=[int(v) if v else "" for v in o[colname]], textposition="inside",
                             customdata=[names(a, states) for a in o["Activity"]],
                             hovertemplate="<b>%{y}</b><br>" + name + ": %{x}<br>%{customdata}<extra></extra>"))
    fig.update_layout(barmode="stack", legend_traceorder="normal")
    fig.update_xaxes(title="Count", dtick=1)
    fig.update_yaxes(title="")
    done, req = int(vs["Done"].sum()), int(vs["Required (Month)"].sum())
    chart(fig, "Visits & Meetings: Required vs Done", max(320, 52 * len(o) + 130), ns="ff",
          subtitle=f"{done} / {req} done · bar par mouse le jao to naam aur date dikhenge · "
                   "Done = 'X' ya STATUS 'Done' · Missed = date nikal gayi par mark nahi")


# -----------------------------------------------------------------------------
# 11. MANAGEMENT OVERVIEW  (first tab)
#     One page for management: Orders / Production / Client / Designer (Master Sheet), Sample, Fabricator stock
#     and Fabric PO.  Target & Revenue is NOT included (it has its own tab).
#     Every number re-uses the SAME calculation functions as the individual dashboards, so they always match.
# -----------------------------------------------------------------------------
def _cfg_of(kind):
    return next(c for c in SHEET_REGISTRY.values() if c.get("type") == kind)


def _ovw_load(kind):
    """Loads one source exactly like its own dashboard. Returns (data or None, error text or None)."""
    cfg = _cfg_of(kind)
    try:
        if kind == "tracker":
            return load_master(cfg), None
        if kind == "sample_summary":
            tab = resolve_source_tab(cfg)
            return (build_samples(fetch_grid(cfg["id"], tab)["values"])[0] if tab else None), None
        if kind == "overview_fabricators":
            tab = resolve_source_tab(cfg, name_regex=r"monthwise|monthly\s*summary")
            if not tab:
                return None, None
            _, df = build_fab_data(fetch_grid(cfg["id"], tab, True)["values"], fetch_grid(cfg["id"], tab)["values"])
            return df, None
        if kind == "fabric_po":
            tab = resolve_source_tab(cfg, name_regex=r"^\s*index\s*$")
            return (build_fabric_po(fetch_grid(cfg["id"], tab)["values"])[0] if tab else None), None
    except SheetAccessError as e:
        return None, str(e)
    return None, None


def _ovw_order_stats(g):
    """Order / production numbers for any slice - same definitions as the Production dashboard KPIs."""
    k = {lbl: val for lbl, _sheet, val, _kind in production_kpis(g)}
    return {"Designs": int(k["TOTAL DESIGN"]), "Order Qty": float(g["qty"].sum()),
            "In Production": float(k["INPRODUCTION PCS (AS)"]), "Received": float(k["RECEIVED PCS (AT)"]),
            "Pending": float(k["PENDING PCS (AU)"]), "Overdue Pcs": float(k["OVERDUE PCS (AH > 0)"]),
            "Avg Overdue Days": float(k["AVG OVERDUE DAYS"]), "On-Time %": float(k["ON-TIME DELIVERY %"])}


def _ovw_group(x, col, label, stats_fn, cols=None):
    """One row per group (+ TOTAL row computed on the whole slice, so averages / % are never summed)."""
    rows = [{label: k, **stats_fn(g)} for k, g in x.groupby(col, sort=False)]
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    num_cols = [c for c in df.columns if c != label]
    df = df[(df[num_cols].abs().sum(axis=1) > 0)]
    df = pd.concat([df, pd.DataFrame([{label: "TOTAL", **stats_fn(x)}])], ignore_index=True)
    return df[[label] + cols] if cols else df


def _pcs(v):
    return f"{fmt(v)} Pcs"


def _top(df, label, col):
    """(name, value) of the biggest row (TOTAL ignored)."""
    if df is None or df.empty:
        return None, 0.0
    b = df[df[label] != "TOTAL"]
    if b.empty or b[col].max() <= 0:
        return None, 0.0
    r = b.loc[b[col].idxmax()]
    return r[label], float(r[col])


def render_overview_dashboard():
    run_live(_overview_body)


def _overview_body():
    dash_title("📌 Management Overview", chip=f"🕒 Updated {now_label()}")
    st.caption("Designer, Production, Client, Sample, Fabricator aur Fabric PO dashboards ka ek page summary "
               "(Target & Revenue apne tab me hai). Har number wahi calculation use karta hai jo us dashboard me hai.")

    # ---------------- filter ----------------
    badge("🔎 Filter")
    c = st.columns([1.3, 3.2, 0.75], vertical_alignment="bottom")
    months = month_filter(c[0], "Month", FY_MONTH_LABELS, "ovw_month", "All months")
    c[1].caption("Month ka matlab: Orders = Order Date · Samples = Process Date · Fabricator = Stock month · "
                 "Fabric PO = Order Date")
    c[2].button("↺ Reset", key="ovw_reset", on_click=_reset_filters, args=({"ovw_month": []},), **STRETCH)

    def by_month(df, col):
        if df is None or df.empty or not months:
            return df
        return df[df[col].isin(months)]

    _prefetch_sources(["tracker", "sample_summary", "overview_fabricators", "fabric_po"])
    master, m_err = _ovw_load("tracker")
    smp, s_err = _ovw_load("sample_summary")
    fab, f_err = _ovw_load("overview_fabricators")
    po, p_err = _ovw_load("fabric_po")

    xm = by_month(master, "month")
    xs = by_month(smp, "month")
    if fab is not None and not fab.empty:
        fab_base = fab[~fab["is_all"]] if (~fab["is_all"]).any() else fab[fab["is_all"]]
        xf = by_month(fab_base, "Month")
    else:
        xf = None
    xp = by_month(po, "month")

    # ======================= numbers (computed first, alerts are shown on top) =======================
    alerts = []

    # ---- Orders & production (Master Sheet) ----
    if xm is not None:
        ok = _ovw_order_stats(xm)
        dk = {lbl: val for lbl, _s, val, _k in designer_kpis(xm)}
        stat_cols = ["Designs", "Order Qty", "In Production", "Received", "Pending", "Overdue Pcs",
                     "Avg Overdue Days", "On-Time %"]
        short_cols = ["Order Qty", "In Production", "Received", "Pending", "Overdue Pcs"]
        by_fab = _ovw_group(xm, "fabricator", "Fabricator", _ovw_order_stats, stat_cols)
        by_cat = _ovw_group(xm, "category", "Category", _ovw_order_stats, short_cols)
        by_cli = _ovw_group(xm, "client", "Client", _ovw_order_stats, short_cols)
        n_overdue = int(((xm["production"] > 0) & (xm["delay"] > 0)).sum())
        if ok["Pending"] > 0:
            parts = []
            for lbl, df in (("Fabricator", by_fab), ("Category", by_cat), ("Client", by_cli)):
                name, val = _top(df, lbl, "Pending")
                if name is not None:
                    parts.append(f"{lbl}: {name} ({fmt(val)})")
            alerts.append(("warning", f"Orders: {_pcs(ok['Pending'])} pending out of {_pcs(ok['In Production'])} "
                                      f"in production ({safe_div_pct(ok['Pending'], ok['In Production']):.1f}%). "
                                      f"Sabse zyada pending - " + " · ".join(parts)))
        if ok["Overdue Pcs"] > 0:
            alerts.append(("error" if ok["On-Time %"] < 50 else "warning",
                           f"Overdue: {_pcs(ok['Overdue Pcs'])} across {n_overdue:,} designs, average "
                           f"{ok['Avg Overdue Days']:.1f} days late. On-time delivery {ok['On-Time %']:.1f}%."))
        elif ok["In Production"] > 0:
            alerts.append(("success", "Overdue: koi design delivery date se late nahi hai."))

    # ---- Fabricator stock (Fabricator Report) ----
    if xf is not None:
        ft = xf[FAB_METRICS].sum()
        f_pct = safe_div_pct(ft["RCVD (Pcs)"], ft["Pcs Under Production"])
        fg = xf.groupby("Fabricator", sort=False)[["Pcs Under Production", "RCVD (Pcs)", "Remaining (Pcs)"]].sum()
        fg = fg[fg.abs().sum(axis=1) > 0]
        fg["RCVD %"] = np.where(fg["Pcs Under Production"] > 0,
                                fg["RCVD (Pcs)"] / fg["Pcs Under Production"].replace(0, np.nan) * 100, 0.0)
        fg.index.name = "Fabricator"
        fab_tbl = with_total(fg.reset_index(), "Fabricator", pct=("RCVD %", "RCVD (Pcs)", "Pcs Under Production"))
        name, val = _top(fab_tbl, "Fabricator", "Remaining (Pcs)")
        if ft["Remaining (Pcs)"] > 0:
            alerts.append(("warning", f"Fabricator stock: {_pcs(ft['Remaining (Pcs)'])} remaining "
                                      f"({f_pct:.1f}% received)." + (f" Most pending: {name} ({fmt(val)})." if name else "")))

    # ---- Fabric PO ----
    if xp is not None and not xp.empty:
        ps = fabpo_stats(xp)
        overdue_po = xp[xp["timing"] == "Overdue (Pending)"]
        if len(overdue_po):
            w = overdue_po.loc[overdue_po["delay_days"].idxmax()]
            alerts.append(("error", f"Fabric PO: {len(overdue_po):,} POs overdue (abhi tak receive nahi hue). "
                                    f"{fmt(ps['Pending (mtr)'])} mtr fabric pending. Longest: {int(w['delay_days'])} days "
                                    f"- PO {w['po']} ({w['vendor']})."))
        else:
            alerts.append(("success", f"Fabric PO: koi overdue PO nahi. {fmt(ps['Pending (mtr)'])} mtr fabric pending."))

    # ---- Samples ----
    if xs is not None and not xs.empty:
        sb = xs.groupby("status")["qty"].sum()
        s_tot = float(xs["qty"].sum())
        s_app, s_rej, s_hld, s_prc = (float(sb.get(k, 0.0)) for k in ("Approved", "Rejected", "On Hold", "In Process"))
        s_ratio = safe_div_pct(s_app, s_tot)
        alerts.append(("success" if s_ratio >= 50 else "warning",
                       f"Samples: approval {s_ratio:.1f}% ({fmt(s_app)} of {fmt(s_tot)}), {fmt(s_rej)} rejected, "
                       f"{fmt(s_hld + s_prc)} on hold / in process."))

    # ======================= render =======================
    badge("🚨 Needs Attention")
    for label, err in (("Master Sheet", m_err), ("Sample Summary", s_err), ("Fabricator Report", f_err),
                       ("Fabric PO", p_err)):
        if err:
            notice(f"{label} load nahi ho payi: {err}", "error")
    if alerts:
        for kind, text in alerts:
            notice(text, kind)
    else:
        notice("Selected month ke liye koi data nahi hai.")

    # ---------------- Orders & Production ----------------
    if xm is not None:
        st.markdown("---")
        badge("📦 Orders & Production  (Master Sheet)")
        metrics([("Total Designs", f"{int(dk['TOTAL DESIGNS']):,}"), ("Order Qty", _pcs(ok["Order Qty"])),
                 ("New / Repeat", f"{fmt(dk['NEW ORDER QTY'])} / {fmt(dk['REPEAT ORDER QTY'])} Pcs"),
                 ("In Production", _pcs(ok["In Production"])), ("Received", _pcs(ok["Received"])),
                 ("Pending", _pcs(ok["Pending"])), ("Overdue Pcs", _pcs(ok["Overdue Pcs"])),
                 ("On-Time Delivery %", f"{ok['On-Time %']:.1f}%"),
                 ("Avg Overdue Days", f"{ok['Avg Overdue Days']:.1f} Days")])

        badge("🧵 Pending & Overdue by Fabricator")
        if by_fab.empty:
            notice("Selected month ke liye order data nahi hai.")
        else:
            table_with_download(by_fab, "overview_fabricator_pending", pct_cols=("On-Time %",),
                                dec_cols=("Avg Overdue Days",), rate_cols=("On-Time %",), key_col="Pending", height=440)
            st.caption("Overdue Pcs / Avg Overdue Days / On-Time % - Production dashboard wala rule (delay days > 0 = overdue).")

        c1, c2 = st.columns(2, gap="medium")
        with c1:
            badge("🧩 Pending by Category")
            table_with_download(by_cat, "overview_category_pending", key_col="Pending", height=420)
        with c2:
            badge("🏢 Pending by Client")
            table_with_download(by_cli, "overview_client_pending", key_col="Pending", height=420)

        c1, c2 = st.columns(2, gap="medium")
        with c1:
            axis = [m for m in month_list(master) if (not months or m in months)]
            mm = xm.groupby("month")[["received", "pending"]].sum().reindex(axis, fill_value=0)
            mm = mm[(mm.sum(axis=1) > 0) | mm.index.isin(FY_MONTH_LABELS)]
            fig = go.Figure()
            fig.add_trace(go.Bar(name="Received", x=list(mm.index), y=mm["received"], marker_color=COLORS["green"]))
            fig.add_trace(go.Bar(name="Pending", x=list(mm.index), y=mm["pending"], marker_color=COLORS["red"]))
            fig.update_layout(barmode="stack")
            fig.update_yaxes(title="Pcs")
            chart(fig, "Monthly Orders: Received vs Pending (Pcs, Order month)", 400, ns="ovw")
        with c2:
            fb = by_fab[by_fab["Fabricator"] != "TOTAL"].sort_values("Pending") if not by_fab.empty else by_fab
            if fb.empty:
                empty_card("Fabricator: Received vs Pending (Pcs)", "Data nahi hai.", ns="ovw")
            else:
                fig = go.Figure()
                fig.add_trace(go.Bar(name="Received", y=list(fb["Fabricator"]), x=fb["Received"], orientation="h",
                                     marker_color=COLORS["green"]))
                fig.add_trace(go.Bar(name="Pending", y=list(fb["Fabricator"]), x=fb["Pending"], orientation="h",
                                     marker_color=COLORS["red"]))
                fig.update_layout(barmode="stack", legend_traceorder="normal")
                chart(fig, "Fabricator: Received vs Pending (Pcs)", max(400, 28 * len(fb) + 120), ns="ovw")

    # ---------------- Designer snapshot (orders + samples) ----------------
    if xm is not None or (xs is not None and not xs.empty):
        st.markdown("---")
        badge("👩‍🎨 Designer Snapshot  (Orders + Samples)")
        parts = []
        if xm is not None:
            g = xm.groupby("designer")
            parts.append(pd.DataFrame({
                "Order Qty": g["qty"].sum(),
                "New Pcs": xm[xm["order_type"] == "NEW"].groupby("designer")["qty"].sum(),
                "Repeat Pcs": xm[xm["order_type"] == "REPEAT"].groupby("designer")["qty"].sum(),
                "Pending Pcs": g["pending"].sum()}))
        if xs is not None and not xs.empty:
            sd = xs.assign(designer=xs["designer"].str.upper().replace({"(BLANK)": "(NOT ASSIGNED)"}))
            parts.append(pd.DataFrame({
                "Samples": sd.groupby("designer")["qty"].sum(),
                "Approved": sd[sd["status"] == "Approved"].groupby("designer")["qty"].sum()}))
        des = pd.concat(parts, axis=1).fillna(0.0)
        for col_ in ("Order Qty", "New Pcs", "Repeat Pcs", "Pending Pcs", "Samples", "Approved"):
            if col_ not in des.columns:
                des[col_] = 0.0
        des = des[des.abs().sum(axis=1) > 0]
        des["Approval %"] = np.where(des["Samples"] > 0, des["Approved"] / des["Samples"].replace(0, np.nan) * 100, 0.0)
        des.index.name = "Designer"
        des = des.reset_index()[["Designer", "Order Qty", "New Pcs", "Repeat Pcs", "Pending Pcs", "Samples",
                                 "Approved", "Approval %"]]
        des = with_total(des, "Designer", pct=("Approval %", "Approved", "Samples"))
        table_with_download(des, "overview_designer", pct_cols=("Approval %",), key_col="Order Qty", height=460)
        st.caption("Order Qty / Pending = Master Sheet · Samples / Approved = Sample sheet (designer naam se joda gaya).")

    # ---------------- Fabricator stock ----------------
    if xf is not None:
        st.markdown("---")
        badge("🏭 Fabricator Stock Position  (Fabricator Report)")
        metrics([("Fabric Issued", f"{ft['Fabric Issue (mtr)']:,.0f} mtr"),
                 ("Pcs Under Production", _pcs(ft["Pcs Under Production"])), ("RCVD", _pcs(ft["RCVD (Pcs)"])),
                 ("Remaining", _pcs(ft["Remaining (Pcs)"])), ("Received %", f"{f_pct:.1f}%"),
                 ("Monthly Incoming", _pcs(ft["Monthly Incoming (Pcs)"]))])
        c1, c2 = st.columns([1.15, 1], gap="medium")
        with c1:
            table_with_download(fab_tbl, "overview_fabricator_stock", pct_cols=("RCVD %",),
                                key_col="Remaining (Pcs)", height=440)
        with c2:
            o = fab_tbl[fab_tbl["Fabricator"] != "TOTAL"].sort_values("Remaining (Pcs)")
            if o.empty:
                empty_card("Fabricator Stock: RCVD vs Remaining (Pcs)", "Data nahi hai.", ns="ovw")
            else:
                fig = go.Figure()
                fig.add_trace(go.Bar(name="RCVD", y=list(o["Fabricator"]), x=o["RCVD (Pcs)"], orientation="h",
                                     marker_color=COLORS["green"]))
                fig.add_trace(go.Bar(name="Remaining", y=list(o["Fabricator"]), x=o["Remaining (Pcs)"],
                                     orientation="h", marker_color=COLORS["red"]))
                fig.update_layout(barmode="stack", legend_traceorder="normal")
                chart(fig, "Fabricator Stock: RCVD vs Remaining (Pcs)", max(400, 30 * len(o) + 120), ns="ovw")

    # ---------------- Fabric PO ----------------
    if xp is not None and not xp.empty:
        st.markdown("---")
        badge("🧾 Fabric PO  (Fabric ordering & receiving)")
        metrics([("Total POs", f"{ps['POs']:,} POs"), ("Fabric Ordered", f"{ps['Fabric Ordered (mtr)']:,.0f} mtr"),
                 ("Fabric Received", f"{ps['Fabric Rcvd (mtr)']:,.0f} mtr"),
                 ("Pending Fabric", f"{ps['Pending (mtr)']:,.0f} mtr"), ("Received %", f"{ps['RCVD %']:.1f}%"),
                 ("Overdue (Not Received)", f"{len(overdue_po):,} POs"), ("Delayed POs", f"{ps['Delayed POs']:,} POs"),
                 ("On-Time Delivery %", f"{ps['On-Time %']:.1f}%")])

        def _po_stats(g):
            s = fabpo_stats(g)
            return {"POs": s["POs"], "Fabric Ordered (mtr)": s["Fabric Ordered (mtr)"],
                    "Pending (mtr)": s["Pending (mtr)"], "RCVD %": s["RCVD %"],
                    "Overdue POs": int((g["timing"] == "Overdue (Pending)").sum()), "Delayed POs": s["Delayed POs"],
                    "Avg Delay (days)": s["Avg Delay (days)"], "On-Time %": s["On-Time %"]}
        badge("🏭 Vendor: Pending & Delays")
        vend = _ovw_group(xp, "vendor", "Vendor", _po_stats)
        table_with_download(vend, "overview_po_vendor", pct_cols=("RCVD %", "On-Time %"),
                            dec_cols=("Fabric Ordered (mtr)", "Pending (mtr)", "Avg Delay (days)"),
                            rate_cols=("RCVD %", "On-Time %"), key_col="Pending (mtr)", height=420)

        c1, c2 = st.columns([1.35, 1], gap="medium")
        with c1:
            badge("⏰ Overdue POs (abhi tak receive nahi hue)")
            if overdue_po.empty:
                notice("Koi overdue PO nahi hai.", "success")
            else:
                op = overdue_po.sort_values("delay_days", ascending=False).head(15)
                op_tbl = pd.DataFrame({"PO No.": op["po"].values, "Vendor": op["vendor"].values,
                                       "Order Date": _fpo_date(op["order_date"]).values,
                                       "Due Date": _fpo_date(op["due"]).values,
                                       "Days Overdue": op["delay_days"].astype(float).values,
                                       "Pending (mtr)": op["pending"].values, "Status": op["status"].values})
                table_with_download(op_tbl, "overview_po_overdue", dec_cols=("Pending (mtr)",),
                                    key_col="Days Overdue", height=440)
                if len(overdue_po) > 15:
                    st.caption(f"Sabse purane 15 dikhaye gaye hain (kul {len(overdue_po):,} overdue POs) - "
                               "poori list Fabric PO tab me.")
        with c2:
            tm = xp["timing"].value_counts()
            colors = fabpo_colors()
            fig = go.Figure(go.Pie(labels=list(tm.index), values=list(tm.values), hole=0.55, sort=False,
                                   marker=dict(colors=[colors.get(k, COLORS["slate"]) for k in tm.index]),
                                   textinfo="percent", textposition="inside", textfont=dict(color="#ffffff")))
            chart(fig, "PO Delivery Status (POs)", 440, ns="ovw", legend="bottom")

    # ---------------- Samples ----------------
    if xs is not None and not xs.empty:
        st.markdown("---")
        badge("🔬 Samples")
        metrics([("Total Samples", _pcs(s_tot)), ("Approved", _pcs(s_app)), ("Approval %", f"{s_ratio:.1f}%"),
                 ("Rejected", _pcs(s_rej)), ("On Hold", _pcs(s_hld)), ("In Process", _pcs(s_prc))])
        c1, c2 = st.columns([1.15, 1], gap="medium")
        with c1:
            badge("🏭 Vendor-wise Samples")
            vnames = [n for n in xs["vendor"].unique() if not n.startswith("(")]
            vt = sample_entity_table(xs, "vendor", vnames, "Vendor")
            vt = vt[["Vendor", "Total", "Approved", "Rejected", "On Hold", "In Process", "Approval %"]]
            table_with_download(with_total(vt, "Vendor", pct=("Approval %", "Approved", "Total")),
                                "overview_sample_vendor", pct_cols=("Approval %",), key_col="Total", height=440)
        with c2:
            mt = sample_monthly(xs)
            mt = mt[mt["Total"] > 0]
            if mt.empty:
                empty_card("Monthly Sample Status (Pcs)", "Data nahi hai.", ns="ovw")
            else:
                long = mt.melt(id_vars="Month", value_vars=SAMPLE_STATUSES, var_name="Status", value_name="Pcs")
                fig = px.bar(long, x="Month", y="Pcs", color="Status", barmode="stack",
                             color_discrete_map=sample_colors(),
                             category_orders={"Month": list(mt["Month"]), "Status": SAMPLE_STATUSES})
                fig.update_xaxes(title="")
                chart(fig, "Monthly Sample Status (Pcs)", 440, ns="ovw")


# -----------------------------------------------------------------------------
# 12. SPEED HELPERS  (only HOW data is fetched / which tab is drawn - no numbers change)
# -----------------------------------------------------------------------------
def _in_parallel(jobs):
    """Runs the cached Google loaders at the same time. They mostly wait on the network, so 5 requests take
    about as long as 1. Results land in the normal cache; errors are ignored here because the regular code
    path right after this shows them in the right tab."""
    jobs = [j for j in jobs if j]
    if not jobs:
        return []
    if len(jobs) == 1:
        fn, args = jobs[0]
        try:
            return [fn(*args)]
        except Exception:
            return [None]

    def run(job):
        fn, args = job
        try:
            return fn(*args)
        except Exception:
            return None

    try:
        import logging
        from concurrent.futures import ThreadPoolExecutor
        # worker threads have no Streamlit page context -> silence the harmless "missing ScriptRunContext" log
        logging.getLogger("streamlit.runtime.scriptrunner_utils.script_run_context").setLevel(logging.ERROR)
        with ThreadPoolExecutor(max_workers=min(8, len(jobs))) as ex:
            return list(ex.map(run, jobs))
    except Exception:
        return [run(j) for j in jobs]


def _source_jobs(kind, tabs_info):
    """Second step of the prefetch: which data requests a source needs (same tab choice as its dashboard)."""
    cfg = _cfg_of(kind)
    sid = cfg["id"]
    if not tabs_info:
        return []
    if kind == "tracker":
        src = next((t for t in (find_tab(tabs_info, c) for c in cfg.get("source_tabs", [])) if t), None)
        src = src or next((t for t in tabs_info if "DASHBOARD" not in t.upper()), tabs_info[0])
        return [(load_tab_raw, (sid, src))]
    regex = {"overview_fabricators": r"monthwise|monthly\s*summary", "fabric_po": r"^\s*index\s*$",
             "flo_future": r"^\s*samples?\s*$"}.get(kind)
    if kind == "target_revenue":
        titles = [t for t, _ in tabs_info]
        out = []
        for names in (cfg.get("source_tabs", ["NEW"]), cfg.get("repeat_tabs", ["REPEAT"])):
            t = next((x for x in (find_tab(titles, n) for n in names) if x), None)
            if t:
                out.append((fetch_grid, (sid, t)))
        return out
    tab = pick_default_tab(tabs_info, cfg.get("source_tabs", []), cfg.get("gid"), regex)
    if kind == "flo_future":
        titles = [t for t, _ in tabs_info]
        vt = next((t for t in (find_tab(titles, n) for n in cfg.get("visits_tabs", [])) if t), None)
        return [(fetch_grid, (sid, tab))] + ([(fetch_grid, (sid, vt))] if vt else [])
    if kind == "overview_fabricators":
        return [(fetch_grid, (sid, tab, True)), (fetch_grid, (sid, tab))]
    return [(fetch_grid, (sid, tab))]


def _prefetch_sources(kinds):
    """Warms the cache for several sheets at once: tab lists in parallel, then the data tabs in parallel."""
    try:
        cfgs = [(k, _cfg_of(k)) for k in kinds]
        first = _in_parallel([(list_tabs if k == "tracker" else list_tabs_meta, (c["id"],)) for k, c in cfgs])
        jobs = []
        for (k, _c), info in zip(cfgs, first):
            jobs += _source_jobs(k, info)
        results = _in_parallel(jobs)
        # Target tab: its IMAGE-column formulas are a 3rd request per tab -> fetch those together as well
        tgt_id = _cfg_of("target_revenue")["id"] if "target_revenue" in kinds else None
        img_jobs = []
        for (fn, args), res in zip(jobs, results):
            if tgt_id and fn is fetch_grid and args[0] == tgt_id and res and res.get("values"):
                try:
                    vals = res["values"]
                    hr = _tgt_header_row(vals)
                    cmap = _tgt_col_map(vals[hr if hr is not None else 1])
                    img_jobs.append((fetch_image_urls, (args[0], args[1], cmap["image"])))
                except Exception:
                    pass
        _in_parallel(img_jobs)
    except Exception:
        pass          # prefetch is only a speed-up; the normal path still loads everything


def _keep_widget_state():
    """Tabs are drawn only when open; Streamlit forgets the filters of widgets it did not draw. Re-saving the
    keys at the start of every run keeps every tab's filters when you switch tabs."""
    skip_exact = {"global_refresh", "live_choice", "main_tab"}
    for k in list(st.session_state.keys()):
        if (not isinstance(k, str) or k in skip_exact or k.startswith(("dl_", "FormSubmitter", "_"))
                or k.endswith("_reset")):
            continue
        try:
            st.session_state[k] = st.session_state[k]
        except Exception:
            pass


# -----------------------------------------------------------------------------
# 13. FLO SPLASH SCREEN  (logo while the first data loads)
# -----------------------------------------------------------------------------
# Put the logo next to app.py:  assets/flo_logo.png  (or .svg / .jpg / .webp). No file -> "FLO" text is shown.
SPLASH_LOGO_FILES = ["assets/flo_logo.png", "assets/flo_logo.svg", "assets/flo_logo.jpg", "assets/flo_logo.jpeg",
                     "assets/flo_logo.webp", "flo_logo.png", "flo_logo.svg", "flo_logo.jpg", "flo_logo.webp"]
SPLASH_MIN_SECONDS = 1.2          # logo stays at least this long (so it does not just flash)
SPLASH_MESSAGE = "Dashboard taiyaar ho raha hai… Google Sheets se live data aa raha hai"
_MIME = {".png": "image/png", ".svg": "image/svg+xml", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


@st.cache_resource(show_spinner=False)
def _splash_logo_src():
    """Logo file as an inline data URI (read once per server start). None if no logo file is present."""
    base = Path(__file__).resolve().parent
    for rel in SPLASH_LOGO_FILES:
        f = base / rel
        if f.is_file():
            try:
                data = base64.b64encode(f.read_bytes()).decode()
                return f"data:{_MIME.get(f.suffix.lower(), 'image/png')};base64,{data}"
            except Exception:
                continue
    return None


def _show_splash():
    """Full-screen FLO logo on the first load of a session (and after 'Refresh data now').
    Returns (placeholder, start_time) or (None, None) when no splash is needed."""
    if st.session_state.get("_splash_done"):
        return None, None
    src = _splash_logo_src()
    logo = f'<img src="{src}" alt="FLO">' if src else '<div class="word">FLO</div>'
    ph = st.empty()
    ph.markdown(f'<div class="flo-splash">{logo}<div class="bar"></div><div class="msg">{_esc(SPLASH_MESSAGE)}</div></div>',
                unsafe_allow_html=True)
    return ph, time.time()


def _hide_splash(ph, t0):
    if ph is None:
        return
    wait = SPLASH_MIN_SECONDS - (time.time() - t0)
    if wait > 0:
        time.sleep(wait)
    ph.empty()
    st.session_state["_splash_done"] = True


# -----------------------------------------------------------------------------
# 14. MAIN ROUTER
# -----------------------------------------------------------------------------
LIVE_OPTIONS = {"Off": 0, "Every 1 min": 60, "Every 2 min": 120, "Every 5 min": 300}


def _safe(render_fn, *args):
    """Runs one dashboard; a sheet that cannot be opened shows an error only inside its own tab."""
    try:
        render_fn(*args)
    except SheetAccessError as e:
        show_access_error(e)


def main():
    _keep_widget_state()
    apply_theme()
    splash, t0 = _show_splash()
    try:
        if splash is not None:
            # behind the logo: all 5 sheets at the same time, so every tab opens without waiting afterwards
            _prefetch_sources(["tracker", "sample_summary", "overview_fabricators", "fabric_po", "target_revenue",
                               "flo_future"])
        _main_body()
    finally:
        _hide_splash(splash, t0)       # always removed, even if something fails


def _main_body():
    with st.sidebar:
        st.markdown("### ⏱️ Live Updates")
        choice = st.selectbox("Auto-refresh (Sample, Fabricator, Fabric PO, Target & FLO Future)", list(LIVE_OPTIONS.keys()), index=2, key="live_choice",
                              help="Google Sheet me change hone par dashboard itne time me khud update ho jayega. "
                                   "Master Sheet ke liye Refresh button use karein ya page reload karein.")
        st.session_state["live_secs"] = LIVE_OPTIONS[choice]
        if st.button("🔄 Refresh data now", key="global_refresh", **STRETCH):
            st.cache_data.clear()
            st.session_state.pop("_splash_done", None)     # show the FLO logo while fresh data loads
            st.rerun()

    fy = f"{FY_START_YEAR}-{(FY_START_YEAR + 1) % 100:02d}"
    _html(f'<div class="dashboard-hero"><div class="hero-t">📊 Executive Operations &amp; Production Hub {fy}</div>'
          f'<p>Overview · Designer · Production · Client · Sample · Fabricator · Fabric PO · Target &amp; Revenue · FLO Future — live from Google Sheets</p></div>')

    master_name = next(n for n, c in SHEET_REGISTRY.items() if c.get("type") == "tracker")
    sample_name = next(n for n, c in SHEET_REGISTRY.items() if c.get("type") == "sample_summary")
    fab_name = next(n for n, c in SHEET_REGISTRY.items() if c.get("type") == "overview_fabricators")
    po_name = next(n for n, c in SHEET_REGISTRY.items() if c.get("type") == "fabric_po")
    tgt_name = next(n for n, c in SHEET_REGISTRY.items() if c.get("type") == "target_revenue")
    ff_name = next(n for n, c in SHEET_REGISTRY.items() if c.get("type") == "flo_future")
    master_cfg = SHEET_REGISTRY[master_name]

    labels = ["📌 Overview", "👨‍🎨 Designer Dashboard", "🏭 Production Process Dashboard", "🏢 Client Dashboard",
              "🔬 Sample Dashboard", "🧵 Fabricator Dashboard", "🧾 Fabric PO Dashboard",
              "🎯 Target & Revenue Dashboard", "🔮 FLO Future"]
    # Only the open tab is built (Streamlit 1.5x+). Older Streamlit: all tabs are built, like before.
    try:
        tabs = st.tabs(labels, key="main_tab", on_change="rerun")
        lazy = True
    except TypeError:
        tabs = st.tabs(labels)
        lazy = False
    t_ovw, t_des, t_prod, t_cli, t_smp, t_fab, t_po, t_tgt, t_ff = tabs

    def is_open(t):
        if not lazy:
            return True
        o = getattr(t, "open", None)
        return True if o is None else bool(o)

    # Master Sheet: loaded once, shared by its 3 dashboards (only when one of them is open)
    master_tabs = [(t, fn) for t, fn in ((t_des, designer_dashboard), (t_prod, production_dashboard),
                                         (t_cli, client_dashboard)) if is_open(t)]
    if master_tabs:
        first_tab = master_tabs[0][0]
        master_err, d_all = None, None
        try:
            with first_tab:
                d_all = load_master(master_cfg)
        except SheetAccessError as e:
            master_err = e
        key = master_cfg["id"][:8]
        for tab, fn in master_tabs:
            with tab:
                if master_err is not None:
                    show_access_error(master_err)
                elif d_all is not None:
                    fn(d_all, key)
                elif tab is not first_tab:
                    notice("Master Sheet ka data load nahi ho paya - Designer Dashboard tab me details dekhein.", "error")

    if is_open(t_ovw):
        with t_ovw:
            _safe(render_overview_dashboard)
    if is_open(t_smp):
        with t_smp:
            _safe(render_sample_summary_dashboard, sample_name, SHEET_REGISTRY[sample_name])
    if is_open(t_fab):
        with t_fab:
            _safe(render_fabricator_dashboard, fab_name, SHEET_REGISTRY[fab_name])
    if is_open(t_po):
        with t_po:
            _safe(render_fabric_po_dashboard, po_name, SHEET_REGISTRY[po_name])
    if is_open(t_tgt):
        _prefetch_sources(["target_revenue"])
        with t_tgt:
            _safe(render_target_dashboard, tgt_name, SHEET_REGISTRY[tgt_name])
    if is_open(t_ff):
        with t_ff:
            _safe(render_flo_future_dashboard, ff_name, SHEET_REGISTRY[ff_name])


if __name__ == "__main__":
    main()
