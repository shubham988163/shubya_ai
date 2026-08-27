"""NSE F&O intraday BUY scanner — opening-window (09:15–10:00 IST) long setups.

Decision support only. Nothing here places orders; the scanner emits ranked
candidates with entry/stop/target derived from market structure, or refuses.

Entry point:  python -m trading.fno --help
"""
