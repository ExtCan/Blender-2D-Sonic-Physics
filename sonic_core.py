# SPDX-License-Identifier: GPL-3.0-or-later OR MIT
"""
sonic_core.py -- Genesis-accurate Sonic the Hedgehog physics engine (pure Python).

This module contains ZERO Blender dependencies so that it can be unit tested
with a stock Python interpreter and reused outside of Blender.  Every constant
and every ordering decision below is taken directly from the Sonic 1
disassembly (github.com/sonicretro/s1disasm, file "_incObj/01 Sonic.asm") and
cross-checked against the Sonic Retro "Sonic Physics Guide".

--------------------------------------------------------------------------------
UNITS
--------------------------------------------------------------------------------
The original game runs at 60 FPS and stores speeds as 8.8 fixed point numbers
("subpixels", 1 pixel == 256 subpixels).  We work in floating point *pixels per
frame* which is exactly value/256 of the raw disassembly immediates, e.g.

    son_acceleration = $C   -> 12 / 256 == 0.046875  px/frame^2
    son_maxspeed     = $600 -> 0x600 / 256 == 6.0    px/frame
    gravity          = $38  -> 0x38 / 256 == 0.21875 px/frame^2
    son_jumpspeed    = $680 -> 0x680 / 256 == 6.5    px/frame

The Blender layer is responsible for mapping "pixels" onto Blender units via a
user controllable scale; the physics itself is scale independent.

--------------------------------------------------------------------------------
COORDINATE CONVENTION (differs from the Genesis, matches Blender)
--------------------------------------------------------------------------------
    +X  : horizontal, to the right.
    +Z  : vertical,   up.       (the Genesis uses Y-down; we use Z-up)
    The player "position" is the FEET / bottom-centre of the collision box, so
    z == ground height means the player is stood on the floor.

    When the Blender layer drives the player along a 3D curve, "x" is really
    the horizontal ARC LENGTH along the path -- the physics stays purely 2D
    (authentic to the Genesis) and the path mapper turns (x, z) back into a 3D
    world position.  Nothing in this module needs to know about that.

    ground_angle (theta), radians:
        0            -> flat ground.
        theta > 0    -> ground rises towards +X (uphill to the right).
        The ground tangent (direction of travel for positive ground speed) is
        (cos theta, sin theta); the surface normal is (-sin theta, cos theta).

Because of the Z-up flip, a few Genesis signs are inverted here:
    * gravity is applied as  z_vel -= GRAVITY   (Genesis adds to a Y-down vel).
    * jump force is applied as z_vel += JUMP_FORCE (upwards).
    * the hurt knockback is z_vel = +HURT_Z_FORCE (Genesis: obVelY = -$400).

--------------------------------------------------------------------------------
COLLISION WORLDS
--------------------------------------------------------------------------------
The engine talks to its surroundings through a tiny CollisionWorld interface
(floor / wall / ceiling queries).  The default TerrainWorld wraps the old
Terrain height-field, reproducing the original behaviour exactly.  The Blender
layer supplies a BVH-backed world that adds accurate mesh collision with
surface types (Walkable / Damage / Speed Up) -- see __init__.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


# =============================================================================
#  PHYSICS CONSTANTS  (defaults == authentic Sonic 1 values)
# =============================================================================
# These live on the engine instance (see SonicEngine.__init__) so the Blender
# UI can override any of them at runtime, but the module level values below are
# the canonical, disassembly-derived defaults and are used by the unit tests.

# -- Ground running -----------------------------------------------------------
ACCELERATION      = 0.046875   # $C   ; ground speed gained per frame when holding a direction
DECELERATION      = 0.5        # $80  ; ground speed lost per frame when pressing the opposite way
FRICTION          = 0.046875   # $C   ; ground speed bled off per frame with no direction held (== ACCELERATION)
TOP_SPEED         = 6.0        # $600 ; maximum *input driven* ground speed (slopes may exceed this)

# -- Air ----------------------------------------------------------------------
AIR_ACCELERATION  = 0.09375    # $18  ; air control accel per frame (== 2x ground ACCELERATION)
GRAVITY           = 0.21875    # $38  ; downward accel per frame while airborne
JUMP_FORCE        = 6.5        # $680 ; initial upward speed of a jump
JUMP_RELEASE_CAP  = 4.0        # $400 ; if the jump button is released, upward speed is clamped to this
AIR_DRAG_SHIFT    = 5          # asr #5 ; air drag divides x speed by 2**5 (== /32) each frame near the apex

# -- Slopes -------------------------------------------------------------------
SLOPE_FACTOR_WALK      = 0.125     # $20 ; slope pull while running
SLOPE_FACTOR_ROLL_UP   = 0.078125  # $50 / 4 ; slope pull while rolling *uphill*
SLOPE_FACTOR_ROLL_DOWN = 0.3125    # $50     ; slope pull while rolling *downhill*

# -- Rolling ------------------------------------------------------------------
ROLL_FRICTION     = 0.0234375  # $6  ; friction while rolling (== ACCELERATION / 2)
ROLL_DECELERATION = 0.125      # $20 ; deceleration while rolling (== DECELERATION / 4)
ROLL_MIN_SPEED    = 0.5        # $80 ; |ground speed| required to begin a roll

# -- Slipping / falling off steep slopes --------------------------------------
FALL_SLIP_SPEED   = 2.5        # $280 ; below this |ground speed| on a steep slope you slip and detach
CONTROL_LOCK_TIME = 30         # 30 frames (half a second) of no left/right input after slipping
SLIP_ANGLE        = math.radians(45.0)   # slopes steeper than this can cause slipping when too slow

# -- Global limits ------------------------------------------------------------
MAX_GLOBAL_SPEED  = 16.0       # $1000 ; the "screen scroll" hard cap applied to every velocity component
MAX_Y_SPEED       = 15.75      # $FC0  ; vertical speed cap while airborne

# -- Air angle recovery -------------------------------------------------------
AIR_ANGLE_RETURN  = math.radians(360.0 / 256.0 * 2.0)  # 2 Genesis-angle units/frame == 2.8125 deg/frame

# -- Spindash (a Sonic 2 mechanic; not present in Sonic 1, values from the SPG)
SPINDASH_CHARGE     = 2.0   # revs added per rev-button press
SPINDASH_MAX        = 8.0   # maximum accumulated revs
SPINDASH_BASE_SPEED = 8.0   # release speed at 0 revs
# release speed == SPINDASH_BASE_SPEED + floor(revs) / 2   (max 8 + 4 == 12)

# -- Super Peel Out (Sonic CD; values from the SPG) ---------------------------
# Hold Up + press a jump button to start revving.  Sonic stays put and "runs on
# the spot" (the figure-8 legs).  After PEELOUT_CHARGE_TIME frames the charge
# is complete; releasing Up then launches at PEELOUT_LAUNCH_SPEED in the facing
# direction *without* curling into a ball (Sonic runs, so unlike the spindash
# he is vulnerable).  Releasing Up before the charge completes launches
# nothing: ground speed stays 0.
PEELOUT_CHARGE_TIME  = 30     # frames of Up held before the launch is armed
PEELOUT_LAUNCH_SPEED = 12.0   # $C00 ; == a fully-revved spindash release

# -- Getting hurt (Sonic_Hurt / Touch_ChkHurt in the disassembly) -------------
# On taking damage Sonic is flung up and away from the hazard and loses all
# control until he lands.  While hurt, gravity is weaker ($30 instead of $38).
# After landing (speeds are zeroed) he is invulnerable for 2 seconds.
HURT_X_FORCE         = 2.0      # $200 ; horizontal knockback, away from the hazard
HURT_Z_FORCE         = 4.0      # $400 ; vertical knockback (upward)
HURT_GRAVITY         = 0.1875   # $30  ; gravity while in the hurt state
INVULNERABILITY_TIME = 120      # $78  ; post-landing invulnerability, frames

# -- Speed booster (the Chemical Plant / Stardust Speedway pads) --------------
# A booster *sets* ground speed to its power (it never stacks): if you are
# already moving faster in the boost direction, nothing happens; slower (or
# moving the wrong way) and your speed becomes +/-power.
BOOST_DEFAULT_POWER  = 16.0     # $1000 ; classic CPZ boosters

# -- Solid-collision sensor geometry ------------------------------------------
STEP_UP_REACH        = 8.0      # floor sensors reach this far *above* the feet (small steps are climbed)
WALL_SENSOR_LOW      = 10.0     # height of the low push sensor above the feet
WALL_SENSOR_HIGH_F   = 0.72     # the high push sensor sits at this fraction of the body height
WALL_MAX_ANGLE       = math.radians(50.0)  # grounded push sensors switch off on steeper slopes
                                           # (the classic engine rotates into wall mode instead;
                                           #  full 4-mode loops are out of scope here)

# -- Hitbox (radii, pixels) ---------------------------------------------------
WIDTH_RADIUS        = 9    # sonic_width  == 18/2 ; ground-sensor half width
PUSH_RADIUS         = 10   # sonic_solid_width-ish ; wall half width (used for the display box)
HEIGHT_RADIUS_STAND = 19   # sonic_height      == 38/2
HEIGHT_RADIUS_ROLL  = 14   # sonic_roll_height == 28/2

# Animation / state thresholds (used purely to expose descriptive attributes).
RUN_SPEED_THRESHOLD  = 6.0   # |ground speed| at/above which the "running" animation plays
DASH_SPEED_THRESHOLD = 9.0   # arbitrary "very fast" descriptive state
SKID_SPEED_THRESHOLD = 4.0   # $400 ; skid ("stopping") animation kicks in above this speed on reversal


# =============================================================================
#  EXTENDED SURFACES / VOLUMES  (ice, water, quicksand)
# =============================================================================
# -- Ice (a low-friction floor surface) ---------------------------------------
ICE_FRICTION_MULT = 0.25   # friction / deceleration are quartered on ice

# -- Water (a passthrough volume: underwater physics + drowning) --------------
# Underwater the classic games roughly halve movement and greatly reduce gravity;
# a ~30 s air timer counts down and, at zero, Sonic drowns.  Values from the SPG.
WATER_ACCEL_MULT    = 0.5
WATER_TOP_MULT      = 0.5
WATER_FRICTION_MULT = 0.5
WATER_GRAVITY_MULT  = 0.2857   # 0.0625 / 0.21875 -> the $10 underwater gravity
WATER_JUMP_MULT     = 0.53     # keeps jump apex ~equal under the lower gravity
AIR_TIME            = 1800      # frames of air underwater (30 s @ 60fps) before drowning

# -- Quicksand (a passthrough volume: sink unless the jump button is mashed) ---
QUICKSAND_SINK  = 0.35   # px/frame the feet sink while idle in quicksand
QUICKSAND_CLIMB = 1.6    # px risen per jump press while mashing out
QUICKSAND_DRAG  = 0.80   # horizontal velocity retained per frame in quicksand

# -- Springs (launch pads) ----------------------------------------------------
SPRING_POWER_YELLOW = 10.0     # $A00
SPRING_POWER_RED    = 16.0     # $1000

# -- Enemy rebound (bopping a badnik) -----------------------------------------
ENEMY_BOUNCE_SPEED  = 5.0      # upward rebound after destroying an enemy


# =============================================================================
#  CHARACTER MOVES  (all opt-in; each is disabled unless enabled on the engine)
# =============================================================================
# These are not from the Sonic 1 disassembly -- they are later-game abilities.
# The numbers below are hand-tuned to feel right and are all runtime-tunable.

# -- Flight (Tails) -----------------------------------------------------------
FLIGHT_TIME           = 480     # frames Tails can ascend before tiring (8 s)
FLIGHT_ASCEND_IMPULSE = 0.5     # upward speed added per flap
FLIGHT_MAX_UP         = 1.0     # cap on the upward speed flapping can build
FLIGHT_GRAVITY_MULT   = 0.35    # floaty gravity while the flight timer is live

# -- Gliding / Climbing (Knuckles) --------------------------------------------
GLIDE_SPEED       = 4.0    # forward speed built while gliding
GLIDE_ACCEL       = 0.03   # how fast glide speed builds
GLIDE_FALL_SPEED  = 0.5    # steady descent rate while gliding
CLIMB_SPEED       = 1.0    # up/down speed while clinging to a wall
CLIMB_JUMP_X      = 3.5    # push-off speed when jumping away from a wall
CLIMB_JUMP_Z      = 4.0    # up speed when jumping off a wall

# -- Drop Dash (Sonic Mania) --------------------------------------------------
DROPDASH_CHARGE_TIME = 20     # frames of jump held in the air before it arms
DROPDASH_SPEED       = 8.0    # launch speed on landing (like a base spindash)
DROPDASH_MAX         = 12.0   # cap when adding to existing momentum

# -- Homing Attack (modern) ---------------------------------------------------
HOMING_SPEED = 8.0     # dash speed toward the locked-on target
HOMING_RANGE = 220.0   # how near a target must be to lock on (pixels)

# -- Boost (modern) -----------------------------------------------------------
BOOST_SPEED      = 12.0   # speed the boost holds you at
BOOST_ENERGY_MAX = 100.0  # boost meter capacity
BOOST_DRAIN      = 0.5    # meter drained per frame while boosting
BOOST_REGEN      = 0.1    # meter regained per frame while not boosting

# -- Hovering -----------------------------------------------------------------
HOVER_TIME = 120   # frames you can hover at the apex before falling


# =============================================================================
#  SURFACES  (what a piece of collision *does* when touched)
# =============================================================================
SURFACE_WALKABLE = "WALKABLE"
SURFACE_DAMAGE   = "DAMAGE"
SURFACE_SPEEDUP  = "SPEED_UP"
SURFACE_ICE       = "ICE"        # a solid floor with reduced friction
SURFACE_WATER     = "WATER"      # a passthrough volume: underwater physics + drowning
SURFACE_QUICKSAND = "QUICKSAND"  # a passthrough volume: sink unless the jump button is mashed


@dataclass
class Surface:
    """Describes the gameplay behaviour of a solid or a trigger volume.

    kind        : SURFACE_WALKABLE (default; plain solid), SURFACE_DAMAGE
                  (hurts on contact) or SURFACE_SPEEDUP (a booster).
    boost_sign  : +1 pushes right (+x), -1 pushes left, 0 == "facing direction".
    boost_power : the ground speed the booster sets (see BOOST_DEFAULT_POWER).
    name        : free-form identifier (the Blender object name).
    """
    kind: str = SURFACE_WALKABLE
    boost_sign: int = 0
    boost_power: float = BOOST_DEFAULT_POWER
    name: str = ""


WALKABLE_SURFACE = Surface()      # shared default instance


@dataclass
class FloorHit:
    height: float                 # floor Z at the sensor
    angle: float                  # ground angle (radians) at the sensor
    surface: Surface = field(default_factory=Surface)


@dataclass
class WallHit:
    distance: float               # distance from the query x to the wall face
    surface: Surface = field(default_factory=Surface)


@dataclass
class CeilingHit:
    height: float                 # Z of the ceiling face
    surface: Surface = field(default_factory=Surface)


# =============================================================================
#  TERRAIN  (analytic height-field ground -- the original floor model)
# =============================================================================
class Terrain:
    """Abstract ground.  height() returns the floor Z at a world X; angle()
    returns the ground angle (radians, +ve rising to +X) at that X."""

    def height(self, x: float) -> float:      # pragma: no cover - interface
        raise NotImplementedError

    def angle(self, x: float) -> float:       # pragma: no cover - interface
        raise NotImplementedError


class FlatTerrain(Terrain):
    """An infinite flat floor at a fixed Z (0 by default)."""

    def __init__(self, z: float = 0.0):
        self.z = z

    def height(self, x: float) -> float:
        return self.z

    def angle(self, x: float) -> float:
        return 0.0


class HeightfieldTerrain(Terrain):
    """A ground profile sampled from an ordered list of (x, z) points, e.g. a
    Blender curve flattened onto the X-Z plane.  Between samples the height is
    linearly interpolated and the angle is the segment slope; outside the sample
    range the terrain is flat at the nearest end point.

    The profile must be single valued in X (a height-field); vertical walls or
    overhangs cannot be represented.  When the Blender layer follows a curve's
    3D depth, X is the horizontal arc length along the path, which keeps the
    profile single valued even when the path bends freely in plan view."""

    def __init__(self, points):
        pts = sorted((float(x), float(z)) for x, z in points)
        # Collapse duplicate / near-duplicate X so the slope never divides by ~0.
        cleaned = []
        for x, z in pts:
            if cleaned and abs(x - cleaned[-1][0]) < 1e-9:
                cleaned[-1] = (cleaned[-1][0], max(cleaned[-1][1], z))  # keep the higher floor
            else:
                cleaned.append((x, z))
        if not cleaned:
            cleaned = [(0.0, 0.0)]
        self.xs = [p[0] for p in cleaned]
        self.zs = [p[1] for p in cleaned]

    def _seg(self, x: float):
        xs = self.xs
        if x <= xs[0]:
            return 0, 0.0
        if x >= xs[-1]:
            return len(xs) - 1, 1.0
        # binary search for the segment containing x
        lo, hi = 0, len(xs) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if xs[mid] <= x:
                lo = mid
            else:
                hi = mid
        x0, x1 = xs[lo], xs[lo + 1]
        t = (x - x0) / (x1 - x0) if x1 > x0 else 0.0
        return lo, t

    def height(self, x: float) -> float:
        xs, zs = self.xs, self.zs
        if x <= xs[0]:
            return zs[0]
        if x >= xs[-1]:
            return zs[-1]
        i, t = self._seg(x)
        return zs[i] + (zs[i + 1] - zs[i]) * t

    def angle(self, x: float) -> float:
        xs, zs = self.xs, self.zs
        if x <= xs[0] or x >= xs[-1] or len(xs) < 2:
            return 0.0
        i, _ = self._seg(x)
        dx = xs[i + 1] - xs[i]
        dz = zs[i + 1] - zs[i]
        return math.atan2(dz, dx)


# =============================================================================
#  COLLISION WORLD  (floor / wall / ceiling queries the engine performs)
# =============================================================================
class CollisionWorld:
    """The engine's view of its surroundings.  Implementations may combine an
    analytic Terrain with arbitrary solid geometry (the Blender layer casts
    rays into BVH trees built from evaluated meshes).

    All heights/distances are in pixels, matching the engine.
    """

    #: False lets the engine skip wall & ceiling sensors entirely (the pure
    #: Terrain world has neither, which reproduces the original behaviour).
    has_walls = False
    has_ceilings = False

    def floor(self, x: float, z: float, reach_up: float, reach_down: float):
        """Best (highest) floor beneath the feet.  Sensors scan from
        ``z + reach_up`` down to ``z - reach_down``; return a FloorHit or
        None.  ``hit.height`` may exceed ``z`` (a small step -- the engine
        climbs it) but never ``z + reach_up``."""
        raise NotImplementedError                     # pragma: no cover

    def wall(self, x: float, z: float, direction: int, max_dist: float):
        """Nearest wall face within ``max_dist`` of ``x`` along ``direction``
        (+1/-1), probed at height ``z``.  Return a WallHit or None."""
        return None

    def ceiling(self, x: float, z_head: float, reach_up: float):
        """Lowest ceiling face between ``z_head`` and ``z_head + reach_up``
        above the head.  Return a CeilingHit or None."""
        return None


class TerrainWorld(CollisionWorld):
    """Wraps a Terrain as a CollisionWorld: an infinite walkable floor with no
    walls and no ceilings.  This is the engine default and reproduces the
    original (pre-mesh-collision) behaviour bit for bit."""

    def __init__(self, terrain: Terrain | None = None):
        self.terrain = terrain if terrain is not None else FlatTerrain(0.0)

    def floor(self, x, z, reach_up, reach_down):
        # The analytic terrain always knows its exact height; reach limits are
        # irrelevant (matching the original unlimited snap behaviour).
        return FloorHit(self.terrain.height(x), self.terrain.angle(x), WALKABLE_SURFACE)


# =============================================================================
#  INPUT
# =============================================================================
@dataclass
class Inputs:
    """A single frame of controller state.  The *_held flags are the current
    button state; jump_pressed / *_pressed are rising edges (True only on the
    frame the button went down) which the Blender layer computes from key
    events (taking auto-repeat into account)."""
    left: bool = False
    right: bool = False
    up: bool = False
    down: bool = False

    a: bool = False      # SEGA A  (mapped to keyboard A)
    b: bool = False      # SEGA B  (mapped to keyboard S)
    c: bool = False      # SEGA C  (mapped to keyboard D)
    x: bool = False      # SEGA X  (mapped to keyboard Q)
    y: bool = False      # SEGA Y  (mapped to keyboard W)
    z: bool = False      # SEGA Z  (mapped to keyboard E)
    start: bool = False  # Start   (mapped to Enter)

    # rising edges
    jump_pressed: bool = False   # any of A/B/C pressed this frame
    down_pressed: bool = False

    @property
    def jump_held(self) -> bool:
        return self.a or self.b or self.c


# =============================================================================
#  ENGINE
# =============================================================================
class SonicEngine:
    """A single-player Sonic physics simulation.  Call step(inputs) once per
    frame (60 FPS).  Read state either directly off the attributes or via
    snapshot() which returns the flat dictionary the Blender layer copies onto
    the empty's custom properties."""

    def __init__(self, terrain: Terrain | None = None,
                 world: CollisionWorld | None = None):
        self.terrain = terrain if terrain is not None else FlatTerrain(0.0)
        self.world = world if world is not None else TerrainWorld(self.terrain)

        # ---- tunable constants (seeded from the module defaults) -------------
        self.acceleration = ACCELERATION
        self.deceleration = DECELERATION
        self.friction = FRICTION
        self.top_speed = TOP_SPEED
        self.air_acceleration = AIR_ACCELERATION
        self.gravity = GRAVITY
        self.jump_force = JUMP_FORCE
        self.jump_release_cap = JUMP_RELEASE_CAP
        self.slope_factor_walk = SLOPE_FACTOR_WALK
        self.slope_factor_roll_up = SLOPE_FACTOR_ROLL_UP
        self.slope_factor_roll_down = SLOPE_FACTOR_ROLL_DOWN
        self.roll_friction = ROLL_FRICTION
        self.roll_deceleration = ROLL_DECELERATION
        self.roll_min_speed = ROLL_MIN_SPEED
        self.fall_slip_speed = FALL_SLIP_SPEED
        self.control_lock_time = CONTROL_LOCK_TIME
        self.max_global_speed = MAX_GLOBAL_SPEED
        self.max_y_speed = MAX_Y_SPEED
        self.air_angle_return = AIR_ANGLE_RETURN
        self.spindash_charge = SPINDASH_CHARGE
        self.spindash_max = SPINDASH_MAX
        self.spindash_base_speed = SPINDASH_BASE_SPEED
        self.enable_peelout = True
        self.peelout_charge_time = PEELOUT_CHARGE_TIME
        self.peelout_launch_speed = PEELOUT_LAUNCH_SPEED
        self.hurt_x_force = HURT_X_FORCE
        self.hurt_z_force = HURT_Z_FORCE
        self.hurt_gravity = HURT_GRAVITY
        self.invulnerability_time = INVULNERABILITY_TIME
        self.height_radius_stand = float(HEIGHT_RADIUS_STAND)
        self.height_radius_roll = float(HEIGHT_RADIUS_ROLL)
        self.width_radius = float(WIDTH_RADIUS)
        self.push_radius = float(PUSH_RADIUS)
        self.run_speed_threshold = RUN_SPEED_THRESHOLD
        self.dash_speed_threshold = DASH_SPEED_THRESHOLD
        self.skid_speed_threshold = SKID_SPEED_THRESHOLD
        # How far the floor may sit below the feet before the player launches
        # off a convex slope.  Emergent "ramp launch at speed" comes out of this
        # fixed reach interacting with per-frame horizontal step size.
        self.ground_snap_distance = 14.0
        # Solid-sensor geometry (only used when the world has walls/ceilings).
        self.step_up_reach = STEP_UP_REACH
        self.wall_sensor_low = WALL_SENSOR_LOW
        self.wall_max_angle = WALL_MAX_ANGLE

        # ---- extended surface / volume tunables -----------------------------
        self.ice_friction_mult = ICE_FRICTION_MULT
        self.water_accel_mult = WATER_ACCEL_MULT
        self.water_top_mult = WATER_TOP_MULT
        self.water_friction_mult = WATER_FRICTION_MULT
        self.water_gravity_mult = WATER_GRAVITY_MULT
        self.water_jump_mult = WATER_JUMP_MULT
        self.air_time = AIR_TIME
        self.quicksand_sink = QUICKSAND_SINK
        self.quicksand_climb = QUICKSAND_CLIMB
        self.quicksand_drag = QUICKSAND_DRAG
        self.spring_power_yellow = SPRING_POWER_YELLOW
        self.spring_power_red = SPRING_POWER_RED
        self.enemy_bounce_speed = ENEMY_BOUNCE_SPEED
        self.lethal_without_rings = False   # opt-in: a hit with 0 rings kills

        # ---- move enables (all opt-in) --------------------------------------
        self.enable_spindash = True      # Down + jump (Sonic 2+). Off for Sonic 1 / CD.
        self.enable_flight = False       # Tails
        self.enable_gliding = False      # Knuckles
        self.enable_climbing = False     # Knuckles (needs walls; driven by the host)
        self.enable_dropdash = False     # Sonic Mania
        self.enable_homing = False       # modern (needs a target; driven by the host)
        self.enable_boost = False        # modern
        self.enable_hovering = False

        # ---- move tunables --------------------------------------------------
        self.flight_time = FLIGHT_TIME
        self.flight_ascend_impulse = FLIGHT_ASCEND_IMPULSE
        self.flight_max_up = FLIGHT_MAX_UP
        self.flight_gravity_mult = FLIGHT_GRAVITY_MULT
        self.glide_speed = GLIDE_SPEED
        self.glide_accel = GLIDE_ACCEL
        self.glide_fall_speed = GLIDE_FALL_SPEED
        self.climb_speed = CLIMB_SPEED
        self.climb_jump_x = CLIMB_JUMP_X
        self.climb_jump_z = CLIMB_JUMP_Z
        self.dropdash_charge_time = DROPDASH_CHARGE_TIME
        self.dropdash_speed = DROPDASH_SPEED
        self.dropdash_max = DROPDASH_MAX
        self.homing_speed = HOMING_SPEED
        self.homing_range = HOMING_RANGE
        self.boost_speed = BOOST_SPEED
        self.boost_energy_max = BOOST_ENERGY_MAX
        self.boost_drain = BOOST_DRAIN
        self.boost_regen = BOOST_REGEN
        self.hover_time = HOVER_TIME

        # ---- state -----------------------------------------------------------
        self.x = 0.0
        self.z = 0.0
        self.ground_speed = 0.0      # inertia along the ground tangent
        self.x_vel = 0.0             # world velocity, +X
        self.z_vel = 0.0             # world velocity, +Z (up)
        self.ground_angle = 0.0      # radians

        self.on_ground = True
        self.rolling = False         # in a ball on the ground
        self.jumping = False         # airborne specifically because of a jump (enables variable height)
        self.roll_jump = False       # jumped straight out of a roll -> no air control
        self.control_lock_timer = 0
        self.facing = 1              # +1 right, -1 left

        self.spindash_active = False
        self.spindash_revs = 0.0

        self.peelout_active = False  # revving a Super Peel Out (Up held)
        self.peelout_timer = 0       # frames spent charging

        self.is_hurt = False         # airborne hurt/knockback state (no control)
        self.invulnerability_timer = 0
        self.hits_taken = 0

        self.boosted = False         # a booster fired this frame (descriptive)

        self.ducking = False
        self.looking_up = False
        self.pushing = False         # solidly pressing into a wall
        self.skidding = False
        self.braking = False

        # ---- extended state -------------------------------------------------
        self.on_ice = False

        self.underwater = False
        self.air_timer = self.air_time
        self.is_dead = False
        self.death_cause = ""

        self.in_quicksand = False

        self.ring_count = 0
        self.rings_lost = 0

        self.sprung = False          # a spring launched us this frame (descriptive)

        self.flying = False          # Tails flight active
        self.flight_timer = 0
        self.flight_tired = False    # flight time used up; can't refly until landing

        self.gliding = False         # Knuckles glide active
        self.climbing = False        # clinging to a wall
        self.climb_wall_dir = 0      # +1 wall to the right, -1 to the left

        self.dropdash_charging = False
        self.dropdash_charge_frames = 0
        self.dropdash_ready = False

        self.homing_active = False
        self.homing_target = None    # (x, z) in engine space, set by the host; None == no lock

        self.boosting = False
        self.boost_energy = self.boost_energy_max

        self.hovering = False
        self.hover_timer = 0

        # Bookkeeping for descriptive attributes.
        self._prev_x_vel = 0.0
        self._prev_z_vel = 0.0
        self.frame = 0

    # ------------------------------------------------------------------ setup
    def _floor_below(self, x: float, z: float):
        """Highest floor at or below z (a generous scan used for placement)."""
        return self.world.floor(x, z, 1.0, 1.0e9)

    def place_on_ground(self, x: float):
        """Drop the player onto the ground at world X (feet on the floor)."""
        self.x = x
        hit = self._floor_below(x, 1.0e9)
        if hit is not None:
            self.z = hit.height
            self.ground_angle = hit.angle
        else:
            self.z = self.terrain.height(x)
            self.ground_angle = self.terrain.angle(x)
        self.on_ground = True
        self.rolling = False
        self.jumping = False
        self.ground_speed = 0.0
        self.x_vel = 0.0
        self.z_vel = 0.0

    def set_position(self, x: float, z: float):
        """Place the player at an explicit point.  If it is above the floor the
        player starts airborne and falls; otherwise it starts grounded."""
        self.x = x
        hit = self._floor_below(x, z)
        gz = hit.height if hit is not None else self.terrain.height(x)
        if z > gz + 1e-4:
            self.z = z
            self.on_ground = False
            self.jumping = False
            self.ground_speed = 0.0
            self.x_vel = 0.0
            self.z_vel = 0.0
        else:
            self.place_on_ground(x)

    # ============================================================= main step
    def step(self, inp: Inputs):
        """Advance the simulation by exactly one 60fps frame."""
        self._prev_x_vel = self.x_vel
        self._prev_z_vel = self.z_vel
        self.ducking = False
        self.looking_up = False
        self.skidding = False
        self.pushing = False
        self.boosted = False
        self.sprung = False
        self.boosting = False
        self.in_quicksand = False

        # Dead players just fall (the classic death arc), ignoring all input.
        if self.is_dead:
            self._step_dead()
            self.frame += 1
            return

        # Underwater air countdown -> drowning.
        if self.underwater and self.air_timer > 0:
            self.air_timer -= 1
            if self.air_timer <= 0:
                self.die("drowned")
                self._step_dead()
                self.frame += 1
                return

        # Post-landing invulnerability counts down while in control.
        if not self.is_hurt and self.invulnerability_timer > 0:
            self.invulnerability_timer -= 1

        if self.on_ground:
            self._step_ground(inp)
        else:
            self._step_air(inp)

        self._update_descriptive(inp)
        self.frame += 1

    def _step_dead(self):
        """A dead player: a brief upward hop (set by die()) then a plain fall,
        with no control.  The host stops the simulation / flags the death."""
        self.on_ground = False
        self.ground_speed = 0.0
        self.x_vel = 0.0
        self.z += self.z_vel
        self.z_vel -= self.gravity

    # ------------------------------------------------------------- GROUND
    def _step_ground(self, inp: Inputs):
        # ---- 0a. Super Peel Out charge / release ---------------------------
        # Revving replaces normal movement for the frame, exactly like the
        # spindash below.  (Started in step 1.)
        if self.peelout_active:
            launched = self.peelout_timer >= self.peelout_charge_time
            self._update_peelout(inp)
            if self.peelout_active:             # still revving -> stay put
                self._ground_snap()
                return
            if launched:
                # A successful launch moves THIS frame at exactly the launch
                # speed: convert to world velocity and travel without running
                # friction/acceleration (which would otherwise shave a frame's
                # worth off the top when no direction is held).
                self._clamp_ground_speed()
                self.x_vel = self.ground_speed * math.cos(self.ground_angle)
                self.z_vel = self.ground_speed * math.sin(self.ground_angle)
                self._move_horizontal(self.x_vel, grounded=True, inp=inp)
                self.z += self.z_vel
                if self.is_hurt:
                    return
                self._ground_snap()
                return
            # Under-charged release: fall through to normal movement so the
            # frame behaves like an ordinary standing frame.

        # ---- 0b. Spindash charge / release ---------------------------------
        # A spindash may only be charged while ducking (Down held) and roughly
        # stationary; charging replaces normal movement for the frame.
        if self.spindash_active:
            self._update_spindash(inp)
            if self.spindash_active:            # still charging -> stay put
                self._ground_snap()
                return
            # released this frame: fall through so the launch takes effect and
            # rolling movement/collision run on the very next frame.

        # ---- 1. Jump (or begin a spindash / Super Peel Out) -----------------
        # Down + jump while stationary starts a spindash instead of jumping;
        # Up + jump while stationary starts a Super Peel Out (Sonic CD).
        if inp.jump_pressed:
            near_still = abs(self.ground_speed) < self.roll_min_speed
            if self.enable_spindash and inp.down and near_still and not self.rolling:
                self.spindash_active = True
                self.spindash_revs = 0.0
                self.ducking = True
                self._ground_snap()
                return
            elif (self.enable_peelout and inp.up and near_still
                    and not self.rolling):
                self.peelout_active = True
                self.peelout_timer = 0
                self._ground_snap()
                return
            else:
                self._do_jump()
                return   # remaining ground routines are skipped on the jump frame

        # ---- 2. Slope resistance -------------------------------------------
        self._apply_slope_resistance()

        # ---- 3. Directional input + friction -------------------------------
        if self.rolling:
            self._roll_input(inp)
        else:
            self._walk_input(inp)

        # ---- 3b. Boost (modern) --------------------------------------------
        self._boost_move(inp)

        # ---- 4. Begin rolling? ---------------------------------------------
        if not self.rolling:
            self._check_start_roll(inp)

        # Duck / look-up posture (purely descriptive when stationary).
        if not self.rolling and abs(self.ground_speed) < self.roll_min_speed:
            if inp.down:
                self.ducking = True
            elif inp.up:
                self.looking_up = True

        # ---- 5. Clamp and convert ground speed to world velocity -----------
        self._clamp_ground_speed()
        self.x_vel = self.ground_speed * math.cos(self.ground_angle)
        self.z_vel = self.ground_speed * math.sin(self.ground_angle)

        # ---- 6. Move (SpeedToPos), stopped early by push sensors -----------
        self._move_horizontal(self.x_vel, grounded=True, inp=inp)
        self.z += self.z_vel
        if self.is_hurt:               # a damaging wall bounced us this frame
            return

        # ---- 7. Stick to / detach from the floor ---------------------------
        self._ground_snap()
        if self.is_hurt:               # a damaging floor bounced us this frame
            return

        # ---- 8. Slip & detach on steep slopes when too slow ----------------
        self._apply_slope_slip()

    def _walk_input(self, inp: Inputs):
        locked = self.control_lock_timer > 0
        gsp = self.ground_speed
        # Ice cuts friction/deceleration; water cuts acceleration/top/friction.
        water = self.water_accel_mult if self.underwater else 1.0
        ice = self.ice_friction_mult if self.on_ice else 1.0
        acc = self.acceleration * water
        dec = self.deceleration * water * ice
        top = self.top_speed * (self.water_top_mult if self.underwater else 1.0)
        fric = self.friction * self._friction_scale()

        pressing_left = inp.left and not inp.right
        pressing_right = inp.right and not inp.left

        if not locked and pressing_left:
            self.facing = -1
            if gsp > 0:                       # decelerate / skid
                gsp -= dec
                if gsp < 0:
                    gsp = -dec               # $80 min-speed-on-sign-change (== -0.5)
                if self.ground_speed >= self.skid_speed_threshold:
                    self.skidding = True
            else:                             # accelerate left
                # Input only accelerates *up to* top speed; momentum already
                # beyond it (spindash, Peel Out, boosters, slopes) is preserved.
                if gsp > -top:
                    gsp -= acc
                    if gsp < -top:
                        gsp = -top
        elif not locked and pressing_right:
            self.facing = 1
            if gsp < 0:                       # decelerate / skid
                gsp += dec
                if gsp > 0:
                    gsp = dec
                if self.ground_speed <= -self.skid_speed_threshold:
                    self.skidding = True
            else:                             # accelerate right
                if gsp < top:
                    gsp += acc
                    if gsp > top:
                        gsp = top
        elif not (inp.left or inp.right):
            # friction (no direction held); also applies while control-locked
            # with no key held.
            if gsp > 0:
                gsp = max(0.0, gsp - fric)
            elif gsp < 0:
                gsp = min(0.0, gsp + fric)

        self.ground_speed = gsp

    def _roll_input(self, inp: Inputs):
        locked = self.control_lock_timer > 0
        gsp = self.ground_speed
        water = self.water_accel_mult if self.underwater else 1.0
        ice = self.ice_friction_mult if self.on_ice else 1.0
        dec = self.roll_deceleration * water * ice
        rfric = self.roll_friction * self._friction_scale()

        pressing_left = inp.left and not inp.right
        pressing_right = inp.right and not inp.left

        # While rolling, input can only *decelerate* you (never accelerate).
        if not locked and pressing_left:
            self.facing = -1
            if gsp > 0:
                gsp -= dec
                if gsp < 0:
                    gsp = -0.5
        elif not locked and pressing_right:
            self.facing = 1
            if gsp < 0:
                gsp += dec
                if gsp > 0:
                    gsp = 0.5

        # Rolling friction is always applied.
        if gsp > 0:
            gsp = max(0.0, gsp - rfric)
        elif gsp < 0:
            gsp = min(0.0, gsp + rfric)

        self.ground_speed = gsp

        # Unroll once stopped (Sonic 1 unrolls at a dead stop).
        if self.ground_speed == 0.0:
            self.rolling = False

    def _check_start_roll(self, inp: Inputs):
        if (inp.down and not (inp.left or inp.right)
                and abs(self.ground_speed) >= self.roll_min_speed):
            self.rolling = True

    def _apply_slope_resistance(self):
        # slope_term is the *unit* pull direction along the tangent:
        #   Delta ground_speed = factor * (-sin(theta))
        # so uphill motion is slowed and downhill motion is sped up.
        slope_term = -math.sin(self.ground_angle)
        if abs(slope_term) < 1e-9:
            return
        if self.rolling:
            gsp = self.ground_speed
            if gsp == 0.0:
                factor = self.slope_factor_roll_down
            else:
                aids = (slope_term > 0.0) == (gsp > 0.0)   # same sign -> speeds you up (descending)
                factor = self.slope_factor_roll_down if aids else self.slope_factor_roll_up
            self.ground_speed += factor * slope_term
        else:
            # Walking slope resist only applies while actually moving.
            if self.ground_speed != 0.0:
                self.ground_speed += self.slope_factor_walk * slope_term

    def _clamp_ground_speed(self):
        m = self.max_global_speed
        if self.ground_speed > m:
            self.ground_speed = m
        elif self.ground_speed < -m:
            self.ground_speed = -m

    # ------------------------------------------------------------- SOLIDS
    @property
    def _body_height(self) -> float:
        r = self.height_radius_roll if self.rolling else self.height_radius_stand
        return 2.0 * r

    def _move_horizontal(self, dx: float, grounded: bool, inp: Inputs | None = None):
        """Move x by dx, stopping early at push walls (E/F sensors).  Grounded
        push sensors switch off on steep slopes -- the classic engine rotates
        into wall mode there, which (like loops) is out of scope."""
        world = self.world
        walls_on = (world.has_walls and dx != 0.0
                    and (not grounded or abs(self.ground_angle) < self.wall_max_angle))
        if not walls_on:
            self.x += dx
            return

        dirn = 1 if dx > 0 else -1
        span = abs(dx) + self.push_radius + 1.0
        h = self._body_height
        best = None
        for zz in (self.z + self.wall_sensor_low, self.z + h * WALL_SENSOR_HIGH_F):
            hit = world.wall(self.x, zz, dirn, span)
            if hit is not None and (best is None or hit.distance < best.distance):
                best = hit

        if best is None or best.distance >= abs(dx) + self.push_radius:
            self.x += dx
            return

        # Contact: advance only up to the wall face minus the push radius.
        allowed = max(0.0, best.distance - self.push_radius)
        self.x += dirn * min(abs(dx), allowed)
        self.x_vel = 0.0
        if grounded:
            self.ground_speed = 0.0
            if inp is not None:
                self.pushing = (inp.right and not inp.left) if dirn > 0 \
                    else (inp.left and not inp.right)
        if best.surface.kind == SURFACE_DAMAGE:
            self.hurt(-dirn)

    def _friction_scale(self) -> float:
        """Combined friction multiplier from being underwater and/or on ice."""
        s = 1.0
        if self.underwater:
            s *= self.water_friction_mult
        if self.on_ice:
            s *= self.ice_friction_mult
        return s

    def _eff_gravity(self) -> float:
        """Gravity after water (floatier) and flight (floaty) modifiers."""
        g = self.gravity
        if self.underwater:
            g *= self.water_gravity_mult
        if self.flying and self.flight_timer > 0:
            g *= self.flight_gravity_mult
        return g

    def _apply_floor_surface(self, surface: Surface):
        """Gameplay effect of the floor the feet are planted on."""
        if surface is None:
            self.on_ice = False
            return
        # Ice is a plain solid floor whose only effect is reduced friction.
        self.on_ice = (surface.kind == SURFACE_ICE)
        if surface.kind == SURFACE_DAMAGE:
            away = -self.facing if self.ground_speed == 0.0 \
                else (-1 if self.ground_speed > 0 else 1)
            self.hurt(away)
        elif surface.kind == SURFACE_SPEEDUP:
            self.apply_boost(surface.boost_sign, surface.boost_power)

    def _ground_snap(self):
        """Keep the feet glued to the floor, or launch into the air if the
        floor has fallen away further than the sensors can reach."""
        # Mesh floor rays cast from a little above the feet; when climbing a
        # slope fast the floor ahead can rise by nearly |x_vel| per frame, so
        # the upward reach scales with speed (the analytic terrain ignores it).
        reach_up = max(self.step_up_reach, abs(self.x_vel) * 1.5 + 2.0)
        hit = self.world.floor(self.x, self.z, reach_up, self.ground_snap_distance)
        if hit is None or self.z - hit.height > self.ground_snap_distance:
            # Ran off a ledge / over a convex crest too fast -> become airborne.
            self.on_ground = False
            self.jumping = False
            self.on_ice = False
            # keep ball form if rolling off a ramp;
            # world velocity was already set from ground speed this frame.
        else:
            # Snap onto the surface and adopt its angle.
            self.z = hit.height
            self.ground_angle = hit.angle
            self.on_ground = True
            self._apply_floor_surface(hit.surface)

    def _apply_slope_slip(self):
        if self.control_lock_timer > 0:
            self.control_lock_timer -= 1
            return
        if not self.on_ground:
            return
        # Steep enough, and too slow?  Slip: kill ground speed, detach, lock
        # controls for half a second (matches Sonic_SlopeRepel).
        if abs(self.ground_angle) >= SLIP_ANGLE and abs(self.ground_speed) < self.fall_slip_speed:
            self.ground_speed = 0.0
            self.control_lock_timer = self.control_lock_time
            self.on_ground = False
            self.jumping = False

    # ------------------------------------------------------------- JUMP
    def _do_jump(self):
        # Jump perpendicular to the ground surface, *added* to current velocity.
        # normal = (-sin theta, cos theta); jump adds jump_force along it.
        jf = self.jump_force * (self.water_jump_mult if self.underwater else 1.0)
        n = self.ground_angle
        self.x_vel = self.ground_speed * math.cos(n) - jf * math.sin(n)
        self.z_vel = self.ground_speed * math.sin(n) + jf * math.cos(n)
        self.on_ground = False
        self.jumping = True
        self.pushing = False
        self.on_ice = False
        # A fresh jump resets the drop-dash charge and grants a hover budget.
        self.dropdash_charging = False
        self.dropdash_ready = False
        self.dropdash_charge_frames = 0
        self.hover_timer = self.hover_time
        # A jump always balls Sonic up; if he was already rolling it becomes a
        # roll-jump (no mid-air steering).
        self.roll_jump = self.rolling
        self.rolling = True

    # ------------------------------------------------------------- AIR
    def _step_air(self, inp: Inputs):
        # Clinging to a wall is a wholly separate mode.
        if self.climbing:
            self._step_climb(inp)
            return

        z_before = self.z
        hurt = self.is_hurt

        # Which special air ability (if any) controls this frame?
        ability = None if hurt else self._resolve_air_ability(inp)
        # Any non-'dropdash' outcome cancels a drop-dash charge in progress.
        if ability != 'dropdash' and self.dropdash_charging:
            self.dropdash_charging = False
            self.dropdash_ready = False
            self.dropdash_charge_frames = 0

        manages_vertical = False       # ability drives z_vel directly -> skip gravity
        allow_air_control = (not hurt and not self.roll_jump and ability is None)

        if ability == 'flight':
            self._air_flight(inp)
            allow_air_control = True           # Tails still steers left/right
        elif ability == 'glide':
            self._air_glide(inp)
            manages_vertical = True
        elif ability == 'homing':
            self._air_homing(inp)
            manages_vertical = self.homing_active
        elif ability == 'hover':
            self._air_hover(inp)
            manages_vertical = True
        elif ability == 'dropdash':
            self._air_dropdash_charge(inp)     # a ball spin: no air control

        # ---- 1. Variable jump height (a real jump only) --------------------
        if (not hurt and self.jumping and ability is None
                and self.z_vel > self.jump_release_cap and not inp.jump_held):
            self.z_vel = self.jump_release_cap

        # cap vertical speed (JumpHeight / screen scroll cap)
        if self.z_vel > self.max_y_speed:
            self.z_vel = self.max_y_speed
        elif self.z_vel < -self.max_y_speed:
            self.z_vel = -self.max_y_speed

        # ---- 2. Air control (none while hurt, roll-jumping, or in an ability)
        if allow_air_control:
            top = self.top_speed * (self.water_top_mult if self.underwater else 1.0)
            air = self.air_acceleration * (self.water_accel_mult if self.underwater else 1.0)
            if inp.left and not inp.right:
                self.facing = -1
                if self.x_vel > -top:
                    self.x_vel -= air
                    if self.x_vel < -top:
                        self.x_vel = -top
            elif inp.right and not inp.left:
                self.facing = 1
                if self.x_vel < top:
                    self.x_vel += air
                    if self.x_vel > top:
                        self.x_vel = top

            # ---- 3. Air drag (only near the apex) --------------------------
            if 0.0 < self.z_vel < self.jump_release_cap:
                self.x_vel -= _asr(self.x_vel, AIR_DRAG_SHIFT)

        # ---- 3b. Boost (modern) --------------------------------------------
        self._boost_move(inp)

        # ---- 4. Move + gravity ---------------------------------------------
        rising = self.z_vel > 0.0
        self._move_horizontal(self.x_vel, grounded=False)
        self.z += self.z_vel
        if not manages_vertical:
            self.z_vel -= self.hurt_gravity if hurt else self._eff_gravity()

        # ---- 4b. Bump the head on a ceiling --------------------------------
        if rising and self.world.has_ceilings:
            h = self._body_height
            climb = self.z - z_before
            hit = self.world.ceiling(self.x, z_before + h, climb + 2.0)
            if hit is not None and hit.height < self.z + h:
                self.z = hit.height - h
                if self.z_vel > 0.0:
                    self.z_vel = 0.0
                if hit.surface.kind == SURFACE_DAMAGE:
                    self.hurt(-self.facing)

        # ---- 5. Angle recovers toward level while airborne -----------------
        if self.ground_angle > 0.0:
            self.ground_angle = max(0.0, self.ground_angle - self.air_angle_return)
        elif self.ground_angle < 0.0:
            self.ground_angle = min(0.0, self.ground_angle + self.air_angle_return)

        # ---- 6. Landing ----------------------------------------------------
        fall = max(0.0, z_before - self.z)
        hit = self.world.floor(self.x, self.z, fall + 4.0, 0.0)
        if hit is not None and self.z <= hit.height:
            self._land(hit.height, hit.angle, hit.surface)

    # -------------------------------------------------- air-ability dispatch
    def _resolve_air_ability(self, inp: Inputs):
        """Pick the single air ability that controls this frame, by priority:
        homing > flight > glide > drop-dash > hover.  Only enabled moves are
        considered, so a character normally exposes just one of them."""
        if self.homing_active:
            return 'homing'
        if self.enable_homing and inp.jump_pressed and self._homing_can_lock():
            self._start_homing()
            return 'homing'
        if self.flying and self.flight_timer > 0:
            return 'flight'
        if (self.enable_flight and inp.jump_pressed and not self.flight_tired
                and not self.roll_jump):
            self._start_flight()
            return 'flight'
        if self.enable_gliding and inp.jump_held and not self.roll_jump:
            return 'glide'
        if self.enable_dropdash and inp.jump_held and not self.roll_jump:
            return 'dropdash'
        if (self.enable_hovering and inp.jump_held and self.hover_timer > 0
                and self.z_vel <= 0.0):
            return 'hover'
        # No ability -> make sure the lingering flags are cleared.
        self.gliding = False
        return None

    def _start_flight(self):
        self.flying = True
        self.flight_timer = self.flight_time
        self.rolling = False          # Tails flies upright
        self.roll_jump = False

    def _air_flight(self, inp: Inputs):
        self.flying = True
        if self.flight_timer > 0:
            self.flight_timer -= 1
        if self.flight_timer <= 0:
            self.flying = False
            self.flight_tired = True
            return
        # Each fresh jump press flaps: a bounded upward nudge.
        if inp.jump_pressed and self.z_vel < self.flight_max_up:
            self.z_vel = min(self.flight_max_up, self.z_vel + self.flight_ascend_impulse)

    def _air_glide(self, inp: Inputs):
        self.gliding = True
        self.rolling = False
        self.roll_jump = False
        # Steady descent.
        self.z_vel = -self.glide_fall_speed
        # Steer, and build toward glide speed in the facing direction.
        if inp.left and not inp.right:
            self.facing = -1
        elif inp.right and not inp.left:
            self.facing = 1
        target = self.glide_speed * self.facing
        if self.x_vel < target:
            self.x_vel = min(target, self.x_vel + self.glide_accel)
        elif self.x_vel > target:
            self.x_vel = max(target, self.x_vel - self.glide_accel)

    def _homing_can_lock(self) -> bool:
        if self.homing_target is None:
            return False
        tx, tz = self.homing_target
        return math.hypot(tx - self.x, tz - self.z) <= self.homing_range

    def _start_homing(self):
        self.homing_active = True
        self.rolling = True
        self.roll_jump = False

    def _air_homing(self, inp: Inputs):
        if self.homing_target is None:
            # Lost the lock -> a short forward air dash, then normal fall.
            self.homing_active = False
            self.x_vel = self.homing_speed * (self.facing if self.facing != 0 else 1)
            return
        tx, tz = self.homing_target
        dx, dz = tx - self.x, tz - self.z
        dist = math.hypot(dx, dz)
        if dist < 1e-4:
            self.x_vel = 0.0
            self.z_vel = 0.0
            return
        self.x_vel = self.homing_speed * dx / dist
        self.z_vel = self.homing_speed * dz / dist
        self.facing = 1 if dx >= 0 else -1

    def _air_hover(self, inp: Inputs):
        self.hovering = True
        if self.hover_timer > 0:
            self.hover_timer -= 1
        # Hold height (a gentle hover); when the budget runs out, fall.
        if self.hover_timer > 0:
            self.z_vel = 0.0
        else:
            self.hovering = False

    def _air_dropdash_charge(self, inp: Inputs):
        self.rolling = True
        self.dropdash_charging = True
        self.dropdash_charge_frames += 1
        if self.dropdash_charge_frames >= self.dropdash_charge_time:
            self.dropdash_ready = True

    def _step_climb(self, inp: Inputs):
        """Clinging to a wall (Knuckles).  Up/Down crawl; a jump leaps away.
        The host keeps x pinned to the wall and ends the climb at the top or
        bottom (by clearing self.climbing / re-grounding the player)."""
        self.climbing = True
        self.gliding = False
        self.flying = False
        self.x_vel = 0.0
        self.z_vel = 0.0
        if inp.jump_pressed:
            # Wall-jump: push away from the wall.
            d = -self.climb_wall_dir if self.climb_wall_dir else -self.facing
            d = 1 if d >= 0 else -1
            self.climbing = False
            self.x_vel = self.climb_jump_x * d
            self.z_vel = self.climb_jump_z
            self.facing = d
            self.jumping = True
            self.rolling = True
            return
        if inp.up and not inp.down:
            self.z += self.climb_speed
        elif inp.down and not inp.up:
            self.z -= self.climb_speed

    def _boost_move(self, inp: Inputs):
        """Modern boost: hold the boost button (X) to hold a high speed while a
        meter drains; the meter refills slowly when not boosting."""
        if not self.enable_boost:
            return
        if inp.x and self.boost_energy > 0.0:
            self.boosting = True
            self.boost_energy = max(0.0, self.boost_energy - self.boost_drain)
            d = self.facing if self.facing != 0 else 1
            spd = self.boost_speed
            if self.on_ground:
                if (d > 0 and self.ground_speed < spd) or (d < 0 and self.ground_speed > -spd):
                    self.ground_speed = spd * d
            else:
                if (d > 0 and self.x_vel < spd) or (d < 0 and self.x_vel > -spd):
                    self.x_vel = spd * d
        else:
            self.boost_energy = min(self.boost_energy_max, self.boost_energy + self.boost_regen)

    def _land(self, gz: float, theta: float, surface: Surface = WALKABLE_SURFACE):
        self.z = gz
        self.on_ground = True
        self.jumping = False
        self.roll_jump = False
        self.ground_angle = theta
        launched_dropdash = False
        if self.is_hurt:
            # Sonic_HurtStop: touching the floor zeroes both speeds, ends the
            # hurt state and starts the post-hit invulnerability countdown.
            self.is_hurt = False
            self.ground_speed = 0.0
            self.x_vel = 0.0
            self.z_vel = 0.0
            self.invulnerability_timer = self.invulnerability_time
        elif self.dropdash_ready:
            # Drop Dash: land into a rolling burst in the facing direction,
            # adding to (and capped above) any speed carried in.
            d = self.facing if self.facing != 0 else 1
            launch = self.dropdash_speed * d
            if abs(self.ground_speed) < abs(launch) or (self.ground_speed > 0) != (launch > 0):
                self.ground_speed = launch
            else:
                self.ground_speed += self.dropdash_speed * 0.5 * d
            if self.ground_speed > self.dropdash_max:
                self.ground_speed = self.dropdash_max
            elif self.ground_speed < -self.dropdash_max:
                self.ground_speed = -self.dropdash_max
            launched_dropdash = True
        else:
            self.ground_speed = self._landing_speed(self.x_vel, self.z_vel, theta)
        # Reset air-move state on touchdown.
        self.flying = False
        self.flight_timer = 0
        self.flight_tired = False
        self.gliding = False
        self.hovering = False
        self.homing_active = False
        self.dropdash_charging = False
        self.dropdash_ready = False
        self.dropdash_charge_frames = 0
        # Drop Dash lands already rolling; everything else stands up.
        self.rolling = launched_dropdash
        self._apply_floor_surface(surface)

    def _landing_speed(self, xv: float, zv: float, theta: float) -> float:
        """Convert airborne velocity into ground speed on landing, following the
        Sonic Physics Guide's three angle bands (flat / slope / steep)."""
        a = abs(math.degrees(theta))
        if a <= 22.5:
            return xv
        # On steeper ground the vertical component can dominate.
        if a <= 45.0:
            if abs(xv) > abs(zv):
                return xv
            return zv * math.copysign(0.5, math.sin(theta))
        # steep
        if abs(xv) > abs(zv):
            return xv
        return zv * math.copysign(1.0, math.sin(theta))

    # ------------------------------------------------------------- SPINDASH
    def _update_spindash(self, inp: Inputs):
        # Released (stopped ducking) -> launch.
        if not inp.down:
            speed = self.spindash_base_speed + math.floor(self.spindash_revs) / 2.0
            self.ground_speed = speed * (self.facing if self.facing != 0 else 1)
            self.spindash_active = False
            self.spindash_revs = 0.0
            self.rolling = True
            self.ducking = False
            return

        self.ducking = True
        # Decay happens *first* (revs -= floor(revs / 0.125) / 256 == floor(revs*8)/256),
        # then a fresh jump press revs up and caps.  This ordering matches the
        # Sonic 2 routine and is what makes a full 8-rev (release speed 12)
        # charge reachable: the final capped press is not decayed on its own frame.
        self.spindash_revs -= math.floor(self.spindash_revs * 8.0) / 256.0
        if self.spindash_revs < 0.0:
            self.spindash_revs = 0.0
        if inp.jump_pressed:
            self.spindash_revs = min(self.spindash_revs + self.spindash_charge, self.spindash_max)

    # ------------------------------------------------------- SUPER PEEL OUT
    def _update_peelout(self, inp: Inputs):
        """Revving a Super Peel Out (Sonic CD).  Charging simply counts frames
        while Up stays held; releasing Up launches at full speed if (and only
        if) the charge completed, otherwise nothing happens."""
        if not inp.up:                                   # released
            if self.peelout_timer >= self.peelout_charge_time:
                self.ground_speed = self.peelout_launch_speed * \
                    (self.facing if self.facing != 0 else 1)
                self.rolling = False    # he RUNS out of it -- no ball, vulnerable
            self.peelout_active = False
            self.peelout_timer = 0
            return
        self.peelout_timer += 1

    # ------------------------------------------------------------- DAMAGE
    def hurt(self, away_sign: int = 0) -> bool:
        """Take a hit (Sonic_Hurt).  ``away_sign`` is the horizontal direction
        of the knockback (+1 flings right, -1 left, 0 == opposite of facing).
        Returns True if damage was actually applied (False while hurt or
        invulnerable).  Rings scatter on every hit; if ``lethal_without_rings``
        is set and the player had none, the hit is fatal instead."""
        if self.is_dead:
            return False
        if self.is_hurt or self.invulnerability_timer > 0:
            return False
        had_rings = self.ring_count > 0
        self.rings_lost = self.ring_count
        self.ring_count = 0
        self.hits_taken += 1
        # Any charge/state is dropped on the spot.
        self.spindash_active = False
        self.spindash_revs = 0.0
        self.peelout_active = False
        self.peelout_timer = 0
        self.flying = False
        self.flight_timer = 0
        self.gliding = False
        self.climbing = False
        self.hovering = False
        self.homing_active = False
        self.dropdash_charging = False
        self.dropdash_ready = False
        self.boosting = False
        self.ducking = False
        self.looking_up = False
        self.skidding = False
        self.pushing = False

        if self.lethal_without_rings and not had_rings:
            self.die("hit")
            return True

        if away_sign == 0:
            away_sign = -self.facing if self.facing != 0 else -1
        self.is_hurt = True
        self.on_ground = False
        self.jumping = False
        self.roll_jump = False
        self.rolling = False
        # Knockback: up and away from the hazard, then hurt-gravity ballistics.
        self.ground_speed = 0.0
        self.x_vel = self.hurt_x_force * away_sign
        self.z_vel = self.hurt_z_force
        return True

    # ---------------------------------------------------- objects / actions
    def die(self, cause: str = "") -> None:
        """Kill the player (drowning, quicksand, a fatal hit...).  Sets a brief
        upward hop then a plain fall via _step_dead(); the host reads is_dead /
        death_cause and stops or restarts the run."""
        if self.is_dead:
            return
        self.is_dead = True
        self.death_cause = cause
        self.on_ground = False
        self.rolling = False
        self.jumping = False
        self.flying = self.gliding = self.climbing = self.hovering = False
        self.spindash_active = self.peelout_active = False
        self.homing_active = self.dropdash_charging = self.boosting = False
        self.ground_speed = 0.0
        self.x_vel = 0.0
        self.z_vel = 7.0            # classic death hop

    def spring(self, power: float, dir_x: float = 0.0, dir_z: float = 1.0) -> None:
        """Launch off a spring.  (dir_x, dir_z) is the launch direction in engine
        space (+x right along the path, +z up); it is normalised here.  A mostly
        vertical spring preserves horizontal momentum; a horizontal one sets it.
        Springs give no variable-height control."""
        mag = math.hypot(dir_x, dir_z) or 1.0
        ux, uz = dir_x / mag, dir_z / mag
        self.on_ground = False
        self.jumping = False
        self.roll_jump = False
        self.dropdash_charging = False
        self.dropdash_ready = False
        if uz > 0.3:
            self.rolling = False
        if abs(ux) > 1e-6:
            self.x_vel = power * ux
            self.ground_speed = power * ux
            self.facing = 1 if ux > 0 else -1
        self.z_vel = power * uz
        self.sprung = True

    def collect_ring(self, n: int = 1) -> int:
        """Pick up rings.  Returns the new total."""
        self.ring_count += int(n)
        return self.ring_count

    def attack_active(self) -> bool:
        """True when the player would destroy a badnik on contact (rolling,
        jumping, spin/drop-dashing, homing, gliding, boosting or invulnerable)
        rather than take damage from it."""
        if self.is_invulnerable:
            return True
        if self.rolling or self.spindash_active or self.dropdash_charging:
            return True
        if (not self.on_ground) and (self.jumping or self.roll_jump
                                     or self.homing_active or self.gliding):
            return True
        return bool(self.boosting)

    def bounce_enemy(self) -> None:
        """Rebound after destroying a badnik (a bop / homing hit)."""
        self.on_ground = False
        self.homing_active = False
        self.jumping = True
        self.rolling = True
        if self.z_vel < self.enemy_bounce_speed:
            self.z_vel = self.enemy_bounce_speed

    def start_climb(self, wall_dir: int = 0) -> None:
        """Begin clinging to a wall (the host calls this when a gliding player
        contacts one).  wall_dir: +1 wall to the right, -1 to the left."""
        self.climbing = True
        self.gliding = False
        self.flying = False
        self.on_ground = False
        self.climb_wall_dir = wall_dir
        if wall_dir:
            self.facing = wall_dir
        self.x_vel = 0.0
        self.z_vel = 0.0

    def set_underwater(self, flag: bool) -> None:
        """Enter or leave water.  Entering splashes (halves speeds); leaving
        refills the air supply.  The host calls this each frame from volume
        overlap; the underwater physics themselves lag entry/exit by one frame."""
        flag = bool(flag)
        if flag and not self.underwater:
            self.x_vel *= 0.5
            self.ground_speed *= 0.5
            if self.z_vel < 0.0:
                self.z_vel *= 0.5
        elif not flag and self.underwater:
            self.air_timer = self.air_time
        self.underwater = flag

    def apply_quicksand(self, inp: Inputs) -> None:
        """Sink slowly unless the jump button is mashed (each press pops you up).
        The host calls this each frame the player overlaps a quicksand volume,
        *after* step(), and triggers die('quicksand') if the player sinks out of
        the bottom of the volume."""
        self.in_quicksand = True
        self.on_ground = False
        self.jumping = False
        self.rolling = False
        self.gliding = self.flying = self.climbing = self.hovering = False
        self.homing_active = False
        self.x_vel *= self.quicksand_drag
        if inp.jump_pressed:
            self.z += self.quicksand_climb
            self.z_vel = 0.0
        else:
            self.z -= self.quicksand_sink
            self.z_vel = 0.0

    # ------------------------------------------------------------- BOOSTERS
    def apply_boost(self, sign: int = 0, power: float | None = None) -> bool:
        """A speed booster pad.  Sets speed to +/-power in the pad's direction
        (0 == the player's facing direction) if the player is currently slower
        in that direction -- classic pads *set* speed, they never stack.
        Grounded pads act on ground speed; a pad touched mid-air acts on x
        velocity.  Returns True if the boost changed the player's speed."""
        if power is None:
            power = BOOST_DEFAULT_POWER
        power = abs(float(power))
        s = sign if sign in (-1, 1) else (self.facing if self.facing != 0 else 1)
        fired = False
        if self.on_ground:
            if s > 0 and self.ground_speed < power:
                self.ground_speed = power
                fired = True
            elif s < 0 and self.ground_speed > -power:
                self.ground_speed = -power
                fired = True
        else:
            if s > 0 and self.x_vel < power:
                self.x_vel = power
                fired = True
            elif s < 0 and self.x_vel > -power:
                self.x_vel = -power
                fired = True
        if fired:
            self.facing = s          # boosters point you the way they fling you
            self.boosted = True
        return fired

    # ------------------------------------------------------------- ATTRIBUTES
    def _update_descriptive(self, inp: Inputs):
        # Braking == actively skidding to a stop.
        self.braking = self.skidding

    # world-space helpers ----------------------------------------------------
    @property
    def in_air(self) -> bool:
        return not self.on_ground

    @property
    def airstate_jump(self) -> bool:
        return (not self.on_ground) and self.z_vel > 0.0

    @property
    def airstate_falling(self) -> bool:
        return (not self.on_ground) and self.z_vel <= 0.0

    @property
    def abs_ground_speed(self) -> float:
        return abs(self.ground_speed)

    @property
    def is_running(self) -> bool:
        return self.on_ground and not self.rolling and abs(self.ground_speed) >= self.run_speed_threshold

    @property
    def is_jogging(self) -> bool:
        return (self.on_ground and not self.rolling
                and 0.0 < abs(self.ground_speed) < self.run_speed_threshold)

    @property
    def is_dashing(self) -> bool:
        return abs(self.ground_speed) >= self.dash_speed_threshold

    @property
    def is_invulnerable(self) -> bool:
        return self.is_hurt or self.invulnerability_timer > 0

    @property
    def peelout_ready(self) -> bool:
        return self.peelout_active and self.peelout_timer >= self.peelout_charge_time

    def snapshot(self, inp: Inputs) -> dict:
        """Return the full, ordered attribute dictionary written onto the empty
        every frame.  Names match the brief (plus a good many extras)."""
        return {
            # -- input ---------------------------------------------------------
            "Is_Holding_Left": bool(inp.left),
            "Is_Holding_Right": bool(inp.right),
            "Is_Holding_Up": bool(inp.up),
            "Is_Holding_Down": bool(inp.down),
            "Button_A": bool(inp.a),
            "Button_B": bool(inp.b),
            "Button_C": bool(inp.c),
            "Button_X": bool(inp.x),
            "Button_Y": bool(inp.y),
            "Button_Z": bool(inp.z),
            "Button_Start": bool(inp.start),
            # -- gross state ---------------------------------------------------
            "On_Ground": bool(self.on_ground),
            "In_Air": bool(self.in_air),
            "Airstate_Jump": bool(self.airstate_jump),
            "Airstate_Falling": bool(self.airstate_falling),
            "Is_Jumping": bool(self.jumping),
            "Is_Rolling": bool(self.rolling),
            "Is_RollJumping": bool(self.roll_jump and not self.on_ground),
            "Is_Running": bool(self.is_running),
            "Is_Jogging": bool(self.is_jogging),
            "Is_Dashing": bool(self.is_dashing),
            "Is_Skidding": bool(self.skidding),
            "Is_Braking": bool(self.braking),
            "Is_Ducking": bool(self.ducking),
            "Is_LookingUp": bool(self.looking_up),
            "Is_Pushing": bool(self.pushing),
            "Is_Spindashing": bool(self.spindash_active),
            "Spindash_Revs": float(self.spindash_revs),
            # -- Super Peel Out ------------------------------------------------
            "Is_Peelout_Charging": bool(self.peelout_active),
            "Peelout_Charge_Frames": int(self.peelout_timer),
            "Peelout_Ready": bool(self.peelout_ready),
            # -- damage --------------------------------------------------------
            "Is_Hurt": bool(self.is_hurt),
            "Is_Invulnerable": bool(self.is_invulnerable),
            "Invulnerability_Timer": int(self.invulnerability_timer),
            "Hits_Taken": int(self.hits_taken),
            # -- boosters ------------------------------------------------------
            "Is_Boosted": bool(self.boosted),
            # -- control -------------------------------------------------------
            "Control_Locked": bool(self.control_lock_timer > 0),
            "Control_Lock_Timer": int(self.control_lock_timer),
            "Facing_Right": bool(self.facing >= 0),
            "Facing": int(self.facing),
            # -- velocities (pixels/frame, Genesis-authentic magnitudes) -------
            #    X == horizontal (Blender X), Z == vertical (Blender Z),
            #    Y == depth (Blender Y; the Blender layer fills this in when the
            #    player follows a curve's 3D depth -- 0 in the pure 2D core).
            "X_Vel": float(self.x_vel),
            "X_Vel_Absolute": float(abs(self.x_vel)),
            "Y_Vel": 0.0,
            "Y_Vel_Absolute": 0.0,
            "Z_Vel": float(self.z_vel),
            "Z_Vel_Absolute": float(abs(self.z_vel)),
            "Ground_Speed": float(self.ground_speed),
            "Ground_Speed_Absolute": float(abs(self.ground_speed)),
            "Ground_Angle": float(math.degrees(self.ground_angle)),
            # -- rings / water / quicksand / death -----------------------------
            "Ring_Count": int(self.ring_count),
            "Rings_Lost": int(self.rings_lost),
            "Is_Underwater": bool(self.underwater),
            "Air_Timer": int(self.air_timer),
            "In_Quicksand": bool(self.in_quicksand),
            "Is_On_Ice": bool(self.on_ice),
            "Is_Dead": bool(self.is_dead),
            "Is_Sprung": bool(self.sprung),
            # -- character moves ----------------------------------------------
            "Is_Flying": bool(self.flying),
            "Flight_Timer": int(self.flight_timer),
            "Is_Gliding": bool(self.gliding),
            "Is_Climbing": bool(self.climbing),
            "Is_DropDash_Charging": bool(self.dropdash_charging),
            "DropDash_Ready": bool(self.dropdash_ready),
            "Is_Homing": bool(self.homing_active),
            "Is_Boosting": bool(self.boosting),
            "Boost_Energy": float(self.boost_energy),
            "Is_Hovering": bool(self.hovering),
        }


# =============================================================================
#  helpers
# =============================================================================
def _asr(value: float, shift: int) -> float:
    """Emulate the 68000 ``asr`` (arithmetic shift right) air-drag term on a
    float, i.e. divide by 2**shift while truncating *towards negative
    infinity*.  For air drag the game shifts the raw 8.8 value; on our float
    domain the important property is that a tiny x speed drags to zero and the
    sign is preserved, which floor division provides."""
    return math.floor(value * 256.0 / (2 ** shift)) / 256.0


# =============================================================================
#  GAME PRESETS
# =============================================================================
# Each preset is {"label", "accurate", "values"}.  ``values`` is a flat dict of
# engine-attribute -> value that the Blender layer copies onto its settings.
#
#   accurate == True  -> the Mega Drive / Genesis (and Mania/Superstars) titles,
#                        whose constants come straight from the disassembly /
#                        Sonic Physics Guide.
#   accurate == False -> APPROXIMATIONS.  The 8-bit, Advance, Rush, Colors, 4,
#                        Generations and Forces engines are not documented at the
#                        subpixel level the way the Genesis games are; these
#                        presets are hand-tuned to feel roughly right and enable
#                        the abilities each game is known for.  Treat them as a
#                        starting point, not gospel.

_GENESIS_PHYS = dict(
    acceleration=ACCELERATION, deceleration=DECELERATION, friction=FRICTION,
    top_speed=TOP_SPEED, air_acceleration=AIR_ACCELERATION, gravity=GRAVITY,
    jump_force=JUMP_FORCE,
    slope_factor_walk=SLOPE_FACTOR_WALK,
    slope_factor_roll_up=SLOPE_FACTOR_ROLL_UP,
    slope_factor_roll_down=SLOPE_FACTOR_ROLL_DOWN,
    roll_friction=ROLL_FRICTION, roll_deceleration=ROLL_DECELERATION,
)

# 8-bit (Master System / Game Gear): no slope momentum, punchier ramp.
_EIGHTBIT_PHYS = dict(
    _GENESIS_PHYS, acceleration=0.09375, friction=0.09375,
    slope_factor_walk=0.0, slope_factor_roll_up=0.0,
)
# Dimps handhelds (Advance): close to Genesis, a touch faster on the ramp.
_ADVANCE_PHYS = dict(_GENESIS_PHYS, acceleration=0.09375)
# Boost handhelds (Rush / Colors DS): faster, floatier jump.
_BOOST_PHYS = dict(_GENESIS_PHYS, acceleration=0.09375, top_speed=8.0, jump_force=7.0)
# Modern boost (Generations / Forces): fast, big jump.
_MODERN_PHYS = dict(_GENESIS_PHYS, acceleration=0.09375, top_speed=9.0, jump_force=7.5)

_NO_MOVES = dict(
    enable_spindash=False, enable_peelout=False, enable_flight=False,
    enable_gliding=False, enable_climbing=False, enable_dropdash=False,
    enable_homing=False, enable_boost=False, enable_hovering=False,
)


def _pset(label, accurate, phys=None, **moves):
    values = dict(_GENESIS_PHYS)
    if phys:
        values.update(phys)
    values.update(_NO_MOVES)
    values.update(moves)
    return {"label": label, "accurate": accurate, "values": values}


# Ordered exactly as requested.
GAME_PRESETS = {
    "sonic1":     _pset("Sonic 1", True, enable_spindash=False),
    "sonic1gg":   _pset("Sonic 1 (Game Gear)", False, phys=_EIGHTBIT_PHYS,
                        enable_spindash=False),
    "soniccd":    _pset("Sonic CD", True, enable_spindash=False, enable_peelout=True),
    "sonic2":     _pset("Sonic 2", True, enable_spindash=True),
    "sonic2gg":   _pset("Sonic 2 (Game Gear)", False, phys=_EIGHTBIT_PHYS,
                        enable_spindash=False),
    "sonic3":     _pset("Sonic 3", True, enable_spindash=True),
    "sonicblast": _pset("Sonic Blast", False, phys=_EIGHTBIT_PHYS,
                        enable_spindash=True),
    "advance1":   _pset("Sonic Advance", False, phys=_ADVANCE_PHYS,
                        enable_spindash=True),
    "advance2":   _pset("Sonic Advance 2", False,
                        phys=dict(_ADVANCE_PHYS, top_speed=8.0),
                        enable_spindash=True, enable_boost=True),
    "advance3":   _pset("Sonic Advance 3", False, phys=_ADVANCE_PHYS,
                        enable_spindash=True),
    "rush":       _pset("Sonic Rush", False, phys=_BOOST_PHYS,
                        enable_boost=True, enable_homing=True),
    "rushadv":    _pset("Sonic Rush Adventure", False, phys=_BOOST_PHYS,
                        enable_boost=True, enable_homing=True),
    "colorsds":   _pset("Sonic Colors (DS)", False, phys=_BOOST_PHYS,
                        enable_boost=True, enable_homing=True),
    "sonic4e1":   _pset("Sonic 4: Episode I", False,
                        phys=dict(_GENESIS_PHYS, air_acceleration=0.046875),
                        enable_spindash=True, enable_homing=True),
    "sonic4e2":   _pset("Sonic 4: Episode II", False,
                        enable_spindash=True, enable_homing=True),
    "genconsole": _pset("Sonic Generations (Console)", False, phys=_MODERN_PHYS,
                        enable_boost=True, enable_homing=True),
    "gen3ds":     _pset("Sonic Generations (3DS)", False,
                        phys=dict(_MODERN_PHYS, top_speed=8.0),
                        enable_boost=True, enable_homing=True),
    "mania":      _pset("Sonic Mania", True, enable_spindash=True,
                        enable_dropdash=True),
    "forces":     _pset("Sonic Forces", False, phys=_MODERN_PHYS,
                        enable_boost=True, enable_homing=True),
    "superstars": _pset("Sonic Superstars", True, enable_spindash=True,
                        enable_dropdash=True),
}
