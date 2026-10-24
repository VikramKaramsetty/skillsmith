# assets/so101: SO101 robot description (WS2)

Vendored third-party files. Refresh them only with `fetch_so101.py`; never hand-edit them.

```bash
python assets/so101/fetch_so101.py            # needs git + network; writes into this folder
```

| File(s) | Upstream | Used by | Changes |
|---|---|---|---|
| `so101_new_calib.urdf`, `urdf_meshes/*.stl` | [TheRobotStudio/SO-ARM100](https://github.com/TheRobotStudio/SO-ARM100) `Simulation/SO101/` | placo (`kinematics.py`), `config/robot.yaml: robot.urdf` | mesh `filename`s rewritten to `urdf_meshes/<name>` |
| `scene.xml`, `so101.xml`, `assets/*` | [google-deepmind/mujoco_menagerie](https://github.com/google-deepmind/mujoco_menagerie) `robotstudio_so101/` | MuJoCo (`hal/sim.py`), `config/robot.yaml: robot.mjcf` | none |
| `LICENSE.so-arm100`, `LICENSE.menagerie` | the upstream LICENSE files, verbatim | | none |
| `README.menagerie.md` | Menagerie's `README.md` for the model | | renamed only |
| `SOURCES.json` | written by the script: upstream commit SHAs and sha256 of every vendored file | `tests/ws2/test_so101_assets.py` | |

Notes:
- The URDF meshes are kept separate from Menagerie's `assets/` so the two sources can never overwrite
  each other. Only the meshes the URDF references are copied.
- `so101_new_calib` puts joint zero at mid-range, which should match LeRobot `use_degrees=True`.
  WS2.7 checks this on the real arm. Do not apply the hqfang pi05 sign/offset convention here.
- Both upstreams are redistributed under their own licenses (see the `LICENSE.*` files). Keep them
  next to the files they cover.
