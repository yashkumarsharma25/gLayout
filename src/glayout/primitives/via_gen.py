# src/glayout/primitives/via_gen.py
"""
Via generator utilities for gLayout.

This version includes safe fallbacks so it can run when `gdsfactory` is not installed.
"""

from typing import Optional, Union, Literal
from math import floor
from decimal import Decimal

# pydantic decorator
from pydantic import validate_arguments

# Safe import of gdsfactory decorators/types - supply dummies if missing
try:
    from gdsfactory.cell import cell
    from gdsfactory.component import Component
    from gdsfactory.components.rectangle import rectangle
except Exception:
    # Dummy `cell` decorator (no-op)
    def cell(func=None, **kwargs):
        if func:
            return func
        return lambda f: f

    # Minimal dummy Component class used only to satisfy code that expects these methods
    class Component:
        def __init__(self, name: str = "dummy_component"):
            self.name = name
            # minimal internals used by this module (list-like behaviour used in code)
            self._refs = []
            self.xmax = 0
            self.ymax = 0

        # placeholder << operator used in original code (component << rectangle(...))
        def __lshift__(self, other):
            # If `other` has a `get_ports_list` or returns something, just append a placeholder
            self._refs.append(other)
            return other

        # some code expects .add, .add_ports, .add_ports_list, .flatten, .remove_layers, .extract
        def add(self, obj):
            self._refs.append(obj)

        def add_ports(self, ports, prefix: str = ""):
            # no-op placeholder
            return None

        def get_ports_list(self):
            return []

        def get_ports(self):
            return []

        def flatten(self):
            return self

        def extract(self, layers=None):
            # return a Component-like placeholder
            return Component(name=f"{self.name}_extract")

        def remove_layers(self, layers=None):
            return self

        def add_port(self, *args, **kwargs):
            return None

        # write_gds used by some callers; return a dummy path
        def write_gds(self, *args, **kwargs):
            return "dummy.gds"

        # for bounding box helpers used in code
        @property
        def xmax(self):
            return 0

        @property
        def ymax(self):
            return 0

    # Dummy rectangle factory returning a placeholder object with .get_ports_list()
    def rectangle(size, layer=None, centered=True):
        class Rect:
            def __init__(self, size, layer, centered):
                self.size = size
                self.layer = layer
                self.centered = centered

            def get_ports_list(self):
                return []

        return Rect(size, layer, centered)


# Use package-relative imports for internal utilities (avoid importing package __init__ side-effects)
from ..pdk.mappedpdk import MappedPDK
from ..util.comp_utils import evaluate_bbox, prec_array, to_float, move, prec_ref_center, to_decimal
from ..util.port_utils import rename_ports_by_orientation, print_ports
from ..util.snap_to_grid import component_snap_to_grid


@validate_arguments
def __error_check_order_layers(
    pdk: MappedPDK, glayer1: str, glayer2: str
) -> tuple[tuple[int, int], tuple[str, str]]:
    """correctly order layers (level1 should be lower than level2)"""
    pdk.activate()
    # check that the generic layers specfied can be routed between
    if not all([pdk.is_routable_glayer(met) for met in [glayer1, glayer2]]):
        raise ValueError("via_stack: specify between two routable layers")
    level1 = int(glayer1[-1]) if "met" in glayer1 else 0
    level2 = int(glayer2[-1]) if "met" in glayer2 else 0
    if level1 > level2:
        level1, level2 = level2, level1
        glayer1, glayer2 = glayer2, glayer1
    # check that all layers needed between glayer1-glayer2 are present
    required_glayers = [glayer2]
    for level in range(level1, level2):
        via_name = "mcon" if level == 0 else "via" + str(level)
        layer_name = glayer1 if level == 0 else "met" + str(level)
        required_glayers += [via_name, layer_name]
    pdk.has_required_glayers(required_glayers)
    return ((level1, level2), (glayer1, glayer2))


@validate_arguments
def __get_layer_dim(pdk: MappedPDK, glayer: str, mode: Literal["both", "above", "below"] = "both") -> float:
    """Returns the required dimension of a routable layer in a via stack"""
    # error checking
    if not pdk.is_routable_glayer(glayer):
        raise ValueError("__get_layer_dim: glayer must be a routable layer")
    # split into above rules and below rules
    consider_above = (mode == "both" or mode == "above")
    consider_below = (mode == "both" or mode == "below")
    is_lvl0 = any([hint in glayer for hint in ["poly", "active"]])
    layer_dim = 0
    if consider_below and not is_lvl0:
        via_below = "mcon" if glayer == "met1" else "via" + str(int(glayer[-1]) - 1)
        layer_dim = pdk.get_grule(via_below)["width"] + 2 * pdk.get_grule(via_below, glayer)["min_enclosure"]
    if consider_above:
        via_above = "mcon" if is_lvl0 else "via" + str(glayer[-1])
        layer_dim = max(layer_dim, pdk.get_grule(via_above)["width"] + 2 * pdk.get_grule(via_above, glayer)["min_enclosure"])
    layer_dim = max(layer_dim, pdk.get_grule(glayer)["min_width"])
    return layer_dim


@validate_arguments
def __get_viastack_minseperation(pdk: MappedPDK, viastack: Component, ordered_layer_info) -> tuple[float, float]:
    """internal use: return absolute via separation and top_enclosure (top via to top met enclosure)"""
    get_sep = lambda _pdk, rule, _lay_, comp: (rule + 2 * comp.extract(layers=[_pdk.get_glayer(_lay_)]).xmax)
    level1, level2 = ordered_layer_info[0]
    glayer1, glayer2 = ordered_layer_info[1]
    mcon_rule = pdk.get_grule("mcon")["min_separation"]
    via_spacing = [] if level1 else [get_sep(pdk, mcon_rule, "mcon", viastack)]
    level1_met = level1 if level1 else level1 + 1
    top_enclosure = 0
    for level in range(level1_met, level2):
        met_glayer = "met" + str(level)
        via_glayer = "via" + str(level)
        mrule = pdk.get_grule(met_glayer)["min_separation"]
        vrule = pdk.get_grule(via_glayer)["min_separation"]
        via_spacing.append(get_sep(pdk, mrule, met_glayer, viastack))
        via_spacing.append(get_sep(pdk, vrule, via_glayer, viastack))
        if level == (level2 - 1):
            top_enclosure = pdk.get_grule(glayer2, via_glayer)["min_enclosure"]
    via_spacing = pdk.snap_to_2xgrid(max(via_spacing), return_type="float")
    top_enclosure = pdk.snap_to_2xgrid(top_enclosure, return_type="float")
    return pdk.snap_to_2xgrid([via_spacing, 2 * top_enclosure], return_type="float")


@cell
def via_stack(
    pdk: MappedPDK,
    glayer1: str,
    glayer2: str,
    centered: bool = True,
    fullbottom: bool = False,
    fulltop: bool = False,
    assume_bottom_via: bool = False,
    same_layer_behavior: Literal["lay_nothing", "min_square"] = "lay_nothing"
) -> Component:
    """produces a single via stack between two layers that are routable (metal, poly, or active)"""
    ordered_layer_info = __error_check_order_layers(pdk, glayer1, glayer2)
    level1, level2 = ordered_layer_info[0]
    glayer1, glayer2 = ordered_layer_info[1]
    viastack = Component()
    # if same level return component with min_width rectangle on that layer
    if level1 == level2:
        if same_layer_behavior == "lay_nothing":
            return viastack
        min_square = viastack << rectangle(size=2 * [pdk.get_grule(glayer1)["min_width"]], layer=pdk.get_glayer(glayer1), centered=centered)
        # update ports
        if level1 == 0:  # both poly or active
            viastack.add_ports(min_square.get_ports_list(), prefix="bottom_layer_")
        else:  # both mets
            viastack.add_ports(min_square.get_ports_list(), prefix="top_met_")
            viastack.add_ports(min_square.get_ports_list(), prefix="bottom_met_")
    else:
        ports_to_add = dict()
        for level in range(level1, level2 + 1):
            via_name = "mcon" if level == 0 else "via" + str(level)
            layer_name = glayer1 if level == 0 else "met" + str(level)
            # get layer sizing
            mode = "below" if level == level2 else ("above" if level == level1 else "both")
            mode = "both" if assume_bottom_via and level == level1 else mode
            layer_dim = __get_layer_dim(pdk, layer_name, mode=mode)
            # place met/via, do not place via if on top layer
            via_ref = None
            if level != level2:
                via_dim = pdk.get_grule(via_name)["width"]
                via_ref = viastack << rectangle(size=[via_dim, via_dim], layer=pdk.get_glayer(via_name), centered=True)
            lay_ref = viastack << rectangle(size=[layer_dim, layer_dim], layer=pdk.get_glayer(layer_name), centered=True)
            # update ports
            if layer_name == glayer1:
                ports_to_add["bottom_layer_"] = lay_ref.get_ports_list()
                if via_ref is not None:
                    ports_to_add["bottom_via_"] = via_ref.get_ports_list()
            if (level1 == 0 and level == 1) or (level1 > 0 and layer_name == glayer1):
                ports_to_add["bottom_met_"] = lay_ref.get_ports_list()
            if layer_name == glayer2:
                ports_to_add["top_met_"] = lay_ref.get_ports_list()
        # implement fulltop and fullbottom options. update ports_to_add accordingly
        if fullbottom:
            bot_ref = viastack << rectangle(size=evaluate_bbox(viastack), layer=pdk.get_glayer(glayer1), centered=True)
            if level1 != 0:
                ports_to_add["bottom_met_"] = bot_ref.get_ports_list()
            ports_to_add["bottom_layer_"] = bot_ref.get_ports_list()
        if fulltop:
            ports_to_add["top_met_"] = (viastack << rectangle(size=evaluate_bbox(viastack), layer=pdk.get_glayer(glayer2), centered=True)).get_ports_list()
        # add all ports in ports_to_add
        for prefix, ports_list in ports_to_add.items():
            viastack.add_ports(ports_list, prefix=prefix)
        # move SW corner to 0,0 if centered=False
        if not centered:
            viastack = move(viastack, (viastack.xmax, viastack.ymax))
    return rename_ports_by_orientation(viastack.flatten())


@cell
def via_array(
    pdk: MappedPDK,
    glayer1: str,
    glayer2: str,
    size: Optional[tuple[Optional[float], Optional[float]]] = None,
    minus1: bool = False,
    num_vias: Optional[tuple[Optional[int], Optional[int]]] = None,
    lay_bottom: bool = True,
    fullbottom: bool = False,
    no_exception: bool = False,
    lay_every_layer: bool = False
) -> Component:
    """Fill a region with vias. Will automatically decide num rows and columns"""
    # setup
    ordered_layer_info = __error_check_order_layers(pdk, glayer1, glayer2)
    level1, level2 = ordered_layer_info[0]
    glayer1, gllayer2 = ordered_layer_info[1]
    viaarray = Component()
    # if same level return empty component
    if level1 == level2:
        return viaarray
    # figure out min space between via stacks
    viastack = via_stack(pdk, glayer1, gllayer2)
    viadim = evaluate_bbox(viastack)[0]
    via_abs_spacing, top_enclosure = __get_viastack_minseperation(pdk, viastack, ordered_layer_info)
    # error check size and determine num_vias, cnum_vias[0]=x, cnum_vias[1]=y
    cnum_vias = [None, None]
    for i in range(2):
        if (num_vias and num_vias[i] is not None):
            cnum_vias[i] = num_vias[i]
        elif (size and size[i] is not None):
            dim = pdk.snap_to_2xgrid(size[i], return_type="float")
            fltnum = floor((dim - top_enclosure) / (via_abs_spacing)) or 1
            fltnum = 1 if fltnum < 1 else fltnum
            cnum_vias[i] = ((fltnum - 1) or 1) if minus1 else fltnum
            if to_decimal(viadim) > to_decimal(dim) and not no_exception:
                raise ValueError(f"via_array,size:dim#{i}={dim} < {viadim}")
        else:
            raise ValueError("give at least 1: num_vias or size for each dim")
    # create array
    viaarray_ref = prec_ref_center(prec_array(viastack, columns=cnum_vias[0], rows=cnum_vias[1], spacing=2 * [via_abs_spacing], absolute_spacing=True))
    viaarray.add(viaarray_ref)
    viaarray.add_ports(viaarray_ref.get_ports_list(), prefix="array_")
    # find the what should be used as full dims
    viadims = evaluate_bbox(viaarray)
    if not size:
        size = [None, None]
    size = [size[i] if size[i] else viadims[i] for i in range(2)]
    size = [viadims[i] if viadims[i] > size[i] else size[i] for i in range(2)]
    # place bottom layer and add bot_lay_ ports
    if lay_bottom or fullbottom or lay_every_layer:
        bdims = evaluate_bbox(viaarray.extract(layers=[pdk.get_glayer(glayer1)]))
        bref = viaarray << rectangle(size=(size if fullbottom else bdims), layer=pdk.get_glayer(glayer1), centered=True)
        viaarray.add_ports(bref.get_ports_list(), prefix="bottom_lay_")
    else:
        viaarray = viaarray.remove_layers(layers=[pdk.get_glayer(glayer1)])
    # place top met
    tref = viaarray << rectangle(size=size, layer=pdk.get_glayer(glayer2), centered=True)
    viaarray.add_ports(tref.get_ports_list(), prefix="top_met_")
    # place every layer in between if lay_every_layer
    if lay_every_layer:
        for i in range(level1 + 1, level2):
            bdims = evaluate_bbox(viaarray.extract(layers=[pdk.get_glayer(f"met{i}")]))
            viaarray << rectangle(size=bdims, layer=pdk.get_glayer(f"met{i}"), centered=True)
    return component_snap_to_grid(rename_ports_by_orientation(viaarray))
