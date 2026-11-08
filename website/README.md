# Limulator website

This is a dependency-free static site. From this directory:

```sh
npm run build
npm run preview
```

Preview runs at `http://127.0.0.1:4173`. Set `PORT` to choose another local port. `dist/` is generated from `src/` and is ignored by Git.

The built assets and evidence paths are relative, so the site can also be served from a repository subdirectory such as `/limulator/`.

## Previewing and publishing

After editing the site or its evidence, build and preview it locally:

```sh
npm --prefix website run build
npm --prefix website run preview
```

The preview server listens at `http://127.0.0.1:4173` by default. The `Publish GitHub Pages` workflow builds the exact commit that triggered it and deploys the resulting `website/dist` artifact. A push to `main` publishes automatically; `workflow_dispatch` can rebuild and publish from `main` when needed. Select `main` when starting a manual run.

## Updating project evidence

The site reads its validation state from `src/data/evidence.json` and device profiles from `src/data/profiles.json`. Keep those files current as native checks complete.

Add only real guest PNGs under `src/assets/native/`, then add an entry to `captures` with its `file`, `title`, `alt`, `context`, `device`, `os`, and `posture`. The gallery includes four direct CounterLab captures from compatible-graphics iPhone 16 runs, an older diagnostic BruTV frame, and one closed-Duo BruTV fixture playback capture. Keep each capture's graphics mode, hash, source, and validation limit with it. App performance results belong in `applicationMeasurements`; keep workload, method, and environment context with each measurement.

Profiles describe validation state, not general iOS support. Update the model, OS, scale, geometry, and evidence status together when a profile is tested.
