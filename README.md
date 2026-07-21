# Sonic Physics — Blender Add-on

Recreate **classic Genesis-era Sonic the Hedgehog physics** inside Blender, on a
controllable empty, with a live "play the game in the viewport" simulate mode and
optional keyframe baking.

Every constant and every rule in the engine is taken **directly from the Sonic 1
disassembly** (`sonicretro/s1disasm`, `_incObj/01 Sonic.asm`) and cross-checked
against the [Sonic Retro Physics Guide](https://info.sonicretro.org/Sonic_Physics_Guide).
The spindash (a Sonic 2 mechanic not present in Sonic 1) uses the Physics
Guide's formula.

- ✅ Accurate acceleration / deceleration / friction / top speed
- ✅ Accurate jump arc **and** variable jump height (release to hop)
- ✅ Slope factors (walking, rolling uphill, rolling downhill)
- ✅ Rolling & the classic spindash (release speeds 8 → 12 px/frame)
- ✅ The **Super Peel Out** (Sonic CD's figure-8 dash)
- ✅ **Getting hurt** — knockback arc, weak "hurt gravity", post-hit invulnerability
- ✅ Air acceleration, air drag near the apex, mid-air angle recovery
- ✅ Slope slipping / control-lock when too slow on a steep slope
- ✅ **Add-menu objects** (Add ▸ Sonic Phys): **Springs**, **Rings**, **Monitors**,
  **Motobugs**, **Spikes**, **Bumpers**
- ✅ Three new **collision types**: **Ice** (low friction), **Water** (underwater
  physics + a drowning air timer), **Quicksand** (sink unless you mash jump)
- ✅ Seven toggleable **character moves**: **Flight** (Tails), **Gliding** &
  **Climbing** (Knuckles), **Drop Dash** (Mania), **Homing Attack**, **Boost**,
  **Hovering** — plus a spin-dash on/off toggle and the classic ringless-hit death
- ✅ **20 game presets** (Sonic 1 → Superstars) + **save your own presets**
- ✅ Optional **TASing** — record every button per frame as keyframes, edit the
  curves, then re-simulate deterministically
- ✅ Optional **curve-as-ground** (the curve is a floor, not gravity — you jump
  off it ballistically), now able to **follow the curve's 3D depth** and yaw to
  face along the path
- ✅ Optional **shape-accurate mesh collision** against any collection of meshes,
  with per-object **surface types** — Walkable, Damage, Trigger, Speed-Up
  (booster), Ice, Water, Quicksand — and **live rebuilding** of animated /
  rigid-body / simulated colliders
- ✅ Optional **animation baking** for timeline playback
- ✅ A bundled **pre-made Sonic model** you can import with one button
- ✅ ~70 live **custom attributes** written to the empty every frame

The physics core has **159 unit tests** (the classic set plus ice, water &
drowning, quicksand, springs, rings & ring-loss, boost, drop dash, flight, glide,
climb, homing, hover, the spin-dash gate, and all 20 presets) and the Blender
layer has headless integration checks (registration, install/enable, both player
types, the full tick → bake → playback pipeline, spindash, curve-ground). The
pure-Python core tests all pass; the Blender layer is compile-checked, executed
under a mock `bpy`, and defensively coded (the sandbox that generated this can't
run Blender itself).

---

## Requirements

- **Blender 3.0 or newer** — the original 1.0 line was verified headlessly on
  both **4.5.0** and **4.2.9 LTS**. (Baking is version-aware: it uses the new
  *slotted Actions* on Blender 4.4+ and legacy Actions on older builds, so
  keyframes always bind to the object.)
- No third-party Python packages. Mesh collision uses Blender's built-in
  `mathutils.bvhtree`; if a stripped-down build lacks it, collision simply
  disables itself and everything else keeps working.

> **This release is 1.2.0.** On top of 1.1's mesh collision, Super Peel Out and
> hurt state, it adds **Add-menu objects** (springs, rings, Motobugs), three new
> **collision types** (ice, water + drowning, quicksand), seven toggleable
> **character moves** (flight, gliding, climbing, drop dash, homing, boost,
> hovering), **20 game presets** plus saveable custom presets, and optional
> **TASing** (record inputs as keyframes, edit, and re-simulate). The 2D physics
> core is covered by **159 unit tests**; the new Blender-side code is
> compile-checked, imports under a mock `bpy`, and written defensively, but hasn't
> been run inside a live Blender at the time of writing — see *Limitations*.

---

## Installation

The add-on ships with a **`blender_manifest.toml`**, so it installs as a modern
**extension** on Blender 4.2+ *and* as a classic add-on on Blender 3.0–4.1 (which
read the legacy `bl_info` instead).

**Blender 4.2 or newer (extension):**
1. Grab **`sonic_physics_addon.zip`**.
2. **Edit ▸ Preferences ▸ Add-ons ▸ Install from Disk…** and pick the zip (or just
   drag-and-drop the zip onto the Blender window).
3. Enable **Sonic Physics** if it isn't already.

**Blender 3.0–4.1 (legacy add-on):**
1. **Edit ▸ Preferences ▸ Add-ons ▸ Install…**, pick **`sonic_physics_addon.zip`**.
2. Tick **Animation: Sonic Physics** to enable it.

Then open the **N-panel** in the 3D Viewport (press `N`) and select the **Sonic**
tab.

> Prefer not to zip-install? You can also drop the `sonic_physics_addon/` folder
> into your Blender `scripts/addons/` directory (legacy) or
> `extensions/user_default/` (4.2+).
>
> On 4.2+ the console may note that `bl_info` is ignored for extensions — that's
> expected. `bl_info` is kept on purpose so the same folder still installs on
> 3.0–4.1.

---

## Quick start

1. In the **Sonic** N-panel, click **Add Sonic Player**. This spawns a cube
   *empty* at the 3D cursor whose **origin is at the feet** — so world **Z = 0 is
   the floor**. (Or click **Import Pre-Made Sonic** to bring in the bundled
   character model; the cube-empty is what the physics actually drive, so keep it
   as your **Player** and parent the model to it if you want the model to follow.)
2. (Optional) Turn on **Bake Animation** in *World & Baking* if you want the run
   recorded as keyframes.
3. Click the big **Simulate** button.
4. Play the game! Use the arrow keys to move, `A` to jump. **Press `Esc` to
   stop.**

> ⚠️ **Simulate mode grabs the entire keyboard and mouse.** Every Blender
> shortcut is disabled until you press **`Esc`** — this is intentional (it stops
> `A` from "select all", `S` from "scale", etc. while you play). If Blender ever
> feels "frozen", it's simulating: press **`Esc`**.

---

## Controls — 6-button SEGA pad

| Keyboard        | SEGA control | Sonic action |
|-----------------|--------------|--------------|
| **Arrow keys**  | D-Pad        | Move / duck / look up |
| **A**           | A button     | Jump (+ air ability) |
| **S**           | B button     | Jump (+ air ability) |
| **D**           | C button     | Jump (+ air ability) |
| **Q**           | X button     | **Boost** (when enabled) |
| **W**           | Y button     | (spare) |
| **E**           | Z button     | (spare) |
| **Enter**       | Start        | (spare) |
| **Esc**         | —            | **End simulation** |

Jump is **A / B / C** (any). The optional **air abilities** (flight, gliding,
drop dash, homing, hover) are all triggered with the jump button **while
airborne**; **Boost** is the **X** button (`Q`). Y / Z / Start are tracked as
attributes but are free for you to wire up to your own logic.

### Moveset

- **Run** — hold Left/Right. Accelerates to a 6 px/f top speed; slopes,
  spindashes, Peel Outs and boosters can push you faster, and that extra
  momentum is **kept** while you keep holding the direction (input only
  accelerates you *up to* the top speed, it never brakes you back down to it).
- **Skid** — press the opposite direction at speed to brake hard.
- **Jump** — tap A/B/C. **Hold** for a full jump, **release early** for a short
  hop (variable height). Jumps leave the ground *perpendicular to the slope*.
- **Roll** — hold **Down** while moving (≥ 0.5 px/f). You keep momentum, can't
  accelerate, and use gentler friction.
- **Spindash** (Sonic 2) — stand still, hold **Down**, and **tap A/B/C**
  repeatedly to charge, then **release Down** to launch (8–12 px/f depending on
  charge). Launches you *rolling*.
- **Super Peel Out** (Sonic CD) — stand still, hold **Up**, and **tap A/B/C** to
  start revving; keep **Up** held to charge (30 frames), then **release Up** to
  launch at full running speed (12 px/f). A partial charge fizzles with no
  launch. Launches you *running* (upright and vulnerable), unlike the spindash.

> Because Sonic CD binds the Peel Out to *Up + jump*, enabling it means **Up +
> jump no longer performs a plain jump** while standing still. Turn
> **Enable Super Peel Out** off in *Physics Constants* to get the plain jump back.

### Optional character moves (Character Moves panel)

All of these are **off by default** — toggle the ones your character should have.
The **spin dash** is on by default; turn it **off** for Sonic 1 / Sonic CD.

- **Flight** (Tails) — in the air, **tap** a jump button to flap and gain height.
  Gravity is floaty while the flight timer (8 s by default) lasts; when it runs
  out Tails tires and falls until he lands.
- **Gliding** (Knuckles) — **hold** a jump button in the air to glide: upward
  motion is cancelled, you descend slowly, and you build/steer forward speed.
- **Climbing** (Knuckles) — glide into a wall (needs *Mesh Collision*) to cling
  on; **Up/Down** climb, a jump leaps off. 
- **Drop Dash** (Mania) — **hold** the jump button after jumping to charge; the
  moment you land you launch into a rolling dash.
- **Homing Attack** — in the air, **press** a jump button to dash at the nearest
  **Motobug** within range; destroying it bounces you so you can chain hits.
- **Boost** — hold the **X** button (`Q`) to hold a high speed while a boost meter
  drains (and refills when you're not boosting).
- **Hovering** — **hold** a jump button at the apex to hang in the air briefly.

> Flight, gliding, drop dash, homing and hovering all use the **jump button in
> the air**, so a character normally has just one. If you enable several, a fixed
> priority decides which runs: **homing ▸ flight ▸ glide ▸ drop dash ▸ hover**.

See **[Game presets](#game-presets)** to enable the right moves for a given game
in one click, and **[Add-menu objects](#add-menu-objects--springs-rings-motobugs)**
for the springs, rings and badniks these moves interact with.

---

## The N-panel

**Sonic Physics** (main)
- **Add Sonic Player** + the player-object type selector.
- **Import Pre-Made Sonic** — appends the bundled `SonicTheHedgehog` collection
  from `Sonic.blend` (shipped inside the add-on) into the current scene, as a
  local copy. Greyed out if the blend isn't present.
- **Player** — which object the simulation drives.
- **Simulate** / **Reset** / **Clear Bake**.

**Controls** — a quick reference card of the keymap.

**World & Baking**
- **Blender Units / Pixel** — size/position scale (see *Units* below).
- **Use Curve As Ground** + **Ground Curve** — see below.
  - **Follow Curve Depth (3D)** — track the curve's bends in plan view (Y depth),
    not just its X/Z profile.
  - **Rotate Along Path (Yaw)** — turn the player to face along the path.
- **Bake Animation** / **Bake Attributes** / **Force Scene to 60 FPS**.
- **Draw Collision Overlay** — draws the true collision box and sensors while
  simulating (the box turns **red** while the player is hurt).
- **Simulation FPS** — tick rate (Sonic is 60).

**Mesh Collision** — shape-accurate collision against a collection of meshes:
- **Mesh Collision** — master on/off.
- **Collection** — only meshes inside this collection collide.
- **Poly Warning Threshold** — the panel flags colliders heavier than this.
- **Advance Timeline (Live Objects)** — steps the scene frame while simulating so
  animated / rigid-body / simulated colliders actually move (the frame is
  restored afterwards).
- With a mesh in the collection **selected**, a per-object box appears to set its
  **Surface Type** and options (see *Mesh collision* below).

**Objects (Springs / Rings / Badniks)** — buttons to add a Spring, Ring or
Motobug, plus a per-object editor for the selected object's **Sonic Object**
settings (kind, power/direction/value, touch radius). See *Add-menu objects*.

**Character Moves** — the spin-dash and Peel-Out toggles, the **Ringless Hit Is
Fatal** switch, the seven optional air/boost abilities, and tuning fields for
whichever moves you've enabled. See *Optional character moves*.

**Game Presets** — the game dropdown (applies on pick), an **Apply** button, an
authentic/approximate note, and the **Save / remove** controls for your own
presets. See *Game presets*.

**TAS (Input Recording)** — the **Record Inputs** toggle plus **Play Back TAS**
and **Clear TAS Channels**. See *TASing*.

**Physics Constants** — every tunable value, defaulting to authentic Sonic 1.
Includes a **Reset To Sonic 1 Defaults** button, plus **Super Peel Out**
(enable, charge time, launch speed) and **Damage** (hurt gravity, invulnerability
length) groups.

**Live Attributes** — a readout of the key state values and flags (rings, badniks,
air timer, boost energy, death cause, and all the state checkboxes; updates as you
simulate, and reflects the current frame during baked playback).

---

## Curve as ground

Enable **Use Curve As Ground** and pick a **Curve** object. While grounded, the
character follows the curve and tilts to its slope; **when it jumps it follows
gravity, not the curve** — exactly as requested. Without a curve, the ground is a
flat plane at **Z = 0**.

There are two modes:

**Flat (Follow Curve Depth off)** — the classic behaviour. The curve's **X/Z
profile** is the floor (a *height-field*: one Z per X). Draw the curve in **Front
view**; its Y (depth) is ignored. Keep it single-valued in X (no vertical walls
or overhangs — those can't be a height-field floor).

**3D depth (Follow Curve Depth on, the default)** — the player follows the curve
**through 3D space**, so a path that snakes in depth (Y) carries the character
with it. The 2D physics run along the curve's **horizontal arc length** — i.e.
distance measured in top-down plan view — so a run feels identical whether the
track is straight or winding, and climbing a hill still costs no horizontal
distance. The path may even double back in world X. Turn on **Rotate Along Path**
to yaw the player to face along the track (the yaw is written to the `Path_Yaw`
attribute either way). The player's depth motion each frame is reported as
`Y_Vel`.

For 3D depth the curve must tessellate to **one connected, unbranched strand**
(a single open or closed spline). Branching or multi-spline curves can't be
walked as one path, so the add-on falls back to the flat X/Z sampler and warns
in the panel.

The curve is re-sampled at the **start of every simulation**, so you can edit it
and simulate again. (It is *not* re-read mid-run, so an animated curve won't
reshape the floor while you play — - use mesh collision for moving ground.)

---

## Mesh collision

Turn on **Mesh Collision** and choose a **Collection**. Every mesh in that
collection becomes solid, collided against **accurately to its shape** — the
add-on ray-casts against the object's *evaluated* triangles (all modifiers
applied) using a BVH tree, so subdivision surfaces, arrays, booleans, etc. all
collide as they look. Collision runs in the same 1D→3D frame as curve-following,
so you can lay meshes along a 3D path too.

Select a collider in the collection to give it a **Surface Type** in the panel:

- **Walkable** (default) — plain solid ground, walls and ceilings. Floors you can
  stand and run on (up to ~55°), walls that stop you and set `Is_Pushing`,
  ceilings that stop an upward jump.
- **Damage** — hurts the player on contact (spikes, hazards): the classic
  knock-back arc, then invulnerability. Damage on a *wall/floor* face hits on
  touch.
- **Trigger** — **passthrough** (not solid). It reports when the player is inside
  by writing two custom properties **onto the trigger object**:
  `Sonic_Trigger_Active` and `Sonic_Player_Inside` (both 0/1). Use them to drive
  doors, cameras, cutscenes, spawns — anything.
  - **Toggle (Stay Active)** — *on*: once entered, `Sonic_Trigger_Active` stays 1
    for the rest of the run even after the player leaves (a latch). *off*: it is 1
    only while the player is actually inside.
- **Speed Up** — a **booster** pad: it **sets** the player's speed (it never
  stacks — if you're already faster in the boost direction, nothing happens).
  - **Boost Direction** — *Facing* (whichever way the player currently faces),
    or force *Left* / *Right* along the path. It also turns the player to face
    that way.
  - **Boost Power** — the speed it sets, in px/f (classic boosters use **16**).
- **Ice** — a **solid** floor with greatly reduced friction and deceleration, so
  you slide (the multiplier is tunable inline; 0.25 by default).
- **Water** — a **passthrough** volume that gives **underwater physics**: halved
  acceleration/top speed, much lower (floaty) gravity, and a weaker jump. It also
  starts an **air timer** (30 s by default) that counts down while you're under;
  at zero you **drown** (`Is_Dead`, `Sonic_Death_Cause = "drowned"`). Leaving the
  water refills your air. Entering makes a small splash that halves your speed.
- **Quicksand** — a **passthrough** volume you **sink** into slowly; **mash the
  jump button** to climb back out. Sink out of the *bottom* of the volume and you
  die (`Sonic_Death_Cause = "quicksand"`).

**Pairing Damage / Speed-Up with Trigger.** Normally Damage and Speed-Up are
solid faces you touch. Tick **Passthrough Trigger** on either one to make it a
volume you pass *through* that applies its effect (hurt / boost) the whole time
you're inside — e.g. a hazard cloud, or a speed field. (**Water** and
**Quicksand** are always passthrough volumes; **Ice** is always solid.) A plain
**Trigger** has no contact effect; it only reports. Only one surface type is
active at a time, except this Trigger pairing.

**Moving colliders.** Each object has a **Rebuild** mode:

- **Auto Detect** (default) — the object is rebuilt every frame if it *looks*
  animated: keyframes/drivers, a rigid body, constraints, a parent, or a
  cloth / soft-body / armature-style modifier. Otherwise it's built once.
- **Static** — built once at the start (fastest).
- **Every Frame** — always rebuilt (moving platforms, simulations, anything Auto
  misses).

For animated or simulated colliders to actually move during a live sim, keep
**Advance Timeline (Live Objects)** on so the scene frame steps with the
simulation. Dynamic colliders are re-evaluated from the scene every frame, so
high-poly moving meshes are the main performance cost — the panel warns when a
collider exceeds the **Poly Warning Threshold**, and again (with exact triangle
counts) when a simulation starts.

---

## Add-menu objects — Springs, Rings, Monitors, Motobugs, Spikes, Bumpers

Under **Add ▸ Sonic Phys** (Shift+A), or the buttons in the **Objects** panel,
you can drop six gameplay objects into the scene. Each is a normal mesh tagged
with an **`obj.sonic_object`** setting; the simulation reacts to them by proximity
(a tunable **Touch Radius**, in pixels), so they do **not** need to be in the mesh
collision collection.

- **Spring** — launches the player on contact. Set its **Power** (yellow = 10,
  red = 16) and **Direction** (Up, Up+Forward, Up+Back, Forward, Back, Down; the
  direction is in *path space*). A vertical spring keeps your horizontal
  momentum; a horizontal one sets it.
- **Ring** — collected on contact: it adds to the ring count (**Ring Value**,
  default 1) and hides for the rest of the run. Getting hit **scatters** all your
  rings (`Rings_Lost`). With **Ringless Hit Is Fatal** on (Character Moves panel),
  a hit while you hold **zero** rings is deadly — the classic rule.
- **Motobug** — a badnik. If you touch it **while attacking** (rolling, jumping,
  spin/drop-dashing, homing, gliding or boosting) it's destroyed and you bounce
  off it (chaining into another homing hit); otherwise it **hurts** you. Destroyed
  badniks count into `Badniks_Destroyed`. With **Homing Attack** enabled, the
  nearest Motobug in range is what your homing dash locks onto.
- **Spikes** — a hazard that **hurts on contact even while attacking** (rolling or
  jumping onto them still hurts; only post-hit invulnerability protects you), just
  like the classic games. Unlike a Motobug, spikes are never destroyed.
- **Bumper** — a pinball bumper that **bounces the player away from its centre**
  (omnidirectional) at a tunable **Bounce Power**. Great for Casino/Spring-Yard
  style setups.
- **Monitor** — an item box. **Break it while attacking** to collect its **Rings
  Inside** (default 10) and bounce off it; walk into it without attacking and
  nothing happens.

You can turn any object into one of these (or back to *None*) with the **Sonic
Object** dropdown in the Objects panel while it's selected.

---

## Game presets

The **Game Presets** panel loads a whole game's feel — physics constants **and**
which moves are enabled — in one click. Pick a game from the dropdown (it applies
immediately; the **✓** button re-applies it):

Sonic 1, Sonic 1 (Game Gear), Sonic CD, Sonic 2, Sonic 2 (Game Gear), Sonic 3,
Sonic Blast, Sonic Advance, Sonic Advance 2, Sonic Advance 3, Sonic Rush, Sonic
Rush Adventure, Sonic Colors (DS), Sonic 4 Ep. I, Sonic 4 Ep. II, Sonic
Generations (Console), Sonic Generations (3DS), Sonic Mania, Sonic Forces, Sonic
Superstars.

> **Authentic vs approximate.** The **Mega Drive / Genesis-era** titles — Sonic 1,
> 2, 3, CD, Mania and Superstars — use the documented disassembly / Physics-Guide
> constants and are labelled **authentic**. Everything else (the 8-bit Game Gear
> titles, Blast, the Dimps handhelds — Advance / Rush / Colors — and the modern
> boost games — Sonic 4 / Generations / Forces) is a **clearly-labelled
> approximation**: those engines aren't documented at the sub-pixel level, so the
> presets are hand-tuned to feel roughly right and to switch on the abilities each
> game is known for (e.g. Rush/Colors/Generations/Forces enable **Boost** +
> **Homing**; Mania/Superstars enable the **Drop Dash**; CD enables the **Peel
> Out**; Sonic 1 turns the **spin dash off**). Treat them as a starting point.

**Save your own presets.** Set the constants and moves however you like, then use
**Save Settings As Preset** (the **＋** in the *Your saved presets* box) to store
them as a named Blender preset. They reappear in the menu next to the **－**
(remove) button. (This uses Blender's standard preset system; on the rare build
without it, the save UI is simply hidden.)

---

## TASing — record & replay inputs

Turn on **Record Inputs (TAS)** in the **TAS** panel, then **Simulate** as normal.
Every button is keyframed **per frame** onto the player as **`TAS_*` channels**
(`TAS_Left`, `TAS_Right`, `TAS_Up`, `TAS_Down`, `TAS_A` … `TAS_Start`), with
**constant** interpolation, alongside the usual motion bake.

Because the inputs are ordinary F-curves, you can **edit them** in the Graph
Editor / Dope Sheet — nudge a jump a frame earlier, extend a held direction,
delete a mistake — and then press **Play Back TAS**. That re-runs the physics
**deterministically** from the (edited) `TAS_*` curves, through the same terrain,
mesh collision, volumes and objects, and re-bakes the resulting motion. **Clear
TAS Channels** removes the recording.

This gives you a simple tool-assisted workflow: perform a rough run, then refine
it frame-by-frame on the curves until it's frame-perfect.

---



With **Bake Animation** on, the whole run is recorded and, when you press `Esc`,
written to the player as keyframes (a fresh `SonicBake` action):

- **Location** and **Rotation** are always baked.
- **Bake Attributes** additionally keyframes every state attribute, so drivers /
  geometry-nodes / shaders can read `On_Ground`, `Spindash_Revs`, `X_Vel`, … on
  playback.
- **Force Scene to 60 FPS** sets the scene frame-rate so playback timing matches
  the game.

The scene frame range is extended to cover the bake, and the playhead returns to
the start. Use **Clear Bake** to remove it, or **Reset** to send the player back
to where the run began.

With baking **off**, Simulate is purely live — the empty moves in real time and
nothing is keyframed.

---

## Attributes written every frame

The player empty receives these custom properties (visible under **Object
Properties ▸ Custom Properties**, and animatable when baked). Booleans are stored
as 0/1.

**Input**
`Is_Holding_Left`, `Is_Holding_Right`, `Is_Holding_Up`, `Is_Holding_Down`,
`Button_A`, `Button_B`, `Button_C`, `Button_X`, `Button_Y`, `Button_Z`,
`Button_Start`

**State**
`On_Ground`, `In_Air`, `Airstate_Jump`, `Airstate_Falling`, `Is_Jumping`,
`Is_Rolling`, `Is_RollJumping`, `Is_Running`, `Is_Jogging`, `Is_Dashing`,
`Is_Skidding`, `Is_Braking`, `Is_Ducking`, `Is_LookingUp`, `Is_Pushing`,
`Is_Spindashing`, `Spindash_Revs`, `Control_Locked`, `Control_Lock_Timer`,
`Facing_Right`, `Facing`

**Super Peel Out**
`Is_Peelout_Charging`, `Peelout_Charge_Frames`, `Peelout_Ready`

**Damage / invulnerability**
`Is_Hurt`, `Is_Invulnerable`, `Invulnerability_Timer`, `Hits_Taken`

**Boosters & path** (the last two written by the Blender layer)
`Is_Boosted`, `Path_Yaw` (degrees, the curve heading), `Triggers_Inside` (how
many trigger volumes currently contain the player)

**Rings / water / quicksand / death**
`Ring_Count`, `Rings_Lost`, `Badniks_Destroyed`, `Is_Underwater`, `Air_Timer`
(frames of air left), `In_Quicksand`, `Is_On_Ice`, `Is_Dead`, `Is_Sprung`.
The death reason is written as a **string** `Sonic_Death_Cause` (`"drowned"`,
`"quicksand"` or `"hit"`).

**Character moves**
`Is_Flying`, `Flight_Timer`, `Is_Gliding`, `Is_Climbing`, `Is_DropDash_Charging`,
`DropDash_Ready`, `Is_Homing`, `Is_Boosting`, `Boost_Energy`, `Is_Hovering`

**TAS** (only when *Record Inputs* is on)
`TAS_Left`, `TAS_Right`, `TAS_Up`, `TAS_Down`, `TAS_A`, `TAS_B`, `TAS_C`,
`TAS_X`, `TAS_Y`, `TAS_Z`, `TAS_Start` (0/1 per frame, constant interpolation)

Trigger volumes additionally get `Sonic_Trigger_Active` and `Sonic_Player_Inside`
written onto **the trigger object** (not the player).

**Velocity** (in **pixels/frame**, the authentic Sonic magnitudes)
`X_Vel`, `X_Vel_Absolute`, `Y_Vel`, `Y_Vel_Absolute`, `Z_Vel`, `Z_Vel_Absolute`,
`Ground_Speed`, `Ground_Speed_Absolute`, `Ground_Angle` (degrees)

> Axis mapping: **X** = horizontal (Blender X), **Z** = vertical (Blender Z),
> **Y** = depth (Blender Y). `X_Vel` is the world horizontal velocity;
> `Ground_Speed` is Sonic's *inertia* along the slope. On flat ground they match;
> on a slope they differ. `Y_Vel` is **0** in the flat 2D sim, but when
> **Follow Curve Depth** is on it reports the player's real depth motion as the
> path winds in Y.

---

## Physics constants (authentic values)

All values are per-frame at 60 FPS, in pixels (the Genesis stored them as 8.8
fixed-point "subpixels"; these are `rawValue / 256`).

| Constant                | Value       | Disasm immediate |
|-------------------------|-------------|------------------|
| Acceleration            | `0.046875`  | `$C`   |
| Deceleration            | `0.5`       | `$80`  |
| Friction                | `0.046875`  | `$C`   |
| Top speed               | `6.0`       | `$600` |
| Air acceleration        | `0.09375`   | `$18` (2×) |
| Gravity                 | `0.21875`   | `$38`  |
| Jump force              | `6.5`       | `$680` |
| Jump release cap        | `4.0`       | `$400` |
| Slope factor (walk)     | `0.125`     | `$20`  |
| Slope factor (roll ↓)   | `0.3125`    | `$50`  |
| Slope factor (roll ↑)   | `0.078125`  | `$50 / 4` |
| Roll friction           | `0.0234375` | `$C / 2` |
| Roll deceleration       | `0.125`     | `$80 / 4` |
| Roll / slip threshold   | `0.5` / `2.5` | `$80` / `$280` |
| Global speed cap        | `16.0`      | `$1000` |
| Control-lock time       | `30 frames` | 30 |
| Spindash release        | `8 + floor(revs)/2` (max 12) | Sonic 2 / SPG |
| Peel Out charge         | `30 frames` | Sonic CD |
| Peel Out launch         | `12.0`      | Sonic CD (== a full spindash) |
| Hurt knockback (x / z)  | `2.0` / `4.0` | `$200` / `$400` |
| Hurt gravity            | `0.1875`    | `$30`  |
| Invulnerability         | `120 frames` | `$78`  |

Everything is editable in the **Physics Constants** panel; **Reset To Sonic 1
Defaults** restores them.

---

## Units & scale

The simulation runs internally in **Sonic pixels/frame** so the feel is
frame-perfect and scale-independent. **Blender Units / Pixel** (default `0.05`)
maps pixels onto Blender units *for display only*:

- At `0.05`, the player is ≈ 1.9 units tall, top speed ≈ 0.3 units/frame, and a
  full jump rises ≈ 4.8 units.
- Velocity **attributes** are always reported in authentic px/frame regardless of
  this scale (so `X_Vel` reads `6.0` at top speed).

---

## "Origin at the bottom of the cube"

The player's **origin is its feet** so that **Z = 0 is the floor**. Two object
types are offered (in the main panel):

- **Cube Empty** (default) — a literal cube-display *empty*. Blender always draws
  an empty-cube *centred* on its origin, so its wireframe straddles the floor; the
  origin itself is at the feet, and the **Draw Collision Overlay** shows the real
  box rising from the origin (plus the ground/wall sensors) while you simulate.
- **Wire Mesh Cube** — a real wireframe cube whose **origin is exactly the bottom
  face**, so it visibly sits on the floor. Choose this if you want the literal
  origin-at-the-base cube.

Either way the object *is* the collision volume, and the physics treats the
origin as the feet.

---

## Limitations

Honest boundaries of what this add-on does, so nothing surprises you:

- **No 4-mode wall sensors → no loops or ceiling-running.** Real Sonic swaps
  which sensors are "the floor" as the angle passes 45° / 135° / 225°, which is
  what lets him run loops, walls and ceilings. This engine keeps the floor
  underneath, so it handles **mesh slopes up to ~55°** and overhang ceilings, but
  a full loop or a wall-run isn't simulated — the character would come off the
  wall as it passes vertical.
- **Curve ground is sampled once, at the start of a run.** Editing the curve
  between runs works; an *animated* curve won't reshape the floor mid-simulation.
  For moving ground, use **mesh collision** with a dynamic collider instead.
- **Horizontal moving platforms don't carry the player yet.** A mesh that moves
  **vertically** pushes the player correctly (the floor rises under the feet); a
  platform sliding **sideways** won't drag them along with it.
- **Trigger "fully inside" detection likes closed meshes.** Overlap with the
  player's box is exact, but the *fully-enclosed* fallback ray-casts for a back
  face, so open/non-manifold trigger meshes are most reliable when the player's
  box actually overlaps their surface.
- **Trigger / boost/ hurt states are written live, not baked.** `Sonic_Trigger_Active`
  and `Sonic_Player_Inside` are set on trigger objects during the live sim; they
  aren't keyframed onto those objects for playback (the *player's* attributes,
  including `Is_Hurt`, `Is_Boosted`, `Triggers_Inside`, are baked as usual).
- **Ceiling clearance isn't checked when unrolling.** As in the original games,
  standing up from a roll doesn't test for a low ceiling.
- **Enabling the Super Peel Out replaces the standing Up+jump.** That's authentic
  to Sonic CD; toggle it off if you want a plain jump from Up + jump.
- **Non-Genesis presets are approximations.** Only the Mega Drive / Genesis-era
  titles (Sonic 1, 2, 3, CD, Mania, Superstars) use documented constants. The
  8-bit, Advance, Rush, Colors, Sonic 4, Generations and Forces presets are
  clearly labelled hand-tuned approximations — a starting point, not exact
  reproductions of those engines.
- **The optional moves are simplified.** Flight, gliding, climbing, drop dash,
  homing, boost and hovering capture each ability's *feel* and inputs, but they're
  not frame-exact recreations of any particular game, and their default numbers
  are tunable rather than disassembly-derived. Climbing in particular needs
  **mesh-collision walls** to cling to.
- **Springs / rings / badniks react by proximity.** Contact is a distance check
  against each object's **Touch Radius**, not full mesh overlap, and destroyed /
  collected objects are hidden live and restored when the run ends (the hiding
  isn't keyframed). `Ring_Count`/`Badniks_Destroyed` *are* baked on the player.
- **TAS playback re-runs the physics, not the whole editor session.** It replays
  the recorded/edited `TAS_*` curves through the same terrain, collision, volumes
  and objects and re-bakes the motion; it's deterministic, but only as faithful as
  those inputs. Animated colliders still advance the timeline during playback when
  *Advance Timeline* is on.
- **The Blender-side collision code hasn't been run in a live Blender here.** The
  2D physics core is covered by **159 passing unit tests**, and the whole add-on
  compiles *and imports* under a mock `bpy`, but the BVH collision / curve-depth /
  object / volume paths were written and checked in an environment without
  Blender. Treat this release's Blender integration as "should work, please shake
  it out."

---

## Troubleshooting

- **"Blender is frozen / my shortcuts don't work."** You're in Simulate mode,
  which disables all input by design. Press **`Esc`**.
- **Player falls through / floats.** Check **Blender Units / Pixel** matches your
  scene scale, and that a curve ground (if used) is a clean height-field. The
  **Ground Snap (px)** constant controls how hard the character sticks to convex
  slopes before launching off ramps.
- **Mesh collision does nothing.** Confirm **Mesh Collision** is on, a
  **Collection** is chosen, and it actually contains **mesh** objects (the player
  and its children are always excluded). The panel shows how many colliders it
  found.
- **The sim is sluggish with collision on.** A dynamic collider is rebuilt every
  frame — watch the **poly warning**. Set colliders that don't move to **Static**,
  and prefer lower-poly collision meshes.
- **A moving collider doesn't move during the sim.** Keep **Advance Timeline
  (Live Objects)** on so the scene frame steps with the simulation, and make sure
  the object's **Rebuild** isn't forced to *Static*.
- **Up + jump won't plain-jump.** The Super Peel Out owns that combo; turn
  **Enable Super Peel Out** off in *Physics Constants*.
- **Nothing bakes.** Make sure **Bake Animation** was on *before* you pressed
  Simulate; the keyframes are written when you press `Esc`. (On Blender 4.4+ the
  bake targets the object's Action *slot* so playback works — v1.0.0 had a bug
  here that produced slot-less curves; use v1.0.1+.)
- **No collision overlay.** It only draws during Simulate and needs the `gpu`
  module; physics still works without it.

---

## Running the tests

```bash
# Pure-Python physics (no Blender needed):
python3 tests/test_sonic_core.py

# Headless Blender integration (needs a Blender executable):
blender --background --python tests/blender_smoke.py    # register / install / ops
blender --background --python tests/blender_smoke2.py   # tick -> bake -> playback
```

---

## Project layout

```
sonic_physics_addon/
    blender_manifest.toml  Blender 4.2+ extension manifest (metadata; ignored on 3.0-4.1)
    __init__.py      Blender layer: panels, operators, modal simulate, baking, overlay
    sonic_core.py    Pure-Python physics engine (no Blender deps, fully unit-tested)
    Sonic.blend      Bundled pre-made character (the SonicTheHedgehog collection)
tests/
    test_sonic_core.py   159 physics unit tests (core + peel out, hurt, boosters,
                         collision world, ice/water/quicksand, springs/rings,
                         boost/dropdash/flight/glide/climb/homing/hover, presets)
    blender_smoke.py     registration / install / operators / panels
    blender_smoke2.py    operator tick -> history -> bake -> playback pipeline
sonic_physics_addon.zip  ready-to-install add-on (v1.2.1)
README.md
```

---

## Sources

- **Sonic 1 disassembly** — <https://github.com/sonicretro/s1disasm>
  (`_incObj/01 Sonic.asm`, `_incObj/sub ObjectFall & SpeedToPos.asm`).
- **Sonic Physics Guide** — <https://info.sonicretro.org/Sonic_Physics_Guide>.

## License

Dual-licensed: **GPL-3.0-or-later OR MIT** — you may use the code under either
license (see the `SPDX-License-Identifier` headers in the `.py` files).

The Blender Extensions Platform requires add-ons (which use the `bpy` API) to be
presented as GPL — the license Blender itself ships under — and its manifest
validator accepts only a single supported SPDX identifier, so
`blender_manifest.toml` declares `SPDX:GPL-3.0-or-later`. That's just the option
the packaged extension is distributed under; the permissive MIT grant still
stands for the source.
