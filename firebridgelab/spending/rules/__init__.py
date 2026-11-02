"""Built-in spending rules, one per module.

Each module registers its strategy with ``firebridgelab.spending.registry`` on
import; the registry imports every module in this package the first time it
builds a strategy. Adding a rule means adding a module here.
"""
