"""`python CHECKOUT ARGS...`: audiolink from a source checkout, with no PYTHONPATH to set
(a Windows ssh ProxyCommand or scheduled task has no shell to set it with)."""
import runpy

runpy.run_module("audiolink", run_name="__main__", alter_sys=True)
