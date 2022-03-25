from __future__ import annotations

import cmath
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from itertools import product
from typing import Literal, Sequence

import gdstk
import numpy as np

from ldesign import config, elements
from ldesign.shapes import bridge
from ldesign.shapes.bondpad import BondPad
from ldesign.shapes.bridge import CpwBridgeArgs
from ldesign.shapes.path import CpwArgs
from ldesign.utils import to_complex

logger = logging.getLogger(__name__)
LEN_ERR = 1e-4
ANGLE_ERR = 1e-5


@dataclass
class PathOptions:
    radius: float = 30
    cpw: CpwArgs = field(default_factory=CpwArgs)
    total_length: float | None = None


class PathOp:
    pass


@dataclass
class Segment(PathOp):
    point: complex | elements.DockingPort
    radius: float

    def __post_init__(self):
        if not isinstance(self.point, elements.DockingPort):
            self.point = to_complex(self.point)
        if self.radius < 0:
            raise ValueError(f"Negative turning radius.")


@dataclass
class Bridge(PathOp):
    bridge: CpwBridgeArgs = field(default_factory=CpwBridgeArgs)


@dataclass
class AutoMeander(PathOp):
    width: float
    depth: float
    wind_direction: Literal["left", "right"]
    in_position: float
    out_position: float
    radius: float
    length: float | None = None


def _create_gdstk_path(pos: complex, cfg: config.Config, cpw: CpwArgs):
    return gdstk.RobustPath(
        to_complex(pos),
        [cpw.width, cpw.gap, cpw.gap],
        [
            0,
            (cpw.width + cpw.gap) / 2,
            -(cpw.width + cpw.gap) / 2,
        ],
        layer=[
            cfg.LD_AL_INNER["layer"],
            cfg.LD_AL_OUTER["layer"],
            cfg.LD_AL_OUTER["layer"],
        ],
        datatype=[
            cfg.LD_AL_INNER["datatype"],
            cfg.LD_AL_OUTER["datatype"],
            cfg.LD_AL_OUTER["datatype"],
        ],
    )


def is_zero_len(v: complex):
    return abs(v) < LEN_ERR


def is_zero_angle(angle: float):
    return abs(angle) < ANGLE_ERR


def _solve_to_single_circle(
    current_pos: complex, next_pos: complex, next_angle: float, next_radius: float
) -> tuple[complex, float]:
    # Determine turn direction
    v_b = cmath.rect(1, next_angle)
    v_ab = next_pos - current_pos
    if is_zero_len(v_ab):
        raise Exception
    angle_diff = cmath.phase(v_b / v_ab)
    if is_zero_angle(angle_diff):
        return next_pos, 0
    # Pointing outwards
    v_b_r_out = -1j * v_b * math.copysign(next_radius, angle_diff)
    p_cb = next_pos - v_b_r_out
    v_a_cb = p_cb - current_pos
    l_a_cb, phi_a_cb = cmath.polar(v_a_cb)
    # Calculate turn angle
    if next_radius > l_a_cb:
        raise Exception
    theta_in = math.asin(next_radius / l_a_cb)
    in_angle = math.copysign(theta_in, -angle_diff) + phi_a_cb
    turn_angle = _get_turn_angle(in_angle, next_angle, angle_diff)
    # Calculate segment point
    v_b_r_in = v_b_r_out * cmath.rect(1, -turn_angle)
    p_b_r_in = p_cb + v_b_r_in
    return p_b_r_in, turn_angle


def _solve_to_two_circle(
    current_pos: complex,
    current_angle: float,
    current_radius: float,
    next_pos: complex,
    next_angle: float,
    next_radius: float,
) -> list[tuple[float, float, complex, complex]]:
    def _solve_angle(d: float, r1: float, r2: float) -> list[tuple[float, float]]:
        if abs(r1 - r2) > d:
            return []
        # outer
        theta = math.acos((r1 - r2) / d)
        angles = [(theta, theta), (-theta, -theta)]
        # inner
        if d > r1 + r2:
            theta = math.acos((r1 + r2) / d)
            angles.extend([(theta, theta - math.pi), (-theta, math.pi - theta)])
        return angles

    results: list[tuple[float, float, complex, complex]] = []
    for turn_sign_a, turn_sign_b in product((-1, 1), repeat=2):
        # Pointing outwards
        v_a_r_in = -1j * turn_sign_a * cmath.rect(current_radius, current_angle)
        v_b_r_out = -1j * turn_sign_b * cmath.rect(next_radius, next_angle)
        p_ca = current_pos - v_a_r_in
        p_cb = next_pos - v_b_r_out
        v_ca_cb = p_cb - p_ca
        if is_zero_len(v_ca_cb):
            if is_zero_len(current_radius - next_radius) and turn_sign_a == turn_sign_b:
                turn_angle = _get_turn_angle(current_angle, next_angle, turn_sign_a)
                results.append((turn_angle, 0, next_pos, next_pos))
            continue
        l_ca_cb, phi_ca_cb = cmath.polar(v_ca_cb)
        angles = _solve_angle(l_ca_cb, current_radius, next_radius)
        for a1, a2 in angles:
            v_r_seg_a = cmath.rect(current_radius, a1 + phi_ca_cb)
            seg_a = p_ca + v_r_seg_a
            v_r_seg_b = cmath.rect(next_radius, a2 + phi_ca_cb)
            seg_b = p_cb + v_r_seg_b
            # check turn direction
            v_sa_sb = seg_b - seg_a
            if (
                cmath.phase(v_sa_sb / v_r_seg_a) * turn_sign_a < 0
                or cmath.phase(v_sa_sb / v_r_seg_b) * turn_sign_b < 0
            ):
                continue
            seg_angle = cmath.phase(v_sa_sb)
            turn_angle_a = _get_turn_angle(current_angle, seg_angle, turn_sign_a)
            turn_angle_b = _get_turn_angle(seg_angle, next_angle, turn_sign_b)
            results.append((turn_angle_a, turn_angle_b, seg_a, seg_b))
    return sorted(
        results,
        key=lambda x: abs(x[0]) * current_radius
        + abs(x[1]) * next_radius
        + abs(x[3] - x[2]),
    )


def _get_turn_angle(start: float, end: float, sign: float):
    ans = (end - start) % (2 * math.pi)
    if sign > 0:
        return ans
    if ans > 0:
        ans -= 2 * math.pi
    return ans


def _get_arc_angle(current_angle: float, turn_angle: float):
    start_angle = math.copysign(math.pi / 2, -turn_angle) + current_angle
    final_angle = start_angle + turn_angle
    return start_angle, final_angle


class _BaseOpVisitor:
    start_pos: complex
    start_angle: float | None
    current_pos: complex
    current_angle: float | None
    current_radius: float
    started: bool

    def __init__(self) -> None:
        self.started = False
        self.start_pos = 0j
        self.start_angle = None
        self.current_pos = 0j
        self.current_angle = None
        self.current_radius = 0

    def process_ops(self, ops: Sequence[PathOp]):
        pending_ops = deque(ops)
        if not self.started:
            first_op = pending_ops.popleft()
            start_angle = None
            match first_op:
                case Segment(
                    point=elements.DockingPort(point=start_pos, angle=start_angle),
                    radius=start_radius,
                ):
                    start_angle -= math.pi
                case Segment(point=complex() as start_pos, radius=start_radius):
                    pass
                case _:
                    raise TypeError(first_op)
            self.start(start_pos, start_angle, start_radius)

        while len(pending_ops) > 0:
            next_op = pending_ops.popleft()
            self._process_single_op(
                next_op,
                pending_ops,
            )

    def start(self, pos: complex, angle: float | None, radius: float):
        self.started = True
        self.start_pos = pos
        self.start_angle = angle
        self.current_pos = pos
        self.current_angle = angle
        self.current_radius = radius

    def segment(self, point: complex) -> None:
        self._ensure_started()
        self._segment_inner(point)

    def turn(self, radius: float, angle: float) -> None:
        self._ensure_started()
        self._turn_inner(radius, angle)

    def _ensure_started(self):
        if not self.started:
            raise RuntimeError("Not started")

    def _segment_inner(self, point: complex) -> None:
        v = point - self.current_pos
        if not is_zero_len(v):
            new_angle = cmath.phase(v)
            if self.start_angle is None:
                self.start_angle = new_angle
            self.current_pos = point
            self.current_angle = new_angle

    def _turn_inner(self, radius: float, angle: float) -> None:
        if not is_zero_angle(angle):
            if self.current_angle is None:
                self.current_angle = 0
            if self.start_angle is None:
                self.start_angle = self.current_angle
            start_angle, final_angle = _get_arc_angle(self.current_angle, angle)
            self.current_angle = (self.current_angle + angle) % (2 * math.pi)
            self.current_pos += cmath.rect(radius, final_angle) - cmath.rect(
                radius, start_angle
            )

    def _process_single_op(self, next_op: PathOp, pending_ops: deque[PathOp]):
        match next_op:
            case Segment():
                self._process_op_segment(next_op, pending_ops)
            case Bridge():
                self._process_op_bridge(next_op, pending_ops)
            case AutoMeander():
                self._process_op_automeander(next_op, pending_ops)

    def _process_op_segment(self, next_op: Segment, pending_ops: deque[PathOp]):
        match next_op:
            case Segment(
                point=elements.DockingPort() as next_port,
                radius=next_radius,
            ):
                self._process_segment_to_port(
                    next_port,
                    next_radius,
                )
            case Segment(point=complex() as next_pos, radius=next_radius):
                future_op = None
                if len(pending_ops) > 0:
                    future_op = pending_ops[0]
                future_point = None
                future_angle = None
                future_radius = None
                match future_op:
                    case Segment(point=complex() as future_point):
                        pass
                    case Segment(
                        point=elements.DockingPort(
                            point=future_point, angle=future_angle
                        ),
                        radius=future_radius,
                    ):
                        future_angle += math.pi
                self._process_segment_to_point(
                    next_pos, next_radius, future_point, future_angle, future_radius
                )

    def _process_segment_to_port(
        self, port: elements.DockingPort, radius: float
    ) -> None:
        self._to_port_helper(port.point, port.angle + math.pi, radius)
        self.current_pos = port.point
        self.current_angle = (port.angle + math.pi) % (2 * math.pi)
        self.current_radius = radius

    def _process_segment_to_point(
        self,
        point: complex,
        radius: float,
        future_point: complex | None,
        future_angle: float | None,
        future_radius: float | None,
    ) -> _BaseOpVisitor:
        self._to_point_helper(point, radius, future_point, future_angle, future_radius)
        self.current_radius = radius
        return self

    def _to_port_helper(
        self,
        next_pos: complex,
        next_angle: float,
        next_radius: float,
    ) -> None:
        current_pos = self.current_pos
        current_angle = self.current_angle
        current_radius = self.current_radius
        v_ab = next_pos - current_pos
        if is_zero_len(v_ab):
            return
        if current_angle is None or is_zero_len(current_radius):
            if is_zero_len(next_radius):
                self.segment(next_pos)
                return
            turn_pos, turn_angle = _solve_to_single_circle(
                current_pos, next_pos, next_angle, next_radius
            )
            self.segment(turn_pos)
            self.turn(next_radius, turn_angle)
            return
        if is_zero_len(next_radius):
            turn_pos, turn_angle = _solve_to_single_circle(
                next_pos, current_pos, current_angle + math.pi, current_radius
            )
            self.turn(current_radius, -turn_angle)
            self.segment(next_pos)
            return
        # (turn_angle_a, turn_angle_b, seg_a, seg_b)
        results = _solve_to_two_circle(
            current_pos,
            current_angle,
            current_radius,
            next_pos,
            next_angle,
            next_radius,
        )
        assert len(results) > 0
        turn_angle_a, turn_angle_b, _, seg_b = results[0]
        self.turn(current_radius, turn_angle_a)
        self.segment(seg_b)
        self.turn(next_radius, turn_angle_b)

    def _to_point_helper(
        self,
        next_pos: complex,
        next_radius: float,
        future_pos: complex | None,
        future_angle: float | None,
        future_radius: float | None,
    ) -> None:
        if self.current_angle is None or is_zero_len(self.current_radius):
            start_pos = self.current_pos
        else:
            start_pos, turn_angle = _solve_to_single_circle(
                next_pos,
                self.current_pos,
                self.current_angle + math.pi,
                self.current_radius,
            )
            self.turn(self.current_radius, -turn_angle)
        if is_zero_len(next_radius) or future_pos is None:
            self.segment(next_pos)
            return
        if future_angle is None or future_radius is None or is_zero_len(future_radius):
            end_pos = future_pos
        else:
            end_pos, _ = _solve_to_single_circle(
                next_pos, future_pos, future_angle, future_radius
            )
        vec1 = next_pos - start_pos
        vec2 = end_pos - next_pos
        # next_radius is nonzero
        if is_zero_len(vec1) or is_zero_len(vec2):
            raise Exception
        turn_angle = cmath.phase(vec2 / vec1)
        arc_len = next_radius * math.tan(abs(turn_angle / 2))
        if (abs(vec1) < arc_len - LEN_ERR) or (abs(vec2) < arc_len - LEN_ERR):
            raise Exception
        arc_start = next_pos - vec1 / abs(vec1) * arc_len
        self.segment(arc_start)
        self.turn(next_radius, turn_angle)

    def _process_op_bridge(self, next_op: Bridge, pending_ops: deque[PathOp]):
        if self.current_angle is None:
            self.current_angle = 0
        length = next_op.bridge.length
        self.current_pos += cmath.rect(length, self.current_angle)

    def _process_op_automeander(self, next_op: AutoMeander, pending_ops: deque[PathOp]):
        if self.current_angle is None:
            self.current_angle = 0
        self.current_pos += cmath.rect(1, self.current_angle - math.pi / 2) * (
            next_op.out_position - next_op.in_position + 1j * next_op.depth
        )


class LengthTracker(_BaseOpVisitor):
    total_length: float

    def __init__(self):
        super().__init__()
        self.total_length = 0

    def _segment_inner(self, point: complex) -> None:
        v = point - self.current_pos
        if not is_zero_len(v):
            self.total_length += abs(v)
        super()._segment_inner(point)

    def _turn_inner(self, radius: float, angle: float) -> None:
        if not is_zero_angle(angle):
            self.total_length += radius * abs(angle)
        super()._turn_inner(radius, angle)

    def _process_op_bridge(self, next_op: Bridge, pending_ops: deque[PathOp]):
        super()._process_op_bridge(next_op, pending_ops)
        self.total_length += next_op.bridge.length

    def _process_op_automeander(self, next_op: AutoMeander, pending_ops: deque[PathOp]):
        super()._process_op_automeander(next_op, pending_ops)


class BasicPathBuilder(_BaseOpVisitor):
    elems: list[elements.Element]
    paths: list[gdstk.RobustPath]
    cpw: elements.CpwWaveguide
    current_path: gdstk.RobustPath | None
    options: PathOptions
    cfg: config.Config

    def __init__(
        self,
        options: PathOptions,
        cfg: config.Config,
    ) -> None:
        super().__init__()
        self.cpw = elements.CpwWaveguide()
        self.elems = []
        self.paths = []
        self.current_path = None
        self.options = options
        self.cfg = cfg

    def _segment_inner(self, point: complex) -> None:
        self._ensure_path()
        assert self.current_path is not None
        v = point - self.current_pos
        if not is_zero_len(v):
            self.current_path.segment(point)
        super()._segment_inner(point)

    def _turn_inner(self, radius: float, angle: float) -> None:
        self._ensure_path()
        assert self.current_path is not None
        if not is_zero_angle(angle):
            current_angle = self.current_angle
            if current_angle is None:
                current_angle = 0
            start_angle, final_angle = _get_arc_angle(current_angle, angle)
            self.current_path.arc(radius, start_angle, final_angle)
        super()._turn_inner(radius, angle)

    def _process_op_bridge(self, next_op: Bridge, pending_ops: deque[PathOp]):
        current_angle = self.current_angle
        current_pos = self.current_pos
        if current_angle is None:
            current_angle = 0
        b = bridge.Bridge(next_op.bridge, self.cfg)
        b = self.cpw.add_element(
            b, elements.DockingPort(current_pos, current_angle, self.cpw), b.port_start
        )
        self.current_path = None
        super()._process_op_bridge(next_op, pending_ops)

    def _process_op_automeander(self, next_op: AutoMeander, pending_ops: deque[PathOp]):
        super()._process_op_automeander(next_op, pending_ops)
        self.current_path = None

    def build(self) -> elements.CpwWaveguide:
        self._ensure_started()
        if self.start_angle is None or self.current_angle is None:
            raise Exception
        self.cpw.create_port("start", self.start_pos, self.start_angle + math.pi)
        self.cpw.create_port("end", self.current_pos, self.current_angle)
        return self.cpw

    def _ensure_path(self):
        if self.current_path is None:
            path = _create_gdstk_path(self.current_pos, self.cfg, self.options.cpw)
            self.cpw.cell.add(path)
            self.current_path = path


# TODO support length mark in the middle of the path
# lines go into ports except the start port
class PathOpGenerator:
    _ops: list[PathOp]
    options: PathOptions

    def __init__(
        self,
        start: complex | elements.DockingPort,
        radius: float | None = None,
        options: PathOptions | None = None,
    ):
        if isinstance(start, elements.DockingPort):
            start = start.copy()
            # rotate start port by pi to unify with other ports
            start.angle += math.pi
        if radius is None:
            radius = self.options.radius
        self._ops = [Segment(start, radius)]
        self.options = PathOptions() if options is None else options

    def segment(
        self,
        point: complex | elements.DockingPort,
        radius: float | None = None,
        angle: float | None = None,
    ) -> None:
        if radius is None:
            radius = self.options.radius
        match point, angle:
            case (elements.DockingPort(), _) | (complex(), None):
                self._ops.append(Segment(point, radius))
            case complex() as p, float() as a:
                self._ops.append(Segment(elements.DockingPort(p, a + math.pi), radius))
            case _:
                raise TypeError(point)

    def bridge(self, width, length):
        self._ops.append(Bridge(CpwBridgeArgs(width, length)))

    def auto_meander(self, width, depth, direction, in_pos, out_pos, radius, length):
        self._ops.append(
            AutoMeander(width, depth, direction, in_pos, out_pos, radius, length)
        )

    def build(self) -> list[PathOp]:
        return list(self._ops)


if __name__ == "__main__":
    # logging.basicConfig(level=logging.DEBUG)
    # elem = Element()
    # builder = CoplanarWaveguideBuilder(0 + 0j)
    # builder.segment(200 + 100j)
    # builder.segment(-100 + 100j)
    # builder.auto_meander(700, 2000, "right", 100, 100, 30, 6000)
    # builder.segment(-5000 + 100j)
    # cpw = builder.build()
    # cpw = elem.add_element(cpw)
    # bp = BondPad()
    # bp = elem.add_element(bp, cpw.port_start, bp.port_line)

    # elem.write_gds("../ptest.gds")
    # elem.view()
    config.use_preset_design()
    p1 = -100 + 100j
    a1 = -2
    r1 = 50
    p3 = 50 - 50j
    a3 = -math.pi / 2
    r3 = 49.99999
    op_gen = PathOpGenerator(p1, r1)
    op_gen.segment(p3, r3, a3)
    op_gen.segment(200 - 50j)
    op_gen.segment(300 - 300j)
    points = [180 - 500j, 500 - 180j] * 10
    points = np.cumsum(points) + 300 - 300j
    for p in points:
        op_gen.segment(p)
        op_gen.auto_meander(100, 150, "left", 10, 20, 30, 100)
    ops = op_gen.build()

    builder = BasicPathBuilder(PathOptions(), config.global_config)
    builder.process_ops(ops)
    lc = LengthTracker()
    lc.process_ops(ops)
    print(lc.total_length)
    bp = BondPad()
    elem = elements.Element()
    cpw = builder.build()
    cpw = elem.add_element(cpw)
    elem.add_element(bp, cpw.port_start, bp.port_line)
    elem.add_element(bp, cpw.port_end, bp.port_line)
    elem.flatten()
    elem.view()
