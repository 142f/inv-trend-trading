"""Leaf primitives shared by ``data`` and ``storage``.

Nothing in ``infra`` may import either package: it is the layer both already
agree on, and moving the shared primitives here is what lets ``storage`` stop
importing ``data`` (the last package cycle in the architecture contract).
"""
