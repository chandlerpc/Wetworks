# Wetworks Floor: art spec for the image AI

Everything the Floor still needs drawn: the cast, and the ops props the Retro Cybercity pack doesn't
have. Made with a pixel-art image AI (PixelLab or Retro Diffusion), saved as PNG sprite sheets into
`wetworks/console/art/custom/`, then wired in.

**Don't upload the Retro Cybercity sheets to the AI.** The license forbids using the pack to train
AI image generators. Describe the style in words (the style block below) instead.

## Style (paste at the start of every prompt)

> Top-down 3/4 view pixel art (like classic RPG interiors: you see the floor from above and the front
> of things). 16 px grid. Crisp pixels, no anti-aliasing, no blur, transparent background. 1 px dark
> navy outline (#141226) around every shape. Limited palette of deep navy, slate, muted teal, plum and
> warm skin tones, with small bright accents (phosphor green #9BE37D, signal red #FF4D3D, amber
> #E8A33D). Light comes from above-front. Gritty, late-night ops center, retro 1990s sci-fi.

Keep every character the same build: chunky big head (about 40% of their height), short body, readable
from far away, like Streets of Rogue.

## Characters

**Frame size: 24 × 32 px.** The figure is about 16 px wide and 24 px tall, standing at the bottom
centre of the frame with its feet on the bottom row (y = 31). Each character gets one sheet:

| Row | What | Frames (columns, 24 px each) |
|---|---|---|
| 1 | facing down (towards camera) | idle 1, idle 2, walk 1–4, work 1–2 |
| 2 | facing up (away; at a desk) | idle 1, idle 2, walk 1–4, work 1–2 |
| 3 | facing left (right is mirrored by code) | idle 1, idle 2, walk 1–4, work 1–2 |
| 4 | lying asleep, seen from above, blanket to the chest | sleep 1, sleep 2 (breathing) |

That's 8 columns × 4 rows = **192 × 128 px**. Idle frames are a tiny breathing bob. "Work" means
typing or handling something at desk height.

| File | Who | Look |
|---|---|---|
| `char_architect.png` | The Architect (overseer) | Slightly taller than the others. Long dark green coat to the knees, high collar with a thin glowing green strip, short dark hair, earpiece with a small mic boom, calm and composed. |
| `char_splinter.png` | Splinter (field agent) | Matte black/slate tactical suit, vest with three pouches, close-fitting helmet, **one** thin horizontal visor band that glows green. Not three-lens goggles: that's another game's signature. |
| `char_intel.png` | Intel (Sleeper) | Navy hoodie with phosphor-green drawstrings, headphones around the neck, messy hair. |
| `char_revenue.png` | Revenue (Sleeper) | Maroon hoodie with amber trim, rolled sleeves, a pencil behind the ear. |
| `char_archivist.png` | The Archivist (Sleeper) | Brown cardigan over a shirt, round glasses, tidy hair. |
| `char_forge.png` | Forge Operator | Leather apron over a dark shirt, welding goggles pushed up on the forehead with orange lenses, heavy gloves. |

**C.L.a.i.R** is different: a small floating mint-green flame wisp with two dot eyes and a soft
glow, no body. `char_clair.png`, frame **24 × 24 px**, one row: idle flicker 1–4, talking 1–4
(the flame brightens and pulses) = **192 × 24 px**.

## Props

Same style block. Sizes are in pixels, and multiples of 16. Animated props are one row of frames.

| File | Room | Size per frame | Frames | Look |
|---|---|---|---|---|
| `prop_gpu_rack.png` | The Forge | 32 × 48 | 3 | Tall server rack holding GPU cards: front grille, two fans spinning across the frames, a vertical strip of status LEDs. |
| `prop_gpu_rack_hot.png` | The Forge | 32 × 48 | 3 | Same rack running hot: red-orange glow from the vents, faster fans, red LEDs. |
| `prop_server_rack.png` | Code Bay | 16 × 48 | 2 | Slim rack, rows of tiny blinking green and amber LEDs. |
| `prop_locker.png` | Ready Room | 16 × 48 | 2 | Tall steel locker; frame 1 closed, frame 2 open with gear inside. |
| `prop_gear_wall.png` | Ready Room | 48 × 32 | 1 | Wall-mounted pegboard: helmet, vest, radio, coiled rope, a flashlight. No guns. |
| `prop_radar.png` | Intel Watch | 48 × 32 | 8 | Radar console: round green scope with a sweep line rotating across the frames, a small keyboard. |
| `prop_bed.png` | Sleepers' rooms | 16 × 32 | 1 | Narrow cot from above: grey frame, pillow, dark blanket. |
| `prop_red_phone.png` | Command Deck | 16 × 16 | 2 | Old red desk phone; frame 2 ringing (handset jiggling, small motion lines). |
| `prop_ops_screen.png` | Command Deck | 64 × 32 | 2 | Big wall display: dark map of a city grid with a few glowing markers, a subtle flicker between frames. |
| `prop_holo_table.png` | Command Deck | 48 × 32 | 4 | Low table projecting a faint green holographic map that shimmers. |
| `prop_lead_board.png` | Revenue Room | 48 × 32 | 1 | Corkboard on the wall with index cards, pins and red string. |
| `prop_comms_desk.png` | Comms Booth | 32 × 32 | 4 | Desk with a studio microphone and a small screen showing an audio waveform that moves. |
| `prop_blast_door.png` | Sealed rooms | 48 × 48 | 5 | Heavy blast door with yellow-black hazard stripes; frame 1 shut, frames 2–5 sliding open (played once when a room unseals). |
| `prop_clutter.png` | Everywhere | 16 × 16 | 8 | One frame each: coffee mug, cable bundle, trash can, stack of papers, energy drink can, crate, floor vent, wet-floor sign. |

## Tips for the image AI

- Generate one character first, adjust until it looks right, then use it as the **reference
  image** for all the others so the build and outline stay identical. Your own generated art is fine
  to use as a reference.
- Most pixel tools have separate modes for "character with directions" and "animation". Make the
  facing-down idle first, then the other directions, then the walk and work frames from that.
- If a sheet comes out at a larger scale (for example each pixel drawn as 4×4), that's fine. Tell
  me the scale and I'll handle it.
- Keep backgrounds transparent. No drop shadows; the Floor adds its own.

## Hand-off

Put the PNGs in `wetworks/console/art/custom/` on the MSI (or send them here) using the file names
above. Partial sets are fine: anything missing keeps its current stand-in.
