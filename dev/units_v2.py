import numpy as np

import simulacra as si
from simulacra import units

m = units.Unit(5, 'm', r'\mathrm{m}')

print(m)
print(repr(m))

arr = np.linspace(0, 1e6, 1e6)

with si.utils.BlockTimer() as timer1:
    for ii in range(1000):
        m * arr
print(timer1)

with si.utils.BlockTimer() as timer1:
    for ii in range(1000):
        5 * arr
print(timer1)
