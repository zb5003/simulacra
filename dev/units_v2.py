import numpy as np

import simulacra as si
from simulacra import units

m = units.Unit(5, 'm', r'\mathrm{m}')

print(m)
print(repr(m))
print(type(m))

a = 5 * m
print(a)
print(repr(a))
print(type(a))


arr = np.linspace(0, 1e6, 1e6)

r = arr * m
print(r)
print(repr(r))
print(r.dtype)
print(type(r[0]))


print(m.latex)
print(5 * m)

unit_array = np.array([m], dtype = units.Unit)
print(unit_array)
print(unit_array.dtype)
print(unit_array[0].latex)

b = 5 * unit_array
print(type(b[0]))

#
#
#  with si.utils.BlockTimer() as timer1:
#     for ii in range(1000):
#         m * arr
# print(timer1)
#
# with si.utils.BlockTimer() as timer1:
#     for ii in range(1000):
#         5 * arr
# print(timer1)
