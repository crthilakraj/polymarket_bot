"""Order management and risk gating: turns signals into (safely) placed orders.

order_manager.OrderManager is the single entry point every strategy's output
routes through - see its docstring for how Signal-producing strategies and
the stateful market-making strategy both end up at the same risk gate.
"""
