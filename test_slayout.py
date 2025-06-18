from glayout.primitives.fet import nmos
from glayout.pdk import sky130_mapped as sky130

print("\n...Creating nmos component...")
nmos_component = nmos(sky130)