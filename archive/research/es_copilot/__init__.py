"""ES day-trading co-pilot.

Reads IBKR delayed MES bars, runs the Tori trendline-break strategy as a
sensor (no orders), and exposes a Claude-powered chat CLI you talk to while
trading manually in NinjaTrader.

Data is delayed ~15-20 min; that's fine because the co-pilot only acts on
confirmed 15m bar closes. Execution happens on your NT live chart, not here.
"""
