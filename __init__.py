# SPDX-License-Identifier: GPL-3.0-or-later OR MIT
"""
Sonic Physics — a Blender add-on that recreates classic Genesis-era Sonic the
Hedgehog physics on a controllable empty.

    * N-panel button spawns a "cube empty" player whose origin is at the bottom
      (the feet), so world Z==0 is the floor.
    * A modal "Simulate" mode turns the keyboard into a 6-button SEGA pad and
      drives the empty with fully accurate Sonic physics (see sonic_core.py).
    * Every frame the empty receives a large set of descriptive custom
      attributes (Is_Holding_Left, On_Ground, Airstate_Jump, Spindash_Revs,
      Is_Peelout_Charging, Is_Hurt, X_Vel, ...).
    * Optional per-frame baking records the run as keyframes for playback.
    * Optional curve-as-ground: a curve object becomes the floor profile; the
      character follows it while grounded but jumps ballistically (the curve
      never bends gravity).  The player can also follow the curve's 3D DEPTH
      (its bends in plan view), optionally yawing to face along the path.
    * Optional mesh collision: every mesh in a chosen collection becomes
      shape-accurate solid geometry (BVH ray casts against the evaluated,
      modifier-applied triangles).  Each object carries a surface type —
      Walkable (default), Damage, Trigger or Speed Up — set in the N-panel.
      Damage and Speed Up may additionally be flagged as passthrough Triggers.
      Animated / rigid-body / simulated meshes are rebuilt live every frame.

All physics constants are taken from the Sonic 1 disassembly and the Sonic
Retro Physics Guide and may be overridden from the panel.
"""

bl_info = {
    "name": "Sonic Physics",
    "author": "Generated with Claude Code",
    "version": (1, 2, 1),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar (N) > Sonic",
    "description": ("Genesis-accurate Sonic physics on a controllable empty: mesh collision "
                    "with surface types, Super Peel Out, curve-following with 3D depth."),
    "warning": "Simulate mode captures ALL keyboard/mouse input until you press Esc.",
    "category": "Animation",
}

import math
import os

import bpy
from bpy.props import (
    BoolProperty,
    EnumProperty,
    FloatProperty,
    FloatVectorProperty,
    IntProperty,
    PointerProperty,
    StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup
from mathutils import Euler, Vector

from . import sonic_core
from .sonic_core import (
    Inputs,
    SonicEngine,
    FlatTerrain,
    HeightfieldTerrain,
    Surface,
    FloorHit,
    WallHit,
    CeilingHit,
    CollisionWorld,
    SURFACE_WALKABLE,
    SURFACE_DAMAGE,
    SURFACE_SPEEDUP,
    WALKABLE_SURFACE,
)

# Optional GPU overlay (viewport drawing of the collision box + sensors).
try:
    import gpu
    from gpu_extras.batch import batch_for_shader
    _HAS_GPU = True
except Exception:  # pragma: no cover - depends on Blender build
    _HAS_GPU = False

# BVH trees power the mesh collision; guarded so the module still imports in
# stripped-down builds (collision simply disables itself).
try:
    from mathutils.bvhtree import BVHTree
    _HAS_BVH = True
except Exception:  # pragma: no cover - depends on Blender build
    BVHTree = None
    _HAS_BVH = False


# =============================================================================
#  Controller key map  (6-button SEGA Genesis pad)
# =============================================================================
# Blender event.type  ->  logical button
KEYMAP = {
    "LEFT_ARROW": "left",
    "RIGHT_ARROW": "right",
    "UP_ARROW": "up",
    "DOWN_ARROW": "down",
    "A": "a",        # SEGA A
    "S": "b",        # SEGA B
    "D": "c",        # SEGA C
    "Q": "x",        # SEGA X
    "W": "y",        # SEGA Y
    "E": "z",        # SEGA Z
    "RET": "start",  # Start
    "NUMPAD_ENTER": "start",
}
JUMP_BUTTONS = ("a", "b", "c")


# =============================================================================
#  Settings
# =============================================================================
def _curve_poll(self, obj):
    return obj is not None and obj.type == "CURVE"


def _player_poll(self, obj):
    return obj is not None and obj.type in {"EMPTY", "MESH"}


class SonicCollisionSettings(PropertyGroup):
    """Per-object collision behaviour (lives on every Object as
    ``obj.sonic_collision``; only meshes inside the chosen collision
    collection are actually used)."""

    surface_type: EnumProperty(
        name="Surface Type",
        description="What this piece of collision does to the player",
        items=[
            ("WALKABLE", "Walkable", "Plain solid ground/wall/ceiling (the default)"),
            ("DAMAGE", "Damage", "Hurts the player on contact (spikes, hazards)"),
            ("TRIGGER", "Trigger", "Passthrough volume that reports when the player is inside "
                                   "(reads back as custom properties on this object)"),
            ("SPEED_UP", "Speed Up", "A booster: sets the player's speed in a direction"),
            ("ICE", "Ice", "Solid floor with greatly reduced friction (you slide)"),
            ("WATER", "Water", "Passthrough volume: underwater physics (floaty, slower) "
                               "plus an air timer — stay under too long and you drown"),
            ("QUICKSAND", "Quicksand", "Passthrough volume: you sink slowly unless the jump "
                                       "button is mashed. Sink out the bottom and you're gone"),
        ],
        default="WALKABLE",
    )
    trigger_paired: BoolProperty(
        name="Passthrough Trigger",
        description=("Pair this surface with the Trigger type: the mesh stops being solid and "
                     "instead applies its effect (damage / boost) while the player is inside it"),
        default=False,
    )
    trigger_toggle: BoolProperty(
        name="Toggle (Stay Active)",
        description=("Enabled: once the player has entered, the trigger stays active even after "
                     "they exit (until the next simulation starts). Disabled: the trigger is "
                     "active only while the player is inside it"),
        default=False,
    )
    boost_mode: EnumProperty(
        name="Boost Direction",
        description="Which way the booster flings the player",
        items=[
            ("FACING", "Facing Direction", "Boost the way the player is currently facing"),
            ("LEFT", "Left (-X / backward along the path)", "Always boost toward -X"),
            ("RIGHT", "Right (+X / forward along the path)", "Always boost toward +X"),
        ],
        default="FACING",
    )
    boost_power: FloatProperty(
        name="Boost Power",
        description=("Ground speed the booster sets (pixels/frame). Classic Chemical Plant "
                     "boosters use 16. Boosters SET speed — if the player is already faster "
                     "in the boost direction nothing happens"),
        default=sonic_core.BOOST_DEFAULT_POWER, min=0.0, max=64.0, precision=3,
    )
    dynamic: EnumProperty(
        name="Rebuild",
        description="When the collision shape of this object is rebuilt",
        items=[
            ("AUTO", "Auto Detect", "Rebuild every frame if the object looks animated "
                                    "(keyframes/drivers, rigid body, constraints, a parent, or "
                                    "cloth/soft-body/armature-style modifiers)"),
            ("STATIC", "Static", "Build once when the simulation starts (fastest)"),
            ("DYNAMIC", "Every Frame", "Force a rebuild every simulation frame (moving platforms, "
                                       "simulations, anything Auto misses)"),
        ],
        default="AUTO",
    )

    # -- derived helpers ------------------------------------------------------
    @property
    def is_trigger_volume(self) -> bool:
        # Water and quicksand are always passthrough volumes; damage/boost can be
        # made passthrough by pairing them with a trigger.
        if self.surface_type in {"TRIGGER", "WATER", "QUICKSAND"}:
            return True
        return self.trigger_paired and self.surface_type in {"DAMAGE", "SPEED_UP"}

    @property
    def is_solid(self) -> bool:
        return not self.is_trigger_volume


class SonicObjectSettings(PropertyGroup):
    """Marks an object as a gameplay object the simulation reacts to
    (``obj.sonic_object``)."""
    kind: EnumProperty(
        name="Sonic Object",
        description="What this object does during the simulation",
        items=[
            ("NONE", "None", "Not a gameplay object"),
            ("SPRING", "Spring", "Launches the player when touched"),
            ("RING", "Ring", "Collected on contact (adds to the ring count, then hides)"),
            ("MOTOBUG", "Motobug", "A badnik: destroyed if you hit it while attacking "
                                   "(rolling/jumping/homing/boosting), otherwise it hurts you"),
            ("SPIKES", "Spikes", "A hazard that hurts on contact — even while rolling or "
                                 "jumping (only invincibility protects you)"),
            ("BUMPER", "Bumper", "Bounces the player away from its centre (a pinball bumper)"),
            ("MONITOR", "Monitor", "An item box: break it while attacking to collect its rings"),
        ],
        default="NONE",
    )
    spring_power: FloatProperty(
        name="Spring Power",
        description="Launch speed (pixels/frame). Yellow spring == 10, red == 16",
        default=sonic_core.SPRING_POWER_RED, min=0.0, max=64.0, precision=2,
    )
    spring_dir: EnumProperty(
        name="Spring Direction",
        description="Which way the spring throws the player (in path space: up, or "
                    "forward/back along the path)",
        items=[
            ("UP", "Up", "Straight up"),
            ("UP_RIGHT", "Up + Forward", "Diagonally up and forward (+X)"),
            ("UP_LEFT", "Up + Back", "Diagonally up and backward (-X)"),
            ("RIGHT", "Forward", "Horizontally forward (+X)"),
            ("LEFT", "Back", "Horizontally backward (-X)"),
            ("DOWN", "Down", "Straight down"),
        ],
        default="UP",
    )
    ring_value: IntProperty(
        name="Ring Value", description="How many rings this pickup is worth",
        default=1, min=1, max=999,
    )
    interact_radius: FloatProperty(
        name="Touch Radius (px)",
        description="How close (in pixels) the player must get to trigger this object",
        default=22.0, min=1.0, max=512.0, precision=1,
    )


SPRING_DIR_VECTORS = {
    "UP": (0.0, 1.0),
    "DOWN": (0.0, -1.0),
    "LEFT": (-1.0, 0.0),
    "RIGHT": (1.0, 0.0),
    "UP_LEFT": (-0.70710678, 0.70710678),
    "UP_RIGHT": (0.70710678, 0.70710678),
}


def _new_mesh_object(context, name, mesh):
    obj = bpy.data.objects.new(name, mesh)
    try:
        obj.location = context.scene.cursor.location.copy()
    except Exception:
        pass
    coll = context.collection if context.collection is not None else context.scene.collection
    coll.objects.link(obj)
    for o in context.selected_objects:
        o.select_set(False)
    obj.select_set(True)
    context.view_layer.objects.active = obj
    return obj


def _bm_to_mesh(bm, name):
    import bmesh
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    return me


def _spring_mesh(name, us):
    """A short, wide cylinder (a coil-spring puck)."""
    import bmesh
    r = 9.0 * us
    h = 8.0 * us
    bm = bmesh.new()
    try:
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=16,
                              radius1=r, radius2=r, depth=h)
    except TypeError:
        bmesh.ops.create_cone(bm, cap_ends=True, segments=16,
                              radius1=r, radius2=r, depth=h)
    # origin at the top face (where you get bounced from)
    for v in bm.verts:
        v.co.z += h * 0.5
    return _bm_to_mesh(bm, name)


def _ring_mesh(name, us):
    """A torus (the classic ring)."""
    import bmesh
    from math import sin, cos, pi
    R = 8.0 * us      # major radius
    r = 2.0 * us      # tube radius
    major, minor = 20, 10
    bm = bmesh.new()
    rings = []
    for i in range(major):
        a = 2.0 * pi * i / major
        ca, sa = cos(a), sin(a)
        loop = []
        for j in range(minor):
            b = 2.0 * pi * j / minor
            rr = R + r * cos(b)
            loop.append(bm.verts.new((rr * ca, rr * sa, r * sin(b))))
        rings.append(loop)
    for i in range(major):
        a2 = rings[i]
        b2 = rings[(i + 1) % major]
        for j in range(minor):
            j2 = (j + 1) % minor
            bm.faces.new((a2[j], a2[j2], b2[j2], b2[j]))
    return _bm_to_mesh(bm, name)


def _motobug_mesh(name, us):
    """A squashed sphere body — a simple badnik stand-in."""
    import bmesh
    bm = bmesh.new()
    try:
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=10, radius=10.0 * us)
    except TypeError:
        bmesh.ops.create_uvsphere(bm, u_segments=16, v_segments=10, diameter=20.0 * us)
    for v in bm.verts:
        v.co.z *= 0.7
        v.co.x *= 1.2
    for v in bm.verts:                     # sit it on the ground
        v.co.z += 7.0 * us
    return _bm_to_mesh(bm, name)


def _spikes_mesh(name, us):
    """A short row of three up-pointing spikes on a thin base."""
    import bmesh
    bm = bmesh.new()
    for off in (-6.0, 0.0, 6.0):
        cone = bmesh.new()
        try:
            bmesh.ops.create_cone(cone, cap_ends=True, cap_tris=False, segments=8,
                                  radius1=3.0 * us, radius2=0.0, depth=10.0 * us)
        except TypeError:
            bmesh.ops.create_cone(cone, cap_ends=True, segments=8,
                                  radius1=3.0 * us, radius2=0.0, depth=10.0 * us)
        for v in cone.verts:
            v.co.x += off * us
            v.co.z += 5.0 * us
        me = bpy.data.meshes.new("_tmp")
        cone.to_mesh(me)
        cone.free()
        bm.from_mesh(me)
        bpy.data.meshes.remove(me)
    return _bm_to_mesh(bm, name)


def _bumper_mesh(name, us):
    """A round pinball bumper."""
    import bmesh
    bm = bmesh.new()
    try:
        bmesh.ops.create_uvsphere(bm, u_segments=18, v_segments=12, radius=9.0 * us)
    except TypeError:
        bmesh.ops.create_uvsphere(bm, u_segments=18, v_segments=12, diameter=18.0 * us)
    for v in bm.verts:
        v.co.z += 9.0 * us
    return _bm_to_mesh(bm, name)


def _monitor_mesh(name, us):
    """A boxy item-box / TV monitor sitting on the ground."""
    import bmesh
    bm = bmesh.new()
    try:
        bmesh.ops.create_cube(bm, size=1.0)
    except TypeError:
        bmesh.ops.create_cube(bm)
    for v in bm.verts:
        v.co.x *= 15.0 * us
        v.co.y *= 12.0 * us
        v.co.z *= 15.0 * us
        v.co.z += 7.5 * us            # origin at the base
    return _bm_to_mesh(bm, name)


class SONIC_OT_add_spring(Operator):
    bl_idname = "sonic.add_spring"
    bl_label = "Spring"
    bl_description = "Add a spring that launches the player on contact"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        us = context.scene.sonic_sim.unit_scale
        obj = _new_mesh_object(context, "Sonic Spring", _spring_mesh("Sonic Spring", us))
        obj.sonic_object.kind = "SPRING"
        obj.sonic_object.spring_power = sonic_core.SPRING_POWER_RED
        obj.sonic_object.interact_radius = 20.0
        obj.color = (1.0, 0.15, 0.15, 1.0)
        return {"FINISHED"}


class SONIC_OT_add_ring(Operator):
    bl_idname = "sonic.add_ring"
    bl_label = "Ring"
    bl_description = "Add a collectible ring"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        us = context.scene.sonic_sim.unit_scale
        obj = _new_mesh_object(context, "Sonic Ring", _ring_mesh("Sonic Ring", us))
        obj.sonic_object.kind = "RING"
        obj.sonic_object.interact_radius = 16.0
        obj.rotation_euler[0] = math.radians(90.0)   # stand it upright, facing the camera
        obj.color = (1.0, 0.85, 0.1, 1.0)
        return {"FINISHED"}


class SONIC_OT_add_motobug(Operator):
    bl_idname = "sonic.add_motobug"
    bl_label = "Motobug"
    bl_description = "Add a Motobug badnik (destroy it while attacking, or it hurts you)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        us = context.scene.sonic_sim.unit_scale
        obj = _new_mesh_object(context, "Sonic Motobug", _motobug_mesh("Sonic Motobug", us))
        obj.sonic_object.kind = "MOTOBUG"
        obj.sonic_object.interact_radius = 22.0
        obj.color = (0.2, 0.3, 0.8, 1.0)
        return {"FINISHED"}


class SONIC_OT_add_spikes(Operator):
    bl_idname = "sonic.add_spikes"
    bl_label = "Spikes"
    bl_description = "Add spikes that hurt on contact, even while rolling or jumping"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        us = context.scene.sonic_sim.unit_scale
        obj = _new_mesh_object(context, "Sonic Spikes", _spikes_mesh("Sonic Spikes", us))
        obj.sonic_object.kind = "SPIKES"
        obj.sonic_object.interact_radius = 16.0
        obj.color = (0.75, 0.75, 0.8, 1.0)
        return {"FINISHED"}


class SONIC_OT_add_bumper(Operator):
    bl_idname = "sonic.add_bumper"
    bl_label = "Bumper"
    bl_description = "Add a pinball bumper that bounces the player away from its centre"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        us = context.scene.sonic_sim.unit_scale
        obj = _new_mesh_object(context, "Sonic Bumper", _bumper_mesh("Sonic Bumper", us))
        obj.sonic_object.kind = "BUMPER"
        obj.sonic_object.spring_power = 10.0
        obj.sonic_object.interact_radius = 18.0
        obj.color = (0.9, 0.2, 0.5, 1.0)
        return {"FINISHED"}


class SONIC_OT_add_monitor(Operator):
    bl_idname = "sonic.add_monitor"
    bl_label = "Monitor"
    bl_description = "Add an item-box monitor — break it while attacking to collect its rings"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        us = context.scene.sonic_sim.unit_scale
        obj = _new_mesh_object(context, "Sonic Monitor", _monitor_mesh("Sonic Monitor", us))
        obj.sonic_object.kind = "MONITOR"
        obj.sonic_object.ring_value = 10
        obj.sonic_object.interact_radius = 18.0
        obj.color = (0.2, 0.7, 0.9, 1.0)
        return {"FINISHED"}


class SONIC_MT_add(bpy.types.Menu):
    bl_idname = "SONIC_MT_add"
    bl_label = "Sonic Phys"

    def draw(self, context):
        layout = self.layout
        layout.operator("sonic.add_spring", text="Spring", icon="FORCE_HARMONIC")
        layout.operator("sonic.add_ring", text="Ring", icon="MESH_TORUS")
        layout.operator("sonic.add_monitor", text="Monitor", icon="MESH_CUBE")
        layout.separator()
        layout.operator("sonic.add_motobug", text="Motobug", icon="GHOST_ENABLED")
        layout.operator("sonic.add_spikes", text="Spikes", icon="MOD_TRIANGULATE")
        layout.operator("sonic.add_bumper", text="Bumper", icon="ANTIALIASED")


def _sonic_add_menu(self, context):
    self.layout.menu("SONIC_MT_add", icon="MOD_PHYSICS")


def _build_preset_items():
    items = [("CUSTOM", "Custom / Manual", "Your own values — not a game preset")]
    for key, p in sonic_core.GAME_PRESETS.items():
        label = p["label"] + ("" if p["accurate"] else "  (approx.)")
        tip = ("Authentic Mega Drive/Genesis-era constants"
               if p["accurate"] else
               "Hand-tuned approximation of a non-Genesis engine")
        items.append((key, label, tip))
    return items


# Built once and kept referenced (Blender can corrupt enums whose item strings
# are generated per-call and garbage-collected).
_PRESET_ITEMS = _build_preset_items()


def _preset_update(self, context):
    """Applying a game preset when it's chosen from the dropdown."""
    key = self.preset
    if key and key != "CUSTOM":
        apply_preset_to_settings(self, key)


class SonicSimSettings(PropertyGroup):
    # ---- objects ------------------------------------------------------------
    player: PointerProperty(
        name="Player",
        description="The empty that is driven by the simulation",
        type=bpy.types.Object,
        poll=_player_poll,
    )
    player_type: EnumProperty(
        name="Player Object",
        description="What kind of object the 'Add Player' button creates",
        items=[
            ("EMPTY", "Cube Empty", "A cube-display empty (origin at the feet)"),
            ("MESH", "Wire Mesh Cube", "A real wireframe cube mesh with its origin at the bottom face"),
        ],
        default="EMPTY",
    )

    # ---- world --------------------------------------------------------------
    unit_scale: FloatProperty(
        name="Blender Units / Pixel",
        description="Maps Sonic 'pixels' onto Blender units. Physics feel is unaffected; this only scales size/position",
        default=0.05, min=0.0001, max=100.0, precision=4,
    )
    use_curve_ground: BoolProperty(
        name="Use Curve As Ground",
        description="Sample a curve as the floor profile instead of a flat plane at Z=0",
        default=False,
    )
    ground_curve: PointerProperty(
        name="Ground Curve",
        description="Curve whose profile is used as the floor",
        type=bpy.types.Object,
        poll=_curve_poll,
    )
    curve_follow_depth: BoolProperty(
        name="Follow Curve Depth (3D)",
        description=("Follow the curve through 3D space: the player's Y (depth) tracks the curve's "
                     "bends in plan view while the 2D physics run along the path's horizontal arc "
                     "length. Disable to flatten the curve onto the X/Z plane (the old behaviour)"),
        default=True,
    )
    curve_follow_rotation: BoolProperty(
        name="Rotate Along Path (Yaw)",
        description="Turn the player to face along the path's horizontal direction while following depth",
        default=True,
    )

    # ---- mesh collision -----------------------------------------------------
    use_mesh_collision: BoolProperty(
        name="Mesh Collision",
        description=("Collide with every mesh inside the chosen collection, accurate to the "
                     "evaluated (modifier-applied) triangles"),
        default=False,
    )
    collision_collection: PointerProperty(
        name="Collision Collection",
        description="Only meshes inside this collection collide with the player",
        type=bpy.types.Collection,
    )
    poly_warn_threshold: IntProperty(
        name="Poly Warning Threshold",
        description=("Warn in the panel when a collision mesh has more triangles than this. "
                     "High-poly colliders slow the simulation — especially dynamic ones, which are "
                     "rebuilt every frame"),
        default=5000, min=100, max=10000000,
    )
    sync_timeline: BoolProperty(
        name="Advance Timeline (Live Objects)",
        description=("Step the scene frame forward while simulating so animated / rigid-body / "
                     "simulated collision objects actually move during play. The frame is restored "
                     "when the simulation ends"),
        default=True,
    )
    collision_warning_text: StringProperty(default="")

    # ---- simulation / baking -----------------------------------------------
    bake_animation: BoolProperty(
        name="Bake Animation",
        description="Record the run as keyframes on the player for timeline playback",
        default=True,
    )
    bake_attributes: BoolProperty(
        name="Bake Attributes",
        description="Also keyframe every state attribute (needed if drivers/geometry-nodes read them during playback)",
        default=True,
    )
    set_scene_fps: BoolProperty(
        name="Force Scene to 60 FPS",
        description="Set the scene frame-rate to 60 when baking so playback matches Sonic's timing",
        default=True,
    )
    fps: IntProperty(
        name="Simulation FPS",
        description="Physics tick rate. Sonic runs at 60",
        default=60, min=1, max=240,
    )
    draw_overlay: BoolProperty(
        name="Draw Collision Overlay",
        description="Draw the true collision box (origin at the bottom) and the ground/wall sensors while simulating",
        default=True,
    )

    is_simulating: BoolProperty(default=False)
    status_text: StringProperty(default="")
    sim_start_location: FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))
    sim_start_rotation: FloatVectorProperty(size=3, default=(0.0, 0.0, 0.0))

    # ---- tunable physics constants (defaults == Sonic 1) --------------------
    acceleration: FloatProperty(name="Acceleration", default=sonic_core.ACCELERATION, precision=6, min=0.0)
    deceleration: FloatProperty(name="Deceleration", default=sonic_core.DECELERATION, precision=6, min=0.0)
    friction: FloatProperty(name="Friction", default=sonic_core.FRICTION, precision=6, min=0.0)
    top_speed: FloatProperty(name="Top Speed", default=sonic_core.TOP_SPEED, precision=6, min=0.0)
    air_acceleration: FloatProperty(name="Air Acceleration", default=sonic_core.AIR_ACCELERATION, precision=6, min=0.0)
    gravity: FloatProperty(name="Gravity", default=sonic_core.GRAVITY, precision=6, min=0.0)
    jump_force: FloatProperty(name="Jump Force", default=sonic_core.JUMP_FORCE, precision=6, min=0.0)
    jump_release_cap: FloatProperty(name="Jump Release Cap", default=sonic_core.JUMP_RELEASE_CAP, precision=6, min=0.0)
    slope_factor_walk: FloatProperty(name="Slope Factor (Walk)", default=sonic_core.SLOPE_FACTOR_WALK, precision=6, min=0.0)
    slope_factor_roll_up: FloatProperty(name="Slope Factor (Roll Up)", default=sonic_core.SLOPE_FACTOR_ROLL_UP, precision=6, min=0.0)
    slope_factor_roll_down: FloatProperty(name="Slope Factor (Roll Down)", default=sonic_core.SLOPE_FACTOR_ROLL_DOWN, precision=6, min=0.0)
    roll_friction: FloatProperty(name="Roll Friction", default=sonic_core.ROLL_FRICTION, precision=6, min=0.0)
    roll_deceleration: FloatProperty(name="Roll Deceleration", default=sonic_core.ROLL_DECELERATION, precision=6, min=0.0)
    roll_min_speed: FloatProperty(name="Roll Min Speed", default=sonic_core.ROLL_MIN_SPEED, precision=6, min=0.0)
    fall_slip_speed: FloatProperty(name="Slip Speed", default=sonic_core.FALL_SLIP_SPEED, precision=6, min=0.0)
    control_lock_time: IntProperty(name="Control Lock (frames)", default=sonic_core.CONTROL_LOCK_TIME, min=0)
    ground_snap_distance: FloatProperty(
        name="Ground Snap (px)",
        description="How far the floor may drop below the feet before launching off a ramp. Larger == sticks harder",
        default=14.0, min=0.0, precision=3,
    )
    spindash_charge: FloatProperty(name="Spindash Charge/Rev", default=sonic_core.SPINDASH_CHARGE, precision=4, min=0.0)
    spindash_max: FloatProperty(name="Spindash Max Revs", default=sonic_core.SPINDASH_MAX, precision=4, min=0.0)
    spindash_base_speed: FloatProperty(name="Spindash Base Speed", default=sonic_core.SPINDASH_BASE_SPEED, precision=4, min=0.0)
    enable_peelout: BoolProperty(
        name="Enable Super Peel Out",
        description=("Sonic CD's figure-8 dash: hold Up, tap a jump button, keep holding Up to rev, "
                     "release Up to launch at full running speed. While enabled, Up+jump no longer "
                     "performs a plain jump (exactly like Sonic CD)"),
        default=True,
    )
    peelout_charge_time: IntProperty(
        name="Peel Out Charge (frames)",
        description="Frames Up must stay held before the launch is armed (Sonic CD: 30)",
        default=sonic_core.PEELOUT_CHARGE_TIME, min=1, max=600,
    )
    peelout_launch_speed: FloatProperty(
        name="Peel Out Launch Speed",
        description="Ground speed of a fully charged launch (Sonic CD: 12 — a full spindash)",
        default=sonic_core.PEELOUT_LAUNCH_SPEED, precision=4, min=0.0,
    )
    hurt_gravity: FloatProperty(
        name="Hurt Gravity",
        description="Gravity during the hurt knockback arc ($30 == 0.1875; normal gravity is $38)",
        default=sonic_core.HURT_GRAVITY, precision=6, min=0.0,
    )
    invulnerability_time: IntProperty(
        name="Invulnerability (frames)",
        description="Post-hit invulnerability after landing from the hurt knockback ($78 == 120 frames)",
        default=sonic_core.INVULNERABILITY_TIME, min=0, max=3600,
    )

    # ---- character moves (opt-in abilities) ---------------------------------
    enable_spindash: BoolProperty(
        name="Spin Dash",
        description="Down + jump while still charges a spin dash (Sonic 2 onward). "
                    "Turn OFF for Sonic 1 / Sonic CD",
        default=True,
    )
    enable_flight: BoolProperty(
        name="Flight (Tails)",
        description="In the air, tap a jump button to flap and gain height for a limited time",
        default=False,
    )
    enable_gliding: BoolProperty(
        name="Gliding (Knuckles)",
        description="Hold a jump button in the air to glide: a slow forward descent you can steer",
        default=False,
    )
    enable_climbing: BoolProperty(
        name="Climbing (Knuckles)",
        description="Glide into a wall to cling on; Up/Down climb, a jump leaps off. "
                    "Needs Mesh Collision so walls exist",
        default=False,
    )
    enable_dropdash: BoolProperty(
        name="Drop Dash (Mania)",
        description="Hold a jump button after jumping; land to launch into a rolling dash",
        default=False,
    )
    enable_homing: BoolProperty(
        name="Homing Attack",
        description="In the air, press a jump button to dash at the nearest Motobug in range",
        default=False,
    )
    enable_boost: BoolProperty(
        name="Boost",
        description="Hold the X button (Q key) to hold a high speed while a boost meter drains",
        default=False,
    )
    enable_hovering: BoolProperty(
        name="Hovering",
        description="Hold a jump button at the apex to hang in the air for a moment",
        default=False,
    )
    lethal_without_rings: BoolProperty(
        name="Ringless Hit Is Fatal",
        description="Classic rule: taking a hit with zero rings kills the player "
                    "(otherwise a hit only scatters rings and knocks you back)",
        default=False,
    )

    # ---- move tunables (a useful subset; the rest use engine defaults) -------
    boost_speed: FloatProperty(name="Boost Speed", default=sonic_core.BOOST_SPEED, precision=3, min=0.0)
    dropdash_speed: FloatProperty(name="Drop Dash Speed", default=sonic_core.DROPDASH_SPEED, precision=3, min=0.0)
    homing_speed: FloatProperty(name="Homing Speed", default=sonic_core.HOMING_SPEED, precision=3, min=0.0)
    homing_range: FloatProperty(name="Homing Range (px)", default=sonic_core.HOMING_RANGE, precision=1, min=0.0)
    flight_time: IntProperty(name="Flight Time (frames)", default=sonic_core.FLIGHT_TIME, min=0, max=6000)
    glide_speed: FloatProperty(name="Glide Speed", default=sonic_core.GLIDE_SPEED, precision=3, min=0.0)
    glide_fall_speed: FloatProperty(name="Glide Fall Speed", default=sonic_core.GLIDE_FALL_SPEED, precision=3, min=0.0)
    hover_time: IntProperty(name="Hover Time (frames)", default=sonic_core.HOVER_TIME, min=0, max=6000)

    # ---- ice / water / quicksand tunables -----------------------------------
    ice_friction_mult: FloatProperty(
        name="Ice Friction x", description="Friction/deceleration multiplier on ice (lower == more slippery)",
        default=sonic_core.ICE_FRICTION_MULT, min=0.0, max=1.0, precision=3,
    )
    water_accel_mult: FloatProperty(
        name="Water Accel x", default=sonic_core.WATER_ACCEL_MULT, min=0.0, max=2.0, precision=3)
    water_top_mult: FloatProperty(
        name="Water Top Speed x", default=sonic_core.WATER_TOP_MULT, min=0.0, max=2.0, precision=3)
    water_gravity_mult: FloatProperty(
        name="Water Gravity x", default=sonic_core.WATER_GRAVITY_MULT, min=0.0, max=2.0, precision=3)
    water_jump_mult: FloatProperty(
        name="Water Jump x", default=sonic_core.WATER_JUMP_MULT, min=0.0, max=2.0, precision=3)
    air_time: IntProperty(
        name="Air (frames underwater)",
        description="Frames you can stay underwater before drowning (1800 == 30 s @ 60fps)",
        default=sonic_core.AIR_TIME, min=1, max=100000,
    )
    quicksand_sink: FloatProperty(
        name="Quicksand Sink", description="Pixels sunk per frame while idle in quicksand",
        default=sonic_core.QUICKSAND_SINK, min=0.0, max=16.0, precision=3,
    )
    quicksand_climb: FloatProperty(
        name="Quicksand Climb/Mash", description="Pixels risen per jump press while mashing out",
        default=sonic_core.QUICKSAND_CLIMB, min=0.0, max=16.0, precision=3,
    )

    # ---- presets ------------------------------------------------------------
    preset: EnumProperty(
        name="Game Preset",
        description="Load a game's physics + moves. Genesis-era titles are authentic; "
                    "8-bit / Advance / Rush / Colors / 4 / Generations / Forces are approximations",
        items=_PRESET_ITEMS,
        update=_preset_update,
    )

    # ---- TASing -------------------------------------------------------------
    tas_record: BoolProperty(
        name="Record Inputs (TAS)",
        description="While simulating live, keyframe every button per frame onto the player as "
                    "TAS_* channels. Edit those curves, then use 'Play Back TAS' to re-run the "
                    "exact same inputs deterministically",
        default=False,
    )


CONST_FIELDS = (
    "acceleration", "deceleration", "friction", "top_speed", "air_acceleration",
    "gravity", "jump_force", "jump_release_cap", "slope_factor_walk",
    "slope_factor_roll_up", "slope_factor_roll_down", "roll_friction",
    "roll_deceleration", "roll_min_speed", "fall_slip_speed", "control_lock_time",
    "ground_snap_distance", "spindash_charge", "spindash_max", "spindash_base_speed",
    "enable_peelout", "peelout_charge_time", "peelout_launch_speed",
    "hurt_gravity", "invulnerability_time",
    # moves + volumes
    "enable_spindash", "enable_flight", "enable_gliding", "enable_climbing",
    "enable_dropdash", "enable_homing", "enable_boost", "enable_hovering",
    "lethal_without_rings",
    "boost_speed", "dropdash_speed", "homing_speed", "homing_range",
    "flight_time", "glide_speed", "glide_fall_speed", "hover_time",
    "ice_friction_mult", "water_accel_mult", "water_top_mult",
    "water_gravity_mult", "water_jump_mult", "air_time",
    "quicksand_sink", "quicksand_climb",
)

# The settings fields a game preset can write (everything a preset's values dict
# might contain).  Also the exact list saved by the user-preset "Save" button.
PRESET_WRITABLE_FIELDS = (
    "acceleration", "deceleration", "friction", "top_speed", "air_acceleration",
    "gravity", "jump_force", "slope_factor_walk", "slope_factor_roll_up",
    "slope_factor_roll_down", "roll_friction", "roll_deceleration",
    "enable_spindash", "enable_peelout", "enable_flight", "enable_gliding",
    "enable_climbing", "enable_dropdash", "enable_homing", "enable_boost",
    "enable_hovering",
)


def configure_engine(engine: SonicEngine, settings: SonicSimSettings):
    """Copy the panel's tunable constants onto the engine instance."""
    for name in CONST_FIELDS:
        setattr(engine, name, getattr(settings, name))


def apply_preset_to_settings(settings, key):
    """Write a GAME_PRESETS entry onto the settings (only known, present fields)."""
    preset = sonic_core.GAME_PRESETS.get(key)
    if not preset:
        return
    for field, value in preset["values"].items():
        if hasattr(settings, field):
            try:
                setattr(settings, field, value)
            except Exception:
                pass


# =============================================================================
#  Geometry helpers
# =============================================================================
def _player_dimensions(settings):
    """(width, depth, stand_height, roll_height) in Blender units."""
    us = settings.unit_scale
    width = sonic_core.PUSH_RADIUS * 2 * us
    depth = width
    stand_h = sonic_core.HEIGHT_RADIUS_STAND * 2 * us
    roll_h = sonic_core.HEIGHT_RADIUS_ROLL * 2 * us
    return width, depth, stand_h, roll_h


def sample_curve_as_terrain(curve_obj, unit_scale):
    """Flatten a curve object into an ordered X/Z height-field (in pixels).
    This is the legacy sampler used when 'Follow Curve Depth' is off (or when
    the curve cannot be walked as a single path)."""
    pts = []
    deps = bpy.context.evaluated_depsgraph_get()
    ev = curve_obj.evaluated_get(deps)
    try:
        me = ev.to_mesh()
    except Exception:
        me = None
    if me is not None:
        mw = curve_obj.matrix_world
        for v in me.vertices:
            wv = mw @ v.co
            pts.append((wv.x / unit_scale, wv.z / unit_scale))
        ev.to_mesh_clear()
    if len(pts) < 2:
        # Fall back to reading control points directly.
        mw = curve_obj.matrix_world
        for spline in curve_obj.data.splines:
            if spline.type == "BEZIER":
                for bp in spline.bezier_points:
                    wv = mw @ bp.co
                    pts.append((wv.x / unit_scale, wv.z / unit_scale))
            else:
                for p in spline.points:
                    wv = mw @ Vector((p.co.x, p.co.y, p.co.z))
                    pts.append((wv.x / unit_scale, wv.z / unit_scale))
    return pts


def sample_curve_path(curve_obj, unit_scale):
    """Sample a curve as an ORDERED polyline of 3D points (pixel space) by
    walking the evaluated mesh's edge chain from one end to the other.
    Returns None when the tessellation is not a single unbranched chain (the
    caller then falls back to the legacy flattened sampler)."""
    deps = bpy.context.evaluated_depsgraph_get()
    ev = curve_obj.evaluated_get(deps)
    try:
        me = ev.to_mesh()
    except Exception:
        me = None
    if me is None:
        return None
    try:
        n = len(me.vertices)
        if n < 2 or len(me.edges) == 0:
            return None
        adj = [[] for _ in range(n)]
        for e in me.edges:
            a, b = e.vertices
            adj[a].append(b)
            adj[b].append(a)
        # A single path has exactly two valence-1 endpoints (or none, when
        # cyclic).  Branches (valence > 2) can't be ordered.
        if any(len(a) > 2 for a in adj):
            return None
        ends = [i for i in range(n) if len(adj[i]) == 1]
        if len(ends) not in (0, 2):
            return None
        start = ends[0] if ends else 0            # cyclic: break the loop at v0
        order = [start]
        prev = -1
        cur = start
        while True:
            nxt = None
            for cand in adj[cur]:
                if cand != prev:
                    nxt = cand
                    break
            if nxt is None or nxt == start:
                break
            order.append(nxt)
            prev, cur = cur, nxt
            if len(order) > n:                    # safety: malformed topology
                return None
        if len(order) != n:
            return None                           # several disconnected splines
        mw = curve_obj.matrix_world
        inv = 1.0 / unit_scale
        pts = []
        for i in order:
            wv = mw @ me.vertices[i].co
            pts.append((wv.x * inv, wv.y * inv, wv.z * inv))
        return pts
    finally:
        try:
            ev.to_mesh_clear()
        except Exception:
            pass


class PathMapper:
    """Maps the engine's 1D horizontal coordinate onto a 3D polyline.

    When 'Follow Curve Depth' is on, the physics' x axis is the HORIZONTAL ARC
    LENGTH s along the curve (measured in plan view, so climbing costs no s).
    This keeps the 2D simulation authentic while the path bends freely in plan
    view — it may even double back in world X.  pos(s) returns the (x, y)
    plan-view position, yaw(s) the horizontal heading, and height_points()
    the (s, z) profile that feeds the HeightfieldTerrain floor.

    Beyond either end the path is extrapolated straight along the end heading.
    """

    def __init__(self, pts3):
        self.valid = False
        if not pts3 or len(pts3) < 2:
            return
        # Height profile keeps EVERY sample (duplicated s collapses inside
        # HeightfieldTerrain, which keeps the higher floor).
        self._hpts = []
        # Plan-view polyline: only samples that actually advance s.
        sx, px, py = [], [], []
        s = 0.0
        lx, ly = pts3[0][0], pts3[0][1]
        sx.append(0.0); px.append(lx); py.append(ly)
        self._hpts.append((0.0, pts3[0][2]))
        for (x, y, z) in pts3[1:]:
            ds = math.hypot(x - lx, y - ly)
            s += ds
            self._hpts.append((s, z))
            if ds > 1e-9:
                sx.append(s); px.append(x); py.append(y)
                lx, ly = x, y
        if len(sx) < 2:
            return                                 # purely vertical "curve"
        self.sx, self.px, self.py = sx, px, py
        self.length = sx[-1]
        # Per-segment heading, then per-vertex heading (shortest-arc average of
        # the neighbouring segments) for smooth yaw interpolation.
        segyaw = []
        for i in range(len(sx) - 1):
            segyaw.append(math.atan2(py[i + 1] - py[i], px[i + 1] - px[i]))
        vyaw = [segyaw[0]]
        for i in range(1, len(sx) - 1):
            a, b = segyaw[i - 1], segyaw[i]
            vyaw.append(a + _wrap_pi(b - a) * 0.5)
        vyaw.append(segyaw[-1])
        self._segyaw = segyaw
        self._vyaw = vyaw
        self.valid = True

    # -- lookup ---------------------------------------------------------------
    def _seg(self, s):
        sx = self.sx
        lo, hi = 0, len(sx) - 1
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if sx[mid] <= s:
                lo = mid
            else:
                hi = mid
        t = (s - sx[lo]) / (sx[lo + 1] - sx[lo])
        return lo, t

    def pos(self, s):
        sx, px, py = self.sx, self.px, self.py
        if s <= sx[0]:
            yaw = self._segyaw[0]
            d = s - sx[0]
            return px[0] + math.cos(yaw) * d, py[0] + math.sin(yaw) * d
        if s >= sx[-1]:
            yaw = self._segyaw[-1]
            d = s - sx[-1]
            return px[-1] + math.cos(yaw) * d, py[-1] + math.sin(yaw) * d
        i, t = self._seg(s)
        return px[i] + (px[i + 1] - px[i]) * t, py[i] + (py[i + 1] - py[i]) * t

    def yaw(self, s):
        sx = self.sx
        if s <= sx[0]:
            return self._segyaw[0]
        if s >= sx[-1]:
            return self._segyaw[-1]
        i, t = self._seg(s)
        a, b = self._vyaw[i], self._vyaw[i + 1]
        return a + _wrap_pi(b - a) * t

    def tangent(self, s):
        yw = self.yaw(s)
        return math.cos(yw), math.sin(yw)

    def closest_s(self, x, y):
        """Arc-length parameter of the point on the path closest to (x, y)."""
        sx, px, py = self.sx, self.px, self.py
        best_s, best_d = sx[0], float("inf")
        for i in range(len(sx) - 1):
            ax, ay = px[i], py[i]
            bx, by = px[i + 1], py[i + 1]
            vx, vy = bx - ax, by - ay
            L2 = vx * vx + vy * vy
            t = 0.0 if L2 <= 0.0 else max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / L2))
            cx, cy = ax + vx * t, ay + vy * t
            d = (x - cx) ** 2 + (y - cy) ** 2
            if d < best_d:
                best_d = d
                best_s = sx[i] + (sx[i + 1] - sx[i]) * t
        return best_s

    def height_points(self):
        return list(self._hpts)


def _wrap_pi(a):
    """Wrap an angle difference to (-pi, pi] for shortest-arc interpolation."""
    while a > math.pi:
        a -= 2.0 * math.pi
    while a <= -math.pi:
        a += 2.0 * math.pi
    return a


# =============================================================================
#  Mesh collision  (BVH trees in pixel-space world coordinates)
# =============================================================================
# Modifier types that imply the mesh changes over time (auto "dynamic").
_SIM_MODIFIER_TYPES = {
    "CLOTH", "SOFT_BODY", "ARMATURE", "OCEAN", "DYNAMIC_PAINT",
    "MESH_CACHE", "MESH_SEQUENCE_CACHE", "FLUID",
}

# A ray-hit face only counts as a *wall* when its normal opposes the movement
# steeply enough (~55 deg and up).  Gentler faces are slopes and are left to the
# floor sensors, so ordinary ramps never register as walls.
_WALL_NORMAL_DOT = -0.80


class _Solid:
    """One collision object: its BVH tree (pixel space) plus behaviour."""
    __slots__ = ("obj", "tree", "surface", "is_solid", "is_trigger",
                 "trigger_toggle", "dynamic", "tri_count", "center")

    def __init__(self, obj, tree, tri_count, center, col):
        self.obj = obj
        self.tree = tree
        self.tri_count = tri_count
        self.center = center
        self.surface = _surface_from_settings(obj, col)
        self.is_solid = col.is_solid
        self.is_trigger = col.is_trigger_volume
        self.trigger_toggle = bool(col.trigger_toggle)
        self.dynamic = _object_is_dynamic(obj, col)


def _surface_from_settings(obj, col):
    kind = {
        "WALKABLE": SURFACE_WALKABLE,
        "DAMAGE": SURFACE_DAMAGE,
        "SPEED_UP": SURFACE_SPEEDUP,
        "TRIGGER": SURFACE_WALKABLE,     # a pure trigger has no contact effect
        "ICE": sonic_core.SURFACE_ICE,
        "WATER": sonic_core.SURFACE_WATER,
        "QUICKSAND": sonic_core.SURFACE_QUICKSAND,
    }.get(col.surface_type, SURFACE_WALKABLE)
    sign = 0
    if col.boost_mode == "LEFT":
        sign = -1
    elif col.boost_mode == "RIGHT":
        sign = 1
    return Surface(kind=kind, boost_sign=sign,
                   boost_power=col.boost_power, name=obj.name)


def _object_is_dynamic(obj, col):
    if col.dynamic == "STATIC":
        return False
    if col.dynamic == "DYNAMIC":
        return True
    try:
        if obj.animation_data is not None:          # keyframes or drivers
            return True
        if getattr(obj, "rigid_body", None) is not None:
            return True
        if len(obj.constraints):
            return True
        if obj.parent is not None:                  # the parent may move it
            return True
        for m in obj.modifiers:
            if m.type in _SIM_MODIFIER_TYPES:
                return True
    except Exception:
        return True                                 # can't tell -> stay correct
    return False


def _is_descendant_of(obj, root):
    p = obj.parent
    while p is not None:
        if p == root:
            return True
        p = p.parent
    return False


def collision_objects(settings):
    """The meshes that currently take part in collision (player excluded)."""
    coll = settings.collision_collection
    if coll is None:
        return []
    player = settings.player
    out = []
    try:
        objs = list(coll.all_objects)
    except Exception:
        objs = list(getattr(coll, "objects", []))
    for o in objs:
        if o.type != "MESH":
            continue
        if player is not None and (o == player or _is_descendant_of(o, player)):
            continue
        out.append(o)
    return out


def _build_world_bvh(obj, deps, unit_scale):
    """Build a BVH tree of obj's evaluated triangles in WORLD PIXEL space.
    Returns (tree, tri_count, center) — tree is None when there is nothing to
    collide with."""
    if not _HAS_BVH:
        return None, 0, Vector((0.0, 0.0, 0.0))
    ev = obj.evaluated_get(deps)
    try:
        me = ev.to_mesh()
    except Exception:
        me = None
    if me is None:
        return None, 0, Vector((0.0, 0.0, 0.0))
    try:
        me.calc_loop_triangles()
        mw = ev.matrix_world
        inv = 1.0 / unit_scale
        verts = []
        mn = [float("inf")] * 3
        mx = [float("-inf")] * 3
        for v in me.vertices:
            w = mw @ v.co
            p = (w.x * inv, w.y * inv, w.z * inv)
            verts.append(p)
            for k in range(3):
                if p[k] < mn[k]:
                    mn[k] = p[k]
                if p[k] > mx[k]:
                    mx[k] = p[k]
        tris = [tuple(lt.vertices) for lt in me.loop_triangles]
        if not tris or not verts:
            return None, 0, Vector((0.0, 0.0, 0.0))
        tree = BVHTree.FromPolygons(verts, tris, all_triangles=True)
        center = Vector(((mn[0] + mx[0]) * 0.5, (mn[1] + mx[1]) * 0.5,
                         (mn[2] + mx[2]) * 0.5))
        return tree, len(tris), center
    except Exception:
        return None, 0, Vector((0.0, 0.0, 0.0))
    finally:
        try:
            ev.to_mesh_clear()
        except Exception:
            pass


class BlenderCollisionWorld(CollisionWorld):
    """CollisionWorld backed by the analytic terrain PLUS BVH ray casts against
    every solid mesh.  All coordinates are engine pixels; the PathMapper (when
    present) turns the engine's 1D x into a 3D ray origin so collision keeps
    working while the player follows a curve through 3D space."""

    def __init__(self, terrain, mapper, solids, fixed_y, width_radius):
        self.terrain = terrain
        self.mapper = mapper
        self.all_solids = list(solids)
        self.solids = [s for s in self.all_solids if s.is_solid and s.tree is not None]
        self.triggers = [s for s in self.all_solids if s.is_trigger]
        self.fixed_y = fixed_y
        self.wr = width_radius
        self.has_walls = bool(self.solids)
        self.has_ceilings = bool(self.solids)

    # -- 1D -> 3D -------------------------------------------------------------
    def point3(self, s, z):
        if self.mapper is not None:
            x, y = self.mapper.pos(s)
            return Vector((x, y, z))
        return Vector((s, self.fixed_y, z))

    def tangent2(self, s):
        if self.mapper is not None:
            return self.mapper.tangent(s)
        return (1.0, 0.0)

    @property
    def has_dynamic(self):
        return any(s.dynamic for s in self.all_solids)

    def refresh_dynamic(self, deps, unit_scale):
        for s in self.all_solids:
            if not s.dynamic:
                continue
            tree, ntris, center = _build_world_bvh(s.obj, deps, unit_scale)
            s.tree = tree
            s.tri_count = ntris
            s.center = center
        self.solids = [s for s in self.all_solids if s.is_solid and s.tree is not None]
        self.has_walls = bool(self.solids)
        self.has_ceilings = bool(self.solids)

    # -- raw ray --------------------------------------------------------------
    def _cast(self, origin, direction, dist):
        best = None
        for s in self.solids:
            try:
                loc, nrm, _idx, d = s.tree.ray_cast(origin, direction, dist)
            except Exception:
                continue
            if loc is not None and (best is None or d < best[3]):
                best = (loc, nrm, s, d)
        return best

    # -- CollisionWorld interface --------------------------------------------
    def floor(self, x, z, reach_up, reach_down):
        best = None
        if self.terrain is not None:
            # The analytic terrain keeps its legacy unlimited reach.
            best = (self.terrain.height(x), self.terrain.angle(x), WALKABLE_SURFACE)
        length = reach_up + reach_down
        if self.solids and length > 0.0:
            tx, ty = self.tangent2(x)
            down = Vector((0.0, 0.0, -1.0))
            for off in (-self.wr, self.wr):
                origin = self.point3(x + off, z + reach_up)
                hit = self._cast(origin, down, length)
                if hit is None:
                    continue
                loc, nrm, sol, _d = hit
                if nrm.z <= 0.1:
                    continue                        # underside / wall face: not a floor
                h = loc.z
                if best is None or h > best[0]:
                    angle = math.atan2(-(nrm.x * tx + nrm.y * ty), nrm.z)
                    best = (h, angle, sol.surface)
        if best is None:
            return None
        return FloorHit(best[0], best[1], best[2])

    def wall(self, x, z, direction, max_dist):
        if not self.solids:
            return None
        tx, ty = self.tangent2(x)
        d3 = Vector((tx * direction, ty * direction, 0.0))
        origin = self.point3(x, z)
        hit = self._cast(origin, d3, max_dist)
        if hit is None:
            return None
        loc, nrm, sol, d = hit
        try:
            facing = nrm.normalized().dot(d3)
        except Exception:
            facing = -1.0
        if facing > _WALL_NORMAL_DOT:
            return None                             # a slope / backface, not a wall
        return WallHit(d, sol.surface)

    def ceiling(self, x, z_head, reach_up):
        if not self.solids or reach_up <= 0.0:
            return None
        up = Vector((0.0, 0.0, 1.0))
        best = None
        for off in (-self.wr, self.wr):
            origin = self.point3(x + off, z_head)
            hit = self._cast(origin, up, reach_up)
            if hit is None:
                continue
            loc, nrm, sol, _d = hit
            if best is None or loc.z < best[0]:
                best = (loc.z, sol.surface)
        if best is None:
            return None
        return CeilingHit(best[0], best[1])

    # -- trigger support ------------------------------------------------------
    def player_box_tree(self, engine):
        """A small BVH box around the player (pixel space, aligned to the path
        heading) used for trigger overlap tests.  Rebuilt every frame."""
        if not _HAS_BVH:
            return None
        px, py = (self.mapper.pos(engine.x) if self.mapper is not None
                  else (engine.x, self.fixed_y))
        tx, ty = self.tangent2(engine.x)
        nx, ny = -ty, tx                            # lateral (plan-view) axis
        hw = engine.push_radius
        hd = engine.push_radius
        h = 2.0 * (engine.height_radius_roll if engine.rolling
                   else engine.height_radius_stand)
        z0, z1 = engine.z, engine.z + h
        corners2 = (
            (px - tx * hw - nx * hd, py - ty * hw - ny * hd),
            (px + tx * hw - nx * hd, py + ty * hw - ny * hd),
            (px + tx * hw + nx * hd, py + ty * hw + ny * hd),
            (px - tx * hw + nx * hd, py - ty * hw + ny * hd),
        )
        verts = [(cx, cy, z0) for (cx, cy) in corners2] + \
                [(cx, cy, z1) for (cx, cy) in corners2]
        faces = [
            (0, 1, 2, 3), (7, 6, 5, 4),
            (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7),
        ]
        try:
            return BVHTree.FromPolygons(verts, faces)
        except Exception:
            return None

    def player_inside_trigger(self, box_tree, solid, engine):
        """True when the player's box touches or is fully inside the trigger."""
        if solid.tree is None:
            return False
        if box_tree is not None:
            try:
                if box_tree.overlap(solid.tree):
                    return True
            except Exception:
                pass
        # Fully-inside fallback: a ray from the player's centre that first hits
        # a BACK face means we are inside a closed volume.
        try:
            h = 2.0 * (engine.height_radius_roll if engine.rolling
                       else engine.height_radius_stand)
            origin = self.point3(engine.x, engine.z + h * 0.5)
            up = Vector((0.0, 0.0, 1.0))
            loc, nrm, _idx, _d = solid.tree.ray_cast(origin, up, 1.0e7)
            if loc is not None and nrm.dot(up) > 0.0:
                return True
        except Exception:
            pass
        return False


# =============================================================================
#  Attributes
# =============================================================================
# A fresh engine snapshot, used to initialise the custom properties so they
# appear on a freshly created player before the first simulation.
def _default_snapshot():
    snap = SonicEngine().snapshot(Inputs())
    # Attributes filled in by the Blender layer (not by the 2D core).
    snap["Path_Yaw"] = 0.0
    snap["Triggers_Inside"] = 0
    return snap


def init_player_attributes(obj):
    snap = _default_snapshot()
    for key, value in snap.items():
        obj[key] = value
    # Give the numeric attributes friendly UI metadata where supported.
    try:
        for key in ("X_Vel", "Z_Vel", "Ground_Speed", "Ground_Angle", "Spindash_Revs"):
            ui = obj.id_properties_ui(key)
            ui.update(description="Sonic Physics live attribute")
    except Exception:
        pass


def write_attributes(obj, snap):
    for key, value in snap.items():
        obj[key] = value


def write_engine_to_object(obj, engine, settings, mapper=None):
    us = settings.unit_scale
    rot = list(settings.sim_start_rotation)
    rot[1] = -engine.ground_angle  # tilt about Y to match the ground normal
    if mapper is not None:
        px, py = mapper.pos(engine.x)
        obj.location = Vector((px * us, py * us, engine.z * us))
        if settings.curve_follow_rotation:
            rot[2] = mapper.yaw(engine.x)
    else:
        obj.location = Vector((engine.x * us, settings.sim_start_location[1], engine.z * us))
    obj.rotation_euler = Euler(rot, "XYZ")


# =============================================================================
#  GPU overlay
# =============================================================================
_draw_handle = None


def _get_line_shader():
    if not _HAS_GPU:
        return None
    for name in ("UNIFORM_COLOR", "3D_UNIFORM_COLOR"):
        try:
            return gpu.shader.from_builtin(name)
        except Exception:
            continue
    return None


def _box_edges(mw, w, d, h):
    """World-space line segments for a box whose bottom face sits on the object
    origin, transformed by matrix_world mw."""
    hw, hd = w / 2.0, d / 2.0
    c = [
        Vector((-hw, -hd, 0)), Vector((hw, -hd, 0)), Vector((hw, hd, 0)), Vector((-hw, hd, 0)),
        Vector((-hw, -hd, h)), Vector((hw, -hd, h)), Vector((hw, hd, h)), Vector((-hw, hd, h)),
    ]
    c = [mw @ p for p in c]
    edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),   # bottom
        (4, 5), (5, 6), (6, 7), (7, 4),   # top
        (0, 4), (1, 5), (2, 6), (3, 7),   # verticals
    ]
    out = []
    for a, b in edges:
        out.append(c[a]); out.append(c[b])
    return out


def _draw_overlay_callback():
    if not _HAS_GPU:
        return
    try:
        settings = bpy.context.scene.sonic_sim
    except Exception:
        return
    if not settings.draw_overlay:
        return
    obj = settings.player
    if obj is None:
        return
    shader = _get_line_shader()
    if shader is None:
        return

    width, depth, stand_h, roll_h = _player_dimensions(settings)
    rolling = bool(obj.get("Is_Rolling", False))
    h = roll_h if rolling else stand_h
    on_ground = bool(obj.get("On_Ground", True))
    hurt = bool(obj.get("Is_Hurt", False))

    mw = obj.matrix_world
    box = _box_edges(mw, width, depth, h)

    # Ground sensors: two short probes straight down from the feet corners.
    us = settings.unit_scale
    wr = sonic_core.WIDTH_RADIUS * us
    probe = sonic_core.HEIGHT_RADIUS_STAND * us
    sensors = [
        mw @ Vector((-wr, 0, 0)), mw @ Vector((-wr, 0, -probe)),
        mw @ Vector((wr, 0, 0)), mw @ Vector((wr, 0, -probe)),
    ]
    # Push sensors: left/right at mid height.
    pr = sonic_core.PUSH_RADIUS * us
    midz = h * 0.5
    walls = [
        mw @ Vector((-pr, 0, midz)), mw @ Vector((-pr - probe * 0.4, 0, midz)),
        mw @ Vector((pr, 0, midz)), mw @ Vector((pr + probe * 0.4, 0, midz)),
    ]

    try:
        gpu.state.blend_set("ALPHA")
        gpu.state.line_width_set(2.0)
    except Exception:
        pass

    def draw(coords, color):
        try:
            batch = batch_for_shader(shader, "LINES", {"pos": coords})
            shader.bind()
            shader.uniform_float("color", color)
            batch.draw(shader)
        except Exception:
            pass

    # box: red while hurt, green on ground, cyan in the air
    if hurt:
        box_color = (1.0, 0.2, 0.2, 0.95)
    elif on_ground:
        box_color = (0.15, 1.0, 0.35, 0.9)
    else:
        box_color = (0.2, 0.8, 1.0, 0.9)
    draw(box, box_color)
    draw(sensors, (1.0, 0.85, 0.1, 0.9))   # floor sensors, yellow
    draw(walls, (1.0, 0.3, 0.3, 0.9))      # wall sensors, red

    try:
        gpu.state.line_width_set(1.0)
        gpu.state.blend_set("NONE")
    except Exception:
        pass


def _enable_overlay():
    global _draw_handle
    if not _HAS_GPU or _draw_handle is not None:
        return
    try:
        _draw_handle = bpy.types.SpaceView3D.draw_handler_add(
            _draw_overlay_callback, (), "WINDOW", "POST_VIEW"
        )
    except Exception:
        _draw_handle = None


def _disable_overlay():
    global _draw_handle
    if _draw_handle is not None:
        try:
            bpy.types.SpaceView3D.draw_handler_remove(_draw_handle, "WINDOW")
        except Exception:
            pass
        _draw_handle = None


# =============================================================================
#  Operators
# =============================================================================
class SONIC_OT_add_player(Operator):
    bl_idname = "sonic.add_player"
    bl_label = "Add Sonic Player"
    bl_description = "Create the cube-empty player (origin at the bottom / feet) and select it"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.sonic_sim
        width, depth, stand_h, roll_h = _player_dimensions(settings)
        loc = context.scene.cursor.location.copy()

        if settings.player_type == "MESH":
            obj = self._make_mesh_cube(context, width, depth, stand_h, loc)
        else:
            obj = bpy.data.objects.new("Sonic_Player", None)
            obj.empty_display_type = "CUBE"
            obj.empty_display_size = stand_h / 2.0
            obj.location = loc
            context.collection.objects.link(obj)

        obj.show_name = True
        obj["_sonic_player"] = True
        init_player_attributes(obj)
        settings.player = obj

        for o in context.selected_objects:
            o.select_set(False)
        obj.select_set(True)
        context.view_layer.objects.active = obj
        self.report({"INFO"}, "Sonic player created (origin at the feet). Z=0 is the floor.")
        return {"FINISHED"}

    @staticmethod
    def _make_mesh_cube(context, width, depth, height, loc):
        me = bpy.data.meshes.new("Sonic_Player")
        hw, hd = width / 2.0, depth / 2.0
        verts = [
            (-hw, -hd, 0.0), (hw, -hd, 0.0), (hw, hd, 0.0), (-hw, hd, 0.0),
            (-hw, -hd, height), (hw, -hd, height), (hw, hd, height), (-hw, hd, height),
        ]
        faces = [
            (0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4),
            (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7),
        ]
        me.from_pydata(verts, [], faces)
        me.update()
        obj = bpy.data.objects.new("Sonic_Player", me)
        obj.display_type = "WIRE"
        obj.show_in_front = True
        obj.location = loc
        context.collection.objects.link(obj)
        return obj


# The pre-made character ships next to this file inside the add-on.
BUNDLED_BLEND_NAME = "Sonic.blend"
PREMADE_COLLECTION = "SonicTheHedgehog"


def _bundled_blend_path():
    """Absolute path to the bundled Sonic.blend, or None when it isn't there."""
    path = os.path.join(os.path.dirname(__file__), BUNDLED_BLEND_NAME)
    return path if os.path.isfile(path) else None


class SONIC_OT_import_premade(Operator):
    bl_idname = "sonic.import_premade"
    bl_label = "Import Pre-Made Sonic"
    bl_description = ("Append the '%s' collection from the Sonic.blend bundled with this "
                      "add-on into the current scene" % PREMADE_COLLECTION)
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        path = _bundled_blend_path()
        if path is None:
            self.report({"ERROR"},
                        "Sonic.blend was not found next to the add-on "
                        "(expected %s beside __init__.py)." % BUNDLED_BLEND_NAME)
            return {"CANCELLED"}

        # Append (link=False) so the character becomes a self-contained local
        # copy that doesn't depend on the add-on file staying in place.
        try:
            with bpy.data.libraries.load(path, link=False) as (data_from, data_to):
                available = list(data_from.collections)
                if PREMADE_COLLECTION not in available:
                    self._available = available
                    data_to.collections = []
                else:
                    data_to.collections = [PREMADE_COLLECTION]
        except Exception as exc:
            self.report({"ERROR"}, "Could not read Sonic.blend: %s" % exc)
            return {"CANCELLED"}

        appended = [c for c in getattr(data_to, "collections", []) if c is not None]
        if not appended:
            avail = getattr(self, "_available", [])
            if avail:
                shown = ", ".join(avail[:8]) + ("…" if len(avail) > 8 else "")
                self.report({"ERROR"},
                            "Collection '%s' isn't in Sonic.blend. Found: %s"
                            % (PREMADE_COLLECTION, shown))
            else:
                self.report({"ERROR"}, "Sonic.blend contains no collections to import.")
            return {"CANCELLED"}

        # Link the freshly appended collection(s) into the scene so they show up,
        # then select their objects and make one active for convenience.
        scene_children = context.scene.collection.children
        for o in context.selected_objects:
            o.select_set(False)
        last_obj = None
        linked_names = []
        for coll in appended:
            try:
                scene_children.link(coll)
            except Exception:
                pass  # already linked (e.g. re-import) — objects are still present
            linked_names.append(coll.name)
            for ob in coll.all_objects:
                try:
                    ob.select_set(True)
                    last_obj = ob
                except Exception:
                    pass
        if last_obj is not None:
            context.view_layer.objects.active = last_obj

        # Blender numbers duplicates on re-import; report the real name(s).
        note = ""
        if any(n != PREMADE_COLLECTION for n in linked_names):
            note = " (imported as %s)" % ", ".join(linked_names)
        self.report({"INFO"}, "Imported pre-made Sonic%s." % note)
        return {"FINISHED"}


class SONIC_OT_reset_player(Operator):
    bl_idname = "sonic.reset_player"
    bl_label = "Reset Player"
    bl_description = "Move the player back to where the last simulation started and clear its rotation"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.sonic_sim
        obj = settings.player
        if obj is None:
            self.report({"WARNING"}, "No player set")
            return {"CANCELLED"}
        obj.location = Vector(settings.sim_start_location)
        obj.rotation_euler = Euler(settings.sim_start_rotation, "XYZ")
        return {"FINISHED"}


class SONIC_OT_clear_bake(Operator):
    bl_idname = "sonic.clear_bake"
    bl_label = "Clear Baked Animation"
    bl_description = "Remove the player's animation (baked keyframes)"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.sonic_sim
        obj = settings.player
        if obj is None or obj.animation_data is None:
            self.report({"INFO"}, "Nothing to clear")
            return {"CANCELLED"}
        obj.animation_data_clear()
        self.report({"INFO"}, "Baked animation cleared")
        return {"FINISHED"}


class SONIC_OT_reset_constants(Operator):
    bl_idname = "sonic.reset_constants"
    bl_label = "Reset To Sonic 1 Defaults"
    bl_description = "Restore every physics constant to its authentic Sonic 1 value"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        settings = context.scene.sonic_sim
        # Re-apply property defaults by unsetting them.
        for name in CONST_FIELDS:
            settings.property_unset(name)
        self.report({"INFO"}, "Physics constants reset")
        return {"FINISHED"}


# =============================================================================
#  Shared simulation context (used by live play AND TAS playback)
# =============================================================================
class _SimContext:
    __slots__ = ("engine", "world", "mapper", "terrain", "warnings", "unit_scale")


def build_sim_context(context, settings, obj):
    """Build the terrain, path mapper, mesh-collision world and a configured
    engine, and place the player at its start.  Shared by the live simulator and
    the TAS playback operator so both run identical physics."""
    us = settings.unit_scale
    warnings = []
    mapper = None

    if settings.use_curve_ground and settings.ground_curve is not None:
        if settings.curve_follow_depth:
            try:
                pts3 = sample_curve_path(settings.ground_curve, us)
            except Exception:
                pts3 = None
            if pts3 is not None and len(pts3) >= 2:
                m = PathMapper(pts3)
                if m.valid:
                    mapper = m
            if mapper is None:
                warnings.append("Curve depth needs one connected, unbranched curve — "
                                "flattened onto X/Z instead.")
        if mapper is not None:
            terrain = HeightfieldTerrain(mapper.height_points())
        else:
            pts = sample_curve_as_terrain(settings.ground_curve, us)
            terrain = HeightfieldTerrain(pts) if len(pts) >= 2 else FlatTerrain(0.0)
    else:
        terrain = FlatTerrain(0.0)

    solids = []
    if settings.use_mesh_collision:
        if not _HAS_BVH:
            warnings.append("mathutils.bvhtree unavailable — mesh collision disabled.")
        elif settings.collision_collection is None:
            warnings.append("Mesh collision is on but no collection is chosen.")
        else:
            deps = context.evaluated_depsgraph_get()
            heavy = []
            for cobj in collision_objects(settings):
                tree, ntris, center = _build_world_bvh(cobj, deps, us)
                if tree is None:
                    continue
                solids.append(_Solid(cobj, tree, ntris, center, cobj.sonic_collision))
                if ntris > settings.poly_warn_threshold:
                    heavy.append("%s (%d tris)" % (cobj.name, ntris))
            if heavy:
                warnings.append("High-poly collision: " + ", ".join(heavy))
            if not solids:
                warnings.append("No usable meshes in the collision collection.")

    world = None
    if solids:
        world = BlenderCollisionWorld(
            terrain, mapper, solids,
            fixed_y=obj.location.y / us,
            width_radius=float(sonic_core.WIDTH_RADIUS),
        )

    engine = SonicEngine(terrain, world)
    configure_engine(engine, settings)
    if mapper is not None:
        s0 = mapper.closest_s(obj.location.x / us, obj.location.y / us)
        engine.set_position(s0, obj.location.z / us)
    else:
        engine.set_position(obj.location.x / us, obj.location.z / us)

    ctx = _SimContext()
    ctx.engine, ctx.world, ctx.mapper, ctx.terrain = engine, world, mapper, terrain
    ctx.warnings, ctx.unit_scale = warnings, us
    return ctx


def gather_sonic_objects(context, settings):
    """Collect all Spring/Ring/Motobug objects in the scene into cached records."""
    out = []
    player = settings.player
    for o in context.scene.objects:
        so = getattr(o, "sonic_object", None)
        if so is None or so.kind == "NONE":
            continue
        if player is not None and (o == player or _is_descendant_of(o, player)):
            continue
        out.append({
            "obj": o,
            "kind": so.kind,
            "spring_power": float(so.spring_power),
            "spring_vec": SPRING_DIR_VECTORS.get(so.spring_dir, (0.0, 1.0)),
            "ring_value": int(so.ring_value),
            "radius": float(so.interact_radius),
        })
    return out


def _player_pixel_pos(S):
    """Player position in world PIXEL space (x, y, z)."""
    eng = S.engine
    if S.mapper is not None:
        px, py = S.mapper.pos(eng.x)
    else:
        px, py = eng.x, S.fixed_y_px
    return px, py, eng.z


def sim_trigger_pass(S, inp):
    """Overlap-test trigger volumes: damage/boost effects, water (underwater +
    drown) and quicksand (sink/mash), plus toggle latching mirrored onto each
    trigger object.  ``S`` carries engine/world/mapper/trigger_states.  Returns
    how many triggers the player is inside."""
    world = S.world
    eng = S.engine
    if world is None or not world.triggers:
        eng.set_underwater(False)
        return 0
    box = world.player_box_tree(eng)
    inside_count = 0
    if world.mapper is not None:
        ppx, ppy = world.mapper.pos(eng.x)
    else:
        ppx, ppy = eng.x, world.fixed_y
    tx, ty = world.tangent2(eng.x)

    any_water = False
    quicksand_sol = None
    for sol in world.triggers:
        state = S.trigger_states.setdefault(sol.obj.name, [False, False])
        inside = world.player_inside_trigger(box, sol, eng)
        state[0] = inside
        kind = sol.surface.kind
        if inside:
            inside_count += 1
            if not state[1]:
                state[1] = True
            if kind == SURFACE_DAMAGE:
                proj = (ppx - sol.center.x) * tx + (ppy - sol.center.y) * ty
                away = 1 if proj > 1e-6 else (-1 if proj < -1e-6 else 0)
                eng.hurt(away)
            elif kind == SURFACE_SPEEDUP:
                eng.apply_boost(sol.surface.boost_sign, sol.surface.boost_power)
            elif kind == sonic_core.SURFACE_WATER:
                any_water = True
            elif kind == sonic_core.SURFACE_QUICKSAND:
                quicksand_sol = sol
        active = (state[1] or inside) if sol.trigger_toggle else inside
        try:
            sol.obj["Sonic_Trigger_Active"] = 1 if active else 0
            sol.obj["Sonic_Player_Inside"] = 1 if inside else 0
        except Exception:
            pass

    # Water: persistent underwater flag + drowning handled inside the engine.
    eng.set_underwater(any_water)
    # Quicksand: sink / mash, and die if sunk out of the bottom of the volume.
    if quicksand_sol is not None:
        eng.apply_quicksand(inp)
        try:
            half = (quicksand_sol.obj.dimensions.z / S.unit_scale) * 0.5
            if eng.z < quicksand_sol.center.z - half:
                eng.die("quicksand")
        except Exception:
            pass
    return inside_count


def _hide_object(S, o):
    S.hidden_objects.append((o, o.hide_viewport))
    try:
        o.hide_viewport = True
    except Exception:
        pass


def sim_object_pass(S, inp):
    """Springs launch, rings are collected, Motobugs are bopped or bite back,
    and the homing target is refreshed to the nearest badnik in range."""
    if not S.sonic_objects:
        S.engine.homing_target = None
        return
    eng = S.engine
    us = S.unit_scale
    px, py, pz = _player_pixel_pos(S)
    tx, ty = (S.mapper.tangent(eng.x) if S.mapper is not None else (1.0, 0.0))

    # decay spring cooldowns
    for k in list(S.spring_cooldown.keys()):
        S.spring_cooldown[k] -= 1
        if S.spring_cooldown[k] <= 0:
            del S.spring_cooldown[k]

    nearest_badnik = None
    nearest_d2 = None
    hz2 = eng.homing_range * eng.homing_range

    for rec in S.sonic_objects:
        o = rec["obj"]
        name = o.name
        if rec["kind"] in ("RING", "MONITOR") and name in S.collected_rings:
            continue
        if rec["kind"] == "MOTOBUG" and name in S.destroyed_badniks:
            continue
        wp = o.matrix_world.translation
        ox, oy, oz = wp.x / us, wp.y / us, wp.z / us
        dx, dy, dz = ox - px, oy - py, oz - pz
        d2 = dx * dx + dy * dy + dz * dz
        r = rec["radius"]
        touching = d2 <= r * r

        if rec["kind"] == "SPRING":
            if touching and name not in S.spring_cooldown:
                vx, vz = rec["spring_vec"]
                eng.spring(rec["spring_power"], vx, vz)
                S.spring_cooldown[name] = 8
        elif rec["kind"] == "RING":
            if touching:
                eng.collect_ring(rec["ring_value"])
                S.collected_rings.add(name)
                _hide_object(S, o)
        elif rec["kind"] == "SPIKES":
            if touching:
                # Spikes hurt even while attacking (only invincibility saves you).
                proj = dx * tx + dy * ty
                away = 1 if proj > 1e-6 else (-1 if proj < -1e-6 else 0)
                eng.hurt(away)
        elif rec["kind"] == "BUMPER":
            if touching and name not in S.spring_cooldown:
                # Bounce away from the bumper's centre in engine (s, z) space.
                s_obj = S.mapper.closest_s(ox, oy) if S.mapper is not None else ox
                bx = eng.x - s_obj
                bz = eng.z - oz
                if abs(bx) < 1e-6 and abs(bz) < 1e-6:
                    bz = 1.0
                eng.spring(rec["spring_power"], bx, bz)
                S.spring_cooldown[name] = 8
        elif rec["kind"] == "MONITOR":
            if touching and eng.attack_active():
                eng.collect_ring(rec["ring_value"])
                eng.bounce_enemy()
                S.collected_rings.add(name)
                _hide_object(S, o)
        elif rec["kind"] == "MOTOBUG":
            if touching:
                if eng.attack_active():
                    S.destroyed_badniks.add(name)
                    S.badniks_destroyed += 1
                    eng.bounce_enemy()
                    eng.homing_target = None
                    _hide_object(S, o)
                    continue
                else:
                    proj = dx * tx + dy * ty
                    away = 1 if proj > 1e-6 else (-1 if proj < -1e-6 else 0)
                    eng.hurt(away)
            # candidate for a homing lock
            if eng.enable_homing and d2 <= hz2:
                if nearest_d2 is None or d2 < nearest_d2:
                    nearest_d2 = d2
                    nearest_badnik = (ox, oy, oz)

    if eng.enable_homing and nearest_badnik is not None:
        bx, by, bz = nearest_badnik
        s = S.mapper.closest_s(bx, by) if S.mapper is not None else bx
        eng.homing_target = (s, bz)
    elif eng.enable_homing:
        eng.homing_target = None


class SONIC_OT_simulate(Operator):
    bl_idname = "sonic.simulate"
    bl_label = "Simulate (Play)"
    bl_description = ("Enter simulation mode. Arrows = D-pad; A/S/D = A/B/C; Q/W/E = X/Y/Z; "
                      "Enter = Start; Esc = stop. ALL other keys/shortcuts are disabled")
    bl_options = {"REGISTER"}

    _timer = None

    # ------------------------------------------------------------------ setup
    def invoke(self, context, event):
        settings = context.scene.sonic_sim
        if settings.is_simulating:
            self.report({"WARNING"}, "Already simulating")
            return {"CANCELLED"}
        obj = settings.player
        if obj is None:
            self.report({"ERROR"}, "No player set. Use 'Add Sonic Player' first.")
            return {"CANCELLED"}

        us = settings.unit_scale
        settings.collision_warning_text = ""

        # --- terrain, collision world and engine (shared builder) -----------
        ctx = build_sim_context(context, settings, obj)
        self.mapper = ctx.mapper
        self.world = ctx.world
        self.engine = ctx.engine
        warnings = ctx.warnings

        if warnings:
            settings.collision_warning_text = "  •  ".join(warnings)
            for w in warnings:
                self.report({"WARNING"}, w)

        # remember where we started so Reset works and so depth/rotation persist
        settings.sim_start_location = obj.location
        settings.sim_start_rotation = obj.rotation_euler

        # --- trigger bookkeeping ---------------------------------------------
        # {object name: [inside_now, latched]}
        self.trigger_states = {}
        if self.world is not None:
            for sol in self.world.triggers:
                self.trigger_states[sol.obj.name] = [False, False]
                try:
                    sol.obj["Sonic_Trigger_Active"] = 0
                    sol.obj["Sonic_Player_Inside"] = 0
                except Exception:
                    pass

        # --- gameplay objects (springs / rings / motobugs) ------------------
        self.unit_scale = us
        self.fixed_y_px = obj.location.y / us
        self.sonic_objects = gather_sonic_objects(context, settings)
        self.spring_cooldown = {}
        self.collected_rings = set()
        self.destroyed_badniks = set()
        self.hidden_objects = []
        self.badniks_destroyed = 0

        # --- TAS input recording --------------------------------------------
        self.tas_record = bool(settings.tas_record)
        self.tas_history = []

        # --- input bookkeeping ----------------------------------------------
        self.held = {}
        self.edges = set()
        self.obj = obj
        self.settings = settings
        self.bake = settings.bake_animation
        self.history = []          # list of (loc, rot, snapshot) when baking
        self.start_frame = context.scene.frame_current
        self.frame_count = 0
        self.max_frames = settings.fps * 60 * 30   # 30 min safety cap
        self.prev_world_y = obj.location.y
        self.sync = bool(settings.sync_timeline and self.world is not None
                         and self.world.has_dynamic)

        settings.is_simulating = True
        settings.status_text = "SIMULATING — Esc to stop"

        if settings.draw_overlay:
            _enable_overlay()

        wm = context.window_manager
        self._timer = wm.event_timer_add(1.0 / max(1, settings.fps), window=context.window)
        wm.modal_handler_add(self)
        _tag_redraw(context)
        return {"RUNNING_MODAL"}

    # ------------------------------------------------------------------ modal
    def modal(self, context, event):
        # End the simulation.
        if event.type == "ESC" and event.value == "PRESS":
            self._finish(context)
            return {"FINISHED"}

        try:
            if event.type == "TIMER":
                self._tick(context)
                if self.frame_count >= self.max_frames:
                    self._finish(context)
                    return {"FINISHED"}
                return {"RUNNING_MODAL"}

            # Track the SEGA pad.
            if event.type in KEYMAP:
                btn = KEYMAP[event.type]
                if event.value == "PRESS":
                    if not self.held.get(btn, False):
                        self.edges.add(btn)
                    self.held[btn] = True
                elif event.value == "RELEASE":
                    self.held[btn] = False
                return {"RUNNING_MODAL"}

        except Exception as exc:  # never leave the user trapped in a modal loop
            self.report({"ERROR"}, "Simulation error: %s" % exc)
            self._finish(context)
            return {"CANCELLED"}

        # Swallow every other event so no Blender shortcut fires while simulating.
        return {"RUNNING_MODAL"}

    # ---------------------------------------------------------------- stepping
    def _tick(self, context):
        # Live objects: advance the timeline so animation / rigid bodies play,
        # then rebuild the dynamic collision shapes from the evaluated scene.
        if self.sync:
            try:
                context.scene.frame_set(self.start_frame + self.frame_count)
            except Exception:
                pass
        if self.world is not None and self.world.has_dynamic:
            try:
                deps = context.evaluated_depsgraph_get()
                self.world.refresh_dynamic(deps, self.settings.unit_scale)
            except Exception:
                pass

        inp = Inputs(
            left=self.held.get("left", False),
            right=self.held.get("right", False),
            up=self.held.get("up", False),
            down=self.held.get("down", False),
            a=self.held.get("a", False),
            b=self.held.get("b", False),
            c=self.held.get("c", False),
            x=self.held.get("x", False),
            y=self.held.get("y", False),
            z=self.held.get("z", False),
            start=self.held.get("start", False),
            jump_pressed=any(k in self.edges for k in JUMP_BUTTONS),
            down_pressed=("down" in self.edges),
        )
        self.edges.clear()

        if self.tas_record:
            self.tas_history.append({
                "left": inp.left, "right": inp.right, "up": inp.up, "down": inp.down,
                "a": inp.a, "b": inp.b, "c": inp.c, "x": inp.x, "y": inp.y,
                "z": inp.z, "start": inp.start,
            })

        self.engine.step(inp)
        triggers_inside = sim_trigger_pass(self, inp)
        sim_object_pass(self, inp)
        snap = self.engine.snapshot(inp)

        obj = self.obj
        write_engine_to_object(obj, self.engine, self.settings, self.mapper)

        # Depth motion: real Y velocity (pixels/frame) from the world position.
        us = self.settings.unit_scale
        yv = (obj.location.y - self.prev_world_y) / us
        self.prev_world_y = obj.location.y
        snap["Y_Vel"] = float(yv)
        snap["Y_Vel_Absolute"] = float(abs(yv))
        snap["Path_Yaw"] = float(math.degrees(self.mapper.yaw(self.engine.x))) \
            if self.mapper is not None else 0.0
        snap["Triggers_Inside"] = int(triggers_inside)
        snap["Badniks_Destroyed"] = int(self.badniks_destroyed)

        write_attributes(obj, snap)
        try:
            obj["Sonic_Death_Cause"] = self.engine.death_cause
        except Exception:
            pass

        if self.bake:
            self.history.append((tuple(obj.location), tuple(obj.rotation_euler), snap))

        self.frame_count += 1
        self.settings.status_text = "SIMULATING f%d — Esc to stop" % self.frame_count
        _tag_redraw(context)

    # ----------------------------------------------------------------- cleanup
    def _finish(self, context):
        wm = context.window_manager
        if self._timer is not None:
            wm.event_timer_remove(self._timer)
            self._timer = None
        _disable_overlay()

        settings = self.settings
        settings.is_simulating = False
        settings.status_text = ""

        # Un-hide any rings / badniks that were collected or destroyed live.
        for o, orig in getattr(self, "hidden_objects", []):
            try:
                o.hide_viewport = orig
            except Exception:
                pass

        if self.bake and self.history:
            try:
                self._write_bake(context)
            except Exception as exc:
                self.report({"WARNING"}, "Baking failed: %s" % exc)
        elif self.sync:
            # We advanced the timeline for live objects; put it back.
            try:
                context.scene.frame_set(self.start_frame)
            except Exception:
                pass

        # Bake the recorded input timeline (TAS) if requested.
        if getattr(self, "tas_record", False) and self.tas_history:
            try:
                n = bake_tas(self.obj, self.tas_history, self.start_frame)
                self.report({"INFO"}, "Recorded %d input frames (TAS_* channels)" % n)
            except Exception as exc:
                self.report({"WARNING"}, "TAS recording failed: %s" % exc)

        _tag_redraw(context)

    def _write_bake(self, context):
        settings = self.settings
        scene = context.scene
        if settings.set_scene_fps:
            scene.render.fps = 60
            scene.render.fps_base = 1.0

        n = bake_history(self.obj, self.history, self.start_frame, settings.bake_attributes)

        scene.frame_start = min(scene.frame_start, self.start_frame)
        scene.frame_end = max(scene.frame_end, self.start_frame + n - 1)
        scene.frame_set(self.start_frame)
        self.report({"INFO"}, "Baked %d frames (%d..%d)"
                    % (n, self.start_frame, self.start_frame + n - 1))


# =============================================================================
#  Bake helpers
# =============================================================================
# Blender 4.4 replaced the animation system with "slotted" Actions: F-Curves
# now live inside an Action > Layer > Strip > Channelbag bound to the object via
# an ActionSlot.  Creating F-Curves the old way (``action.fcurves.new``) on 4.4+
# produces curves that are NOT bound to any slot, so they exist but never drive
# the object (empty timeline, no playback).  We build a version-correct F-Curve
# *container* and add curves to that, keeping the fast bulk fill on every build.

def _prepare_fcurve_container(obj):
    """Give obj a fresh action and return (action, container) where container
    exposes ``fcurves.new()`` correctly bound to obj on both legacy and
    slotted Actions."""
    obj.animation_data_clear()
    anim = obj.animation_data_create()
    action = bpy.data.actions.new("SonicBake")
    anim.action = action

    # Slotted Actions (Blender >= 4.4).
    if hasattr(action, "slots") and hasattr(action, "layers"):
        try:
            slot = action.slots.new(id_type="OBJECT", name=obj.name)
            if hasattr(anim, "action_slot"):
                anim.action_slot = slot          # bind the object to this slot
            layer = action.layers[0] if len(action.layers) else action.layers.new("Layer")
            strip = layer.strips[0] if len(layer.strips) else layer.strips.new(type="KEYFRAME")
            channelbag = None
            try:
                channelbag = strip.channelbag(slot, ensure=True)
            except TypeError:
                try:
                    channelbag = strip.channelbags.new(slot)
                except Exception:
                    channelbag = strip.channelbag(slot)
            if channelbag is not None:
                return action, channelbag
        except Exception:
            pass   # fall through to the legacy accessor

    # Legacy Actions (Blender < 4.4): F-Curves live directly on the action.
    return action, action


def bake_history(obj, history, start_frame, bake_attributes):
    """Write a recorded (location, rotation, snapshot) history onto obj as a
    fresh action of keyframes.  Returns the number of frames written.  Works on
    both legacy (<4.4) and slotted (>=4.4) Actions."""
    action, container = _prepare_fcurve_container(obj)

    n = len(history)
    for idx in range(3):
        _bulk_fcurve(container, "location", idx, start_frame,
                     [loc[idx] for (loc, rot, snap) in history], "LINEAR")
    for idx in range(3):
        _bulk_fcurve(container, "rotation_euler", idx, start_frame,
                     [rot[idx] for (loc, rot, snap) in history], "LINEAR")

    if bake_attributes and n:
        for key in history[0][2].keys():
            values = [snap[key] for (loc, rot, snap) in history]
            interp = "LINEAR" if isinstance(values[0], float) else "CONSTANT"
            _bulk_fcurve(container, '["%s"]' % key, 0, start_frame,
                         [float(v) for v in values], interp)
    return n


def _bulk_fcurve(container, data_path, index, start_frame, values, interpolation):
    """Create an F-Curve on the container (an Action or a slotted Channelbag)
    and fill it with one key per value, fast."""
    fc = None
    try:
        fc = container.fcurves.find(data_path, index=index)
    except Exception:
        fc = None
    if fc is None:
        try:
            fc = container.fcurves.new(data_path, index=index)
        except Exception:
            return
    n = len(values)
    fc.keyframe_points.add(n)
    for i in range(n):
        kp = fc.keyframe_points[i]
        kp.co = (start_frame + i, values[i])
        kp.interpolation = interpolation
    fc.update()


def baked_fcurves(obj):
    """Return obj's baked F-Curves regardless of Blender version."""
    ad = obj.animation_data
    if ad is None or ad.action is None:
        return []
    action = ad.action
    slot = getattr(ad, "action_slot", None)
    if hasattr(action, "layers") and slot is not None:
        for layer in action.layers:
            for strip in layer.strips:
                try:
                    cb = strip.channelbag(slot)
                except Exception:
                    cb = None
                if cb is not None and len(cb.fcurves):
                    return list(cb.fcurves)
    try:
        return list(action.fcurves)
    except Exception:
        return []


def _tag_redraw(context):
    try:
        for area in context.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
    except Exception:
        pass


# =============================================================================
#  TASing: record inputs to keyframes, and re-simulate deterministically
# =============================================================================
TAS_CHANNELS = ("left", "right", "up", "down", "a", "b", "c", "x", "y", "z", "start")


def _tas_key(ch):
    return "TAS_" + ch.capitalize()


def bake_tas(obj, tas_history, start_frame):
    """Keyframe every button (0/1, CONSTANT interpolation) per frame as TAS_*
    custom properties on the player.  Returns the number of frames written."""
    action, container = _prepare_fcurve_container(obj)
    n = len(tas_history)
    if not n:
        return 0
    for ch in TAS_CHANNELS:                 # make sure the props exist
        try:
            obj[_tas_key(ch)] = 1 if tas_history[0][ch] else 0
        except Exception:
            pass
    for ch in TAS_CHANNELS:
        vals = [1.0 if fr[ch] else 0.0 for fr in tas_history]
        _bulk_fcurve(container, '["%s"]' % _tas_key(ch), 0, start_frame, vals, "CONSTANT")
    return n


def read_tas_inputs(obj):
    """Rebuild a per-frame Inputs list from the player's TAS_* F-curves.
    Returns (start_frame, [Inputs, ...]) or None if there are no TAS channels."""
    fcs = {}
    for fc in baked_fcurves(obj):
        dp = getattr(fc, "data_path", "")
        if dp and dp.startswith('["TAS_'):
            fcs[dp] = fc
    if not fcs:
        return None
    fmin = fmax = None
    for fc in fcs.values():
        for kp in fc.keyframe_points:
            f = kp.co[0]
            fmin = f if fmin is None else min(fmin, f)
            fmax = f if fmax is None else max(fmax, f)
    if fmin is None:
        return None
    start, end = int(round(fmin)), int(round(fmax))

    def sample(ch, frame):
        fc = fcs.get('["%s"]' % _tas_key(ch))
        return bool(fc) and fc.evaluate(frame) >= 0.5

    frames = []
    prev = {ch: False for ch in TAS_CHANNELS}
    for f in range(start, end + 1):
        cur = {ch: sample(ch, f) for ch in TAS_CHANNELS}
        jump_pressed = any(cur[b] and not prev[b] for b in ("a", "b", "c"))
        down_pressed = cur["down"] and not prev["down"]
        frames.append(Inputs(
            left=cur["left"], right=cur["right"], up=cur["up"], down=cur["down"],
            a=cur["a"], b=cur["b"], c=cur["c"], x=cur["x"], y=cur["y"], z=cur["z"],
            start=cur["start"], jump_pressed=jump_pressed, down_pressed=down_pressed))
        prev = cur
    return start, frames


class _TasRunner:
    """Minimal state carrier so sim_trigger_pass / sim_object_pass work outside
    the live modal operator (used by TAS playback)."""
    def __init__(self, ctx, settings, obj, context):
        self.engine = ctx.engine
        self.world = ctx.world
        self.mapper = ctx.mapper
        self.unit_scale = ctx.unit_scale
        self.fixed_y_px = obj.location.y / ctx.unit_scale
        self.trigger_states = {}
        if self.world is not None:
            for sol in self.world.triggers:
                self.trigger_states[sol.obj.name] = [False, False]
        self.sonic_objects = gather_sonic_objects(context, settings)
        self.spring_cooldown = {}
        self.collected_rings = set()
        self.destroyed_badniks = set()
        self.hidden_objects = []
        self.badniks_destroyed = 0
        self.prev_world_y = obj.location.y


class SONIC_OT_playback_tas(Operator):
    bl_idname = "sonic.playback_tas"
    bl_label = "Play Back TAS"
    bl_description = ("Re-run the physics deterministically from the TAS_* input channels on "
                      "the player (record them with 'Record Inputs', then edit the curves), "
                      "and bake the resulting motion")
    bl_options = {"REGISTER"}

    def execute(self, context):
        settings = context.scene.sonic_sim
        if settings.is_simulating:
            self.report({"ERROR"}, "Stop the live simulation first")
            return {"CANCELLED"}
        obj = settings.player
        if obj is None:
            self.report({"ERROR"}, "No player set")
            return {"CANCELLED"}
        data = read_tas_inputs(obj)
        if not data:
            self.report({"ERROR"}, "No TAS_* input channels on the player. Turn on "
                                   "'Record Inputs (TAS)' and run a simulation first.")
            return {"CANCELLED"}
        start_frame, frames = data

        ctx = build_sim_context(context, settings, obj)
        for w in ctx.warnings:
            self.report({"WARNING"}, w)
        us = ctx.unit_scale
        S = _TasRunner(ctx, settings, obj, context)
        scene = context.scene
        sync = bool(settings.sync_timeline and ctx.world is not None and ctx.world.has_dynamic)

        history = []
        for i, inp in enumerate(frames):
            if sync:
                try:
                    scene.frame_set(start_frame + i)
                except Exception:
                    pass
                try:
                    ctx.world.refresh_dynamic(context.evaluated_depsgraph_get(), us)
                except Exception:
                    pass
            ctx.engine.step(inp)
            sim_trigger_pass(S, inp)
            sim_object_pass(S, inp)
            snap = ctx.engine.snapshot(inp)
            write_engine_to_object(obj, ctx.engine, settings, ctx.mapper)
            yv = (obj.location.y - S.prev_world_y) / us
            S.prev_world_y = obj.location.y
            snap["Y_Vel"] = float(yv)
            snap["Y_Vel_Absolute"] = float(abs(yv))
            snap["Path_Yaw"] = float(math.degrees(ctx.mapper.yaw(ctx.engine.x))) \
                if ctx.mapper is not None else 0.0
            snap["Badniks_Destroyed"] = int(S.badniks_destroyed)
            history.append((tuple(obj.location), tuple(obj.rotation_euler), snap))

        for o, orig in S.hidden_objects:
            try:
                o.hide_viewport = orig
            except Exception:
                pass

        if settings.set_scene_fps:
            scene.render.fps = 60
            scene.render.fps_base = 1.0
        n = bake_history(obj, history, start_frame, settings.bake_attributes)
        scene.frame_start = min(scene.frame_start, start_frame)
        scene.frame_end = max(scene.frame_end, start_frame + n - 1)
        scene.frame_set(start_frame)
        self.report({"INFO"}, "TAS playback baked %d frames (%d..%d)"
                    % (n, start_frame, start_frame + n - 1))
        return {"FINISHED"}


class SONIC_OT_clear_tas(Operator):
    bl_idname = "sonic.clear_tas"
    bl_label = "Clear TAS Channels"
    bl_description = "Remove the recorded TAS_* input channels (and properties) from the player"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        obj = context.scene.sonic_sim.player
        if obj is None:
            self.report({"ERROR"}, "No player set")
            return {"CANCELLED"}
        removed = 0
        for fc in list(baked_fcurves(obj)):
            dp = getattr(fc, "data_path", "")
            if dp and dp.startswith('["TAS_'):
                try:
                    fc.id_data.fcurves.remove(fc)  # legacy
                    removed += 1
                    continue
                except Exception:
                    pass
        # also drop the custom props
        for ch in TAS_CHANNELS:
            try:
                del obj[_tas_key(ch)]
            except Exception:
                pass
        self.report({"INFO"}, "Cleared TAS channels")
        return {"FINISHED"}


# =============================================================================
#  Presets (game presets + user-savable presets)
# =============================================================================
class SONIC_OT_apply_preset(Operator):
    bl_idname = "sonic.apply_preset"
    bl_label = "Apply Game Preset"
    bl_description = "Apply the selected game preset to the physics constants and moves"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        s = context.scene.sonic_sim
        if s.preset == "CUSTOM":
            self.report({"INFO"}, "Choose a game preset first")
            return {"CANCELLED"}
        apply_preset_to_settings(s, s.preset)
        p = sonic_core.GAME_PRESETS.get(s.preset)
        note = "" if (p and p["accurate"]) else "  (approximation)"
        self.report({"INFO"}, "Applied: %s%s" % (p["label"] if p else s.preset, note))
        return {"FINISHED"}


try:
    from bl_operators.presets import AddPresetBase as _AddPresetBase
    _HAS_PRESET_BASE = True
except Exception:
    class _AddPresetBase:      # inert fallback keeps the class body + MRO valid
        pass
    _HAS_PRESET_BASE = False


class SONIC_MT_user_presets(bpy.types.Menu):
    bl_label = "User Presets"
    preset_subdir = "sonic_physics"
    preset_operator = "script.execute_preset"
    draw = bpy.types.Menu.draw_preset


class SONIC_OT_add_user_preset(_AddPresetBase, Operator):
    """Save the current physics + move settings as a reusable user preset"""
    bl_idname = "sonic.add_user_preset"
    bl_label = "Save Settings As Preset"
    preset_menu = "SONIC_MT_user_presets"
    preset_subdir = "sonic_physics"
    preset_defines = ["s = bpy.context.scene.sonic_sim"]
    preset_values = ["s.%s" % f for f in PRESET_WRITABLE_FIELDS]


# =============================================================================
#  Panels
# =============================================================================
class SonicPanelBase:
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Sonic"


class SONIC_PT_main(SonicPanelBase, Panel):
    bl_idname = "SONIC_PT_main"
    bl_label = "Sonic Physics"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sonic_sim

        col = layout.column(align=True)
        col.operator("sonic.add_player", icon="MESH_CUBE")
        col.prop(settings, "player_type", text="")
        layout.prop(settings, "player", text="Player")

        row = layout.row()
        row.enabled = _bundled_blend_path() is not None
        row.operator("sonic.import_premade", icon="ARMATURE_DATA")
        if _bundled_blend_path() is None:
            layout.label(text="Sonic.blend not bundled with the add-on", icon="ERROR")

        if settings.is_simulating:
            box = layout.box()
            box.alert = True
            box.label(text=settings.status_text or "SIMULATING", icon="REC")
            box.label(text="Press Esc to stop", icon="EVENT_ESC")
        else:
            row = layout.row()
            row.scale_y = 1.6
            row.enabled = settings.player is not None
            row.operator("sonic.simulate", icon="PLAY", text="Simulate")
            row2 = layout.row(align=True)
            row2.operator("sonic.reset_player", icon="LOOP_BACK", text="Reset")
            row2.operator("sonic.clear_bake", icon="TRASH", text="Clear Bake")


class SONIC_PT_controls(SonicPanelBase, Panel):
    bl_idname = "SONIC_PT_controls"
    bl_parent_id = "SONIC_PT_main"
    bl_label = "Controls (SEGA 6-Button)"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        pairs = [
            ("Arrow Keys", "D-Pad"),
            ("A / S / D", "A / B / C"),
            ("Q / W / E", "X / Y / Z"),
            ("Enter", "Start"),
            ("Esc", "End simulation"),
        ]
        for k, v in pairs:
            row = col.row()
            row.label(text=k)
            row.label(text=v, icon="RIGHTARROW")
        layout.label(text="Jump: A / B / C", icon="INFO")
        layout.label(text="Roll: Down while moving")
        layout.label(text="Spindash: Down + tap A/B/C, release Down")
        layout.label(text="Peel Out: hold Up + tap A/B/C,")
        layout.label(text="   keep holding Up, release Up to launch")


class SONIC_PT_world(SonicPanelBase, Panel):
    bl_idname = "SONIC_PT_world"
    bl_parent_id = "SONIC_PT_main"
    bl_label = "World & Baking"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sonic_sim
        layout.enabled = not settings.is_simulating

        layout.prop(settings, "unit_scale")

        box = layout.box()
        box.prop(settings, "use_curve_ground")
        sub = box.column()
        sub.enabled = settings.use_curve_ground
        sub.prop(settings, "ground_curve", text="Curve")
        sub.prop(settings, "curve_follow_depth")
        subrot = sub.column()
        subrot.enabled = settings.curve_follow_depth
        subrot.prop(settings, "curve_follow_rotation")
        if settings.use_curve_ground and settings.curve_follow_depth:
            box.label(text="Physics run along the path's arc length.", icon="INFO")
        box.label(text="Curve is a floor, not gravity.", icon="INFO")

        box = layout.box()
        box.prop(settings, "bake_animation", icon="REC")
        sub = box.column()
        sub.enabled = settings.bake_animation
        sub.prop(settings, "bake_attributes")
        sub.prop(settings, "set_scene_fps")

        layout.prop(settings, "draw_overlay")
        layout.prop(settings, "fps")


class SONIC_PT_collision(SonicPanelBase, Panel):
    bl_idname = "SONIC_PT_collision"
    bl_parent_id = "SONIC_PT_main"
    bl_label = "Mesh Collision"

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sonic_sim

        top = layout.column()
        top.enabled = not settings.is_simulating
        top.prop(settings, "use_mesh_collision")
        sub = top.column()
        sub.enabled = settings.use_mesh_collision
        sub.prop(settings, "collision_collection", text="Collection")
        sub.prop(settings, "poly_warn_threshold")
        sub.prop(settings, "sync_timeline")

        if not settings.use_mesh_collision:
            return

        coll = settings.collision_collection
        if coll is None:
            layout.label(text="Choose a collection of collider meshes.", icon="OUTLINER_COLLECTION")
            return

        objs = collision_objects(settings)
        layout.label(text="%d collision mesh%s" % (len(objs), "" if len(objs) == 1 else "es"),
                     icon="OUTLINER_COLLECTION")

        # Live high-poly warning (base mesh counts; modifiers may raise them).
        heavy = []
        for o in objs:
            try:
                npoly = len(o.data.polygons)
            except Exception:
                npoly = 0
            if npoly > settings.poly_warn_threshold:
                heavy.append((o.name, npoly))
        if heavy:
            warn = layout.box()
            warn.alert = True
            warn.label(text="High-poly collision (slows the sim):", icon="ERROR")
            for name, npoly in heavy[:4]:
                warn.label(text="  %s — %d polys" % (name, npoly))
            if len(heavy) > 4:
                warn.label(text="  … and %d more" % (len(heavy) - 4))
            warn.label(text="Modifiers can raise the final count further.")

        # Warnings raised when the last simulation started (exact tri counts).
        if settings.collision_warning_text:
            warn = layout.box()
            warn.alert = True
            warn.label(text="Last simulation:", icon="ERROR")
            for part in settings.collision_warning_text.split("  •  "):
                warn.label(text=part)

        # Per-object surface settings for the active object.
        obj = context.active_object
        if obj is not None and obj.type == "MESH" and obj in set(objs):
            box = layout.box()
            box.label(text=obj.name, icon="OBJECT_DATA")
            sc = obj.sonic_collision
            box.prop(sc, "surface_type")
            if sc.surface_type in {"DAMAGE", "SPEED_UP"}:
                box.prop(sc, "trigger_paired")
            if sc.is_trigger_volume:
                box.prop(sc, "trigger_toggle")
                box.label(text="Reads back: Sonic_Trigger_Active,", icon="RNA")
                box.label(text="   Sonic_Player_Inside (custom props)")
            if sc.surface_type == "SPEED_UP":
                box.prop(sc, "boost_mode")
                box.prop(sc, "boost_power")
            elif sc.surface_type == "ICE":
                box.prop(settings, "ice_friction_mult")
            elif sc.surface_type == "WATER":
                box.label(text="Underwater volume (global tuning):", icon="MOD_FLUIDSIM")
                box.prop(settings, "air_time")
                box.prop(settings, "water_gravity_mult")
                box.prop(settings, "water_jump_mult")
            elif sc.surface_type == "QUICKSAND":
                box.label(text="Mash jump to escape (global tuning):", icon="INFO")
                box.prop(settings, "quicksand_sink")
                box.prop(settings, "quicksand_climb")
            box.prop(sc, "dynamic")
        else:
            layout.label(text="Select a mesh in the collection to edit", icon="RESTRICT_SELECT_OFF")
            layout.label(text="   its surface type.")


class SONIC_PT_constants(SonicPanelBase, Panel):
    bl_idname = "SONIC_PT_constants"
    bl_parent_id = "SONIC_PT_main"
    bl_label = "Physics Constants"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sonic_sim
        layout.enabled = not settings.is_simulating
        layout.operator("sonic.reset_constants", icon="LOOP_BACK")

        col = layout.column(align=True)
        col.label(text="Running")
        col.prop(settings, "acceleration")
        col.prop(settings, "deceleration")
        col.prop(settings, "friction")
        col.prop(settings, "top_speed")

        col = layout.column(align=True)
        col.label(text="Air")
        col.prop(settings, "air_acceleration")
        col.prop(settings, "gravity")
        col.prop(settings, "jump_force")
        col.prop(settings, "jump_release_cap")

        col = layout.column(align=True)
        col.label(text="Slopes")
        col.prop(settings, "slope_factor_walk")
        col.prop(settings, "slope_factor_roll_up")
        col.prop(settings, "slope_factor_roll_down")

        col = layout.column(align=True)
        col.label(text="Rolling / Slipping")
        col.prop(settings, "roll_friction")
        col.prop(settings, "roll_deceleration")
        col.prop(settings, "roll_min_speed")
        col.prop(settings, "fall_slip_speed")
        col.prop(settings, "control_lock_time")
        col.prop(settings, "ground_snap_distance")

        col = layout.column(align=True)
        col.label(text="Spindash")
        col.prop(settings, "spindash_charge")
        col.prop(settings, "spindash_max")
        col.prop(settings, "spindash_base_speed")

        col = layout.column(align=True)
        col.label(text="Super Peel Out")
        col.prop(settings, "enable_peelout")
        sub = col.column(align=True)
        sub.enabled = settings.enable_peelout
        sub.prop(settings, "peelout_charge_time")
        sub.prop(settings, "peelout_launch_speed")

        col = layout.column(align=True)
        col.label(text="Damage")
        col.prop(settings, "hurt_gravity")
        col.prop(settings, "invulnerability_time")


ATTR_DISPLAY = [
    ("On_Ground", "In_Air"),
    ("Airstate_Jump", "Airstate_Falling"),
    ("Is_Running", "Is_Jogging"),
    ("Is_Rolling", "Is_Spindashing"),
    ("Is_Ducking", "Is_Skidding"),
    ("Is_Peelout_Charging", "Is_Pushing"),
    ("Is_Hurt", "Is_Invulnerable"),
    ("Is_Boosted", "Control_Locked"),
    # extended
    ("Is_Underwater", "Is_On_Ice"),
    ("In_Quicksand", "Is_Dead"),
    ("Is_Flying", "Is_Gliding"),
    ("Is_Climbing", "Is_Hovering"),
    ("Is_Homing", "Is_Boosting"),
    ("Is_DropDash_Charging", "DropDash_Ready"),
    ("Is_Sprung", "Is_Dashing"),
]


class SONIC_PT_objects(SonicPanelBase, Panel):
    bl_idname = "SONIC_PT_objects"
    bl_parent_id = "SONIC_PT_main"
    bl_label = "Objects (Springs / Rings / Badniks)"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        col = layout.column(align=True)
        row = col.row(align=True)
        row.operator("sonic.add_spring", text="Spring", icon="FORCE_HARMONIC")
        row.operator("sonic.add_ring", text="Ring", icon="MESH_TORUS")
        row.operator("sonic.add_monitor", text="Monitor", icon="MESH_CUBE")
        row = col.row(align=True)
        row.operator("sonic.add_motobug", text="Motobug", icon="GHOST_ENABLED")
        row.operator("sonic.add_spikes", text="Spikes", icon="MOD_TRIANGULATE")
        row.operator("sonic.add_bumper", text="Bumper", icon="ANTIALIASED")
        layout.label(text="Also under Add ▸ Sonic Phys (Shift+A).", icon="INFO")

        obj = context.active_object
        if obj is None:
            layout.label(text="Select an object to edit it.", icon="RESTRICT_SELECT_OFF")
            return
        so = obj.sonic_object
        box = layout.box()
        box.label(text=obj.name, icon="OBJECT_DATA")
        box.prop(so, "kind")
        if so.kind == "SPRING":
            box.prop(so, "spring_power")
            box.prop(so, "spring_dir")
            box.prop(so, "interact_radius")
        elif so.kind == "RING":
            box.prop(so, "ring_value")
            box.prop(so, "interact_radius")
        elif so.kind == "BUMPER":
            box.prop(so, "spring_power", text="Bounce Power")
            box.prop(so, "interact_radius")
        elif so.kind == "MONITOR":
            box.prop(so, "ring_value", text="Rings Inside")
            box.prop(so, "interact_radius")
            box.label(text="Break it while attacking.", icon="INFO")
        elif so.kind == "SPIKES":
            box.prop(so, "interact_radius")
            box.label(text="Hurts even while attacking.", icon="ERROR")
        elif so.kind == "MOTOBUG":
            box.prop(so, "interact_radius")
            box.label(text="Destroyed if hit while attacking,", icon="INFO")
            box.label(text="   otherwise it hurts you.")


class SONIC_PT_moves(SonicPanelBase, Panel):
    bl_idname = "SONIC_PT_moves"
    bl_parent_id = "SONIC_PT_main"
    bl_label = "Character Moves"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = context.scene.sonic_sim
        layout.enabled = not s.is_simulating

        col = layout.column(align=True)
        col.prop(s, "enable_spindash")
        col.prop(s, "enable_peelout")
        col.prop(s, "lethal_without_rings")

        box = layout.box()
        box.label(text="Air abilities (jump button in the air):", icon="MOD_PHYSICS")
        box.label(text="priority homing ▸ flight ▸ glide ▸ drop dash ▸ hover")
        col = box.column(align=True)
        col.prop(s, "enable_flight")
        col.prop(s, "enable_gliding")
        col.prop(s, "enable_climbing")
        col.prop(s, "enable_dropdash")
        col.prop(s, "enable_homing")
        col.prop(s, "enable_hovering")
        box.prop(s, "enable_boost")
        box.label(text="Boost is the X button (Q key).", icon="INFO")

        tune = layout.box()
        tune.label(text="Move tuning")
        any_tune = False
        if s.enable_boost:
            tune.prop(s, "boost_speed"); any_tune = True
        if s.enable_dropdash:
            tune.prop(s, "dropdash_speed"); any_tune = True
        if s.enable_homing:
            tune.prop(s, "homing_speed"); tune.prop(s, "homing_range"); any_tune = True
        if s.enable_flight:
            tune.prop(s, "flight_time"); any_tune = True
        if s.enable_gliding:
            tune.prop(s, "glide_speed"); tune.prop(s, "glide_fall_speed"); any_tune = True
        if s.enable_hovering:
            tune.prop(s, "hover_time"); any_tune = True
        if not any_tune:
            tune.label(text="(enable a move to tune it)")


class SONIC_PT_presets(SonicPanelBase, Panel):
    bl_idname = "SONIC_PT_presets"
    bl_parent_id = "SONIC_PT_main"
    bl_label = "Game Presets"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = context.scene.sonic_sim
        layout.enabled = not s.is_simulating

        row = layout.row(align=True)
        row.prop(s, "preset", text="")
        row.operator("sonic.apply_preset", text="", icon="CHECKMARK")
        p = sonic_core.GAME_PRESETS.get(s.preset)
        if p is not None and not p["accurate"]:
            layout.label(text="Approximation — tune to taste.", icon="INFO")
        elif p is not None:
            layout.label(text="Authentic Genesis-era constants.", icon="CHECKMARK")

        if _HAS_PRESET_BASE:
            box = layout.box()
            box.label(text="Your saved presets")
            row = box.row(align=True)
            row.menu("SONIC_MT_user_presets", text=SONIC_MT_user_presets.bl_label)
            row.operator("sonic.add_user_preset", text="", icon="ADD")
            row.operator("sonic.add_user_preset", text="", icon="REMOVE").remove_active = True


class SONIC_PT_tas(SonicPanelBase, Panel):
    bl_idname = "SONIC_PT_tas"
    bl_parent_id = "SONIC_PT_main"
    bl_label = "TAS (Input Recording)"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        s = context.scene.sonic_sim
        layout.prop(s, "tas_record", icon="REC")
        layout.label(text="Records each button per frame onto", icon="INFO")
        layout.label(text="   the player as TAS_* channels.")
        col = layout.column(align=True)
        col.enabled = not s.is_simulating
        col.operator("sonic.playback_tas", icon="PLAY")
        col.operator("sonic.clear_tas", icon="TRASH")


class SONIC_PT_readout(SonicPanelBase, Panel):
    bl_idname = "SONIC_PT_readout"
    bl_parent_id = "SONIC_PT_main"
    bl_label = "Live Attributes"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        settings = context.scene.sonic_sim
        obj = settings.player
        if obj is None:
            layout.label(text="No player")
            return

        def val(key):
            return obj.get(key, None)

        col = layout.column(align=True)
        gs = val("Ground_Speed")
        if gs is not None:
            col.label(text="Ground Speed: %.3f" % gs)
        xv, zv = val("X_Vel"), val("Z_Vel")
        if xv is not None:
            col.label(text="X Vel: %.3f   Z Vel: %.3f" % (xv, zv))
        yv = val("Y_Vel")
        if yv is not None:
            col.label(text="Y Vel (depth): %.3f" % yv)
        ga = val("Ground_Angle")
        if ga is not None:
            col.label(text="Ground Angle: %.1f deg" % ga)
        rc = val("Ring_Count")
        if rc is not None:
            col.label(text="Rings: %d   Badniks: %d" % (rc, val("Badniks_Destroyed") or 0))
        if val("Is_Underwater"):
            at = val("Air_Timer")
            col.label(text="Air: %.1f s" % ((at or 0) / 60.0), icon="MOD_FLUIDSIM")
        if settings.enable_boost:
            be = val("Boost_Energy")
            if be is not None:
                col.label(text="Boost Energy: %.0f" % be)
        dc = val("Sonic_Death_Cause")
        if dc:
            col.label(text="DEAD: %s" % dc, icon="GHOST_DISABLED")
        ti = val("Triggers_Inside")
        if ti is not None:
            col.label(text="Triggers Inside: %d" % ti)

        grid = layout.column(align=True)
        for a, b in ATTR_DISPLAY:
            row = grid.row()
            row.label(text=a, icon="CHECKBOX_HLT" if val(a) else "CHECKBOX_DEHLT")
            row.label(text=b, icon="CHECKBOX_HLT" if val(b) else "CHECKBOX_DEHLT")


# =============================================================================
#  Registration
# =============================================================================
CLASSES = (
    SonicCollisionSettings,
    SonicObjectSettings,
    SonicSimSettings,
    SONIC_OT_add_player,
    SONIC_OT_import_premade,
    SONIC_OT_reset_player,
    SONIC_OT_clear_bake,
    SONIC_OT_reset_constants,
    SONIC_OT_add_spring,
    SONIC_OT_add_ring,
    SONIC_OT_add_motobug,
    SONIC_OT_add_spikes,
    SONIC_OT_add_bumper,
    SONIC_OT_add_monitor,
    SONIC_MT_add,
    SONIC_OT_apply_preset,
    SONIC_OT_simulate,
    SONIC_OT_playback_tas,
    SONIC_OT_clear_tas,
    SONIC_PT_main,
    SONIC_PT_controls,
    SONIC_PT_world,
    SONIC_PT_collision,
    SONIC_PT_objects,
    SONIC_PT_moves,
    SONIC_PT_presets,
    SONIC_PT_tas,
    SONIC_PT_constants,
    SONIC_PT_readout,
)

# User-preset classes only register when Blender's preset base is available.
_PRESET_CLASSES = (SONIC_MT_user_presets, SONIC_OT_add_user_preset) if _HAS_PRESET_BASE else ()


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    for cls in _PRESET_CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.sonic_sim = PointerProperty(type=SonicSimSettings)
    bpy.types.Object.sonic_collision = PointerProperty(type=SonicCollisionSettings)
    bpy.types.Object.sonic_object = PointerProperty(type=SonicObjectSettings)
    bpy.types.VIEW3D_MT_add.append(_sonic_add_menu)


def unregister():
    _disable_overlay()
    try:
        bpy.types.VIEW3D_MT_add.remove(_sonic_add_menu)
    except Exception:
        pass
    del bpy.types.Object.sonic_object
    del bpy.types.Object.sonic_collision
    del bpy.types.Scene.sonic_sim
    for cls in reversed(_PRESET_CLASSES):
        bpy.utils.unregister_class(cls)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)


if __name__ == "__main__":
    register()
