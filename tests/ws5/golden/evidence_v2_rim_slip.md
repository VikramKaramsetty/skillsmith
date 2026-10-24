# Failure report: `stack` v0 failed on the test world

**Failure class:** `SLIP_IN_CARRY` (skill, confidence 0.90) in phase **carry** at call #6

**What happened:** grasp at z=0.07 (88% of cup height) held, lost during a 16 cm sideways move

**Look at:** grip height

## Phases
| phase | ok | calls | detail | frame |
|---|---|---|---|---|
| approach | ✓ | #1–#3 | hovered 11 cm above blue cup |  |
| grasp | ✓ | #4–#4 | closed at z=0.070, holding | /frames/real/grasp.jpg |
| lift | ✓ | #5–#5 | lifted 11 cm, holding |  |
| **carry** ← | ✗ | #6–#6 | lost blue cup during 16 cm sideways move (grip at 88% of cup height) | /frames/real/carry.jpg |

## Task
**Task:** stack the blue cup on the red cup

**Params:** `{'top': 'blue cup', 'bottom': 'red cup'}`

**Success check:** `on_top(blue cup, red cup)`

**Why it failed:** SLIP_IN_CARRY: grasp at z=0.07 (88% of cup height) held, lost during a 16 cm sideways move

**Camera check:** FAIL: blue cup is 16.0 cm from red cup in xy (limit 3.0 cm)

**Safety:** all safety checks passed

## Key frames
Annotated: boxes + labels, target crosshair, gripper tip, error arrow in cm.
![carry: lost blue cup during 16 cm sideways move (grip at 88% of cup height)](/frames/real/carry.jpg)
![grasp: closed at z=0.070, holding](/frames/real/grasp.jpg)
Cropped before / after around the objects in the success check:
![before](/frames/real/crop_before.jpg)
![after](/frames/real/crop_after.jpg)

## Expected vs actual
### Object positions: what the success check wanted vs where they ended up
| object | wanted | expected (x, y, z) | actual (x, y, z) | xy error cm | z error cm |
|---|---|---|---|---|---|
| blue cup | on top of red cup | (0.220, 0.080, 0.080) | (0.220, -0.080, 0.000) | 16.0 | -8.0 |

### This skill passed on fake (v0) but failed on fake
| phase | expected (fake) | actual (fake) |
|---|---|---|
| approach | ✓ hovered 11 cm above blue cup | ✓ hovered 11 cm above blue cup |
| grasp | ✓ closed at z=0.070, holding | ✓ closed at z=0.070, holding |
| lift | ✓ lifted 11 cm, holding | ✓ lifted 11 cm, holding |
| carry | ✓ blue cup held through 16 cm sideways move | ✗ lost blue cup during 16 cm sideways move (grip at 88% of cup height) |
| place | ✓ descended onto red cup | — (not reached) |
| release | ✓ opened, blue cup stayed | — (not reached) |
![expected carry (fake)](/frames/sim/carry.jpg)
![actual carry (fake)](/frames/real/carry.jpg)
![expected release (fake)](/frames/sim/release.jpg)
_no fake frame for release_

| object | end (fake) | end (fake) | apart cm |
|---|---|---|---|
| blue cup | (0.220, 0.080, 0.080) | (0.220, -0.080, 0.000) | 17.9 |

## Trace
| # | call | args | tip before | tip after | ok | result / error |
|---|---|---|---|---|---|---|
| 0 | note | text=stacking blue cup on red cup | (0.180, 0.000, 0.150) | (0.180, 0.000, 0.150) | ✓ |  |
| 1 | release |  | (0.180, 0.000, 0.150) | (0.180, 0.000, 0.150) | ✓ |  |
| 2 | move_to | x=0.220, y=-0.080, z=0.180 | (0.180, 0.000, 0.150) | (0.220, -0.080, 0.180) | ✓ |  |
| 3 | move_to | x=0.220, y=-0.080, z=0.070 | (0.220, -0.080, 0.180) | (0.220, -0.080, 0.070) | ✓ |  |
| 4 | grasp |  | (0.220, -0.080, 0.070) | (0.220, -0.080, 0.070) | ✓ | True |
| 5 | move_to | x=0.220, y=-0.080, z=0.180 | (0.220, -0.080, 0.070) | (0.220, -0.080, 0.180) | ✓ |  |
| 6 | move_to | x=0.220, y=0.080, z=0.180 | (0.220, -0.080, 0.180) | (0.220, 0.080, 0.180) | ✓ | **← failed here** |

## Scene before
| label | x | y | z (base) | top | width |
|---|---|---|---|---|---|
| blue cup | 0.220 | -0.080 | 0.000 | 0.080 | 0.065 |
| red cup | 0.220 | 0.080 | 0.000 | 0.080 | 0.065 |
| green block | 0.150 | 0.000 | 0.000 | 0.030 | 0.030 |

## Scene after
| label | x | y | z (base) | top | width |
|---|---|---|---|---|---|
| blue cup | 0.220 | -0.080 | 0.000 | 0.080 | 0.065 |
| red cup | 0.220 | 0.080 | 0.000 | 0.080 | 0.065 |
| green block | 0.150 | 0.000 | 0.000 | 0.030 | 0.030 |

## Earlier rounds (do not repeat these)
- v0 on sim: ok

## Lessons from earlier tasks
- 2026-10-20 (stack the red block on the green block): Hover 10 cm above before descending.
- 2026-10-23 (put the ball in the cup): Release only after the place descent finishes.
